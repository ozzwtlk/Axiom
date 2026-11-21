"""Chapter extraction stage: pages → PageBlocks → persisted source blocks assigned to sections."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pymupdf

from axiom.config import settings
from axiom.extraction.layout import (PageBlock, estimate_geometry, group_rows, lines_from_pymupdf, margin_blocks,
                                     segment_page, split_margin)
from axiom.ingestion.pdf import open_pdf
from axiom.storage import db

EXTRACTOR_VERSION = "layout-v1"


def ink_fraction(page: pymupdf.Page, bbox: tuple[float, float, float, float]) -> float:
    """Fraction of dark pixels in a region (cheap test whether an OCR-empty band contains anything)."""
    r = pymupdf.Rect(bbox) & page.rect
    if r.is_empty or r.height < 2:
        return 0.0
    pix = page.get_pixmap(dpi=60, clip=r, colorspace=pymupdf.csGRAY)
    s = pix.samples
    if not s:
        return 0.0
    return sum(1 for b in s if b < 110) / len(s)


def fit_to_ink(page: pymupdf.Page, bbox, upper: float, lower: float, max_grow: float = 30.0,
               dpi: int = 100) -> tuple[float, float, float, float]:
    """Grow a display-math box vertically while its top/bottom edge cuts through ink (e.g. a tall matrix whose
    band was cut at an OCR row boundary), stopping at the first blank strip (~2pt). Never crosses `upper` /
    `lower` (the neighbouring blocks), so no foreign text is pulled into the crop."""
    x0, y0, x1, y1 = bbox
    lo, hi = max(upper, y0 - max_grow, page.rect.y0), min(lower, y1 + max_grow, page.rect.y1)
    if hi - lo < 4 or x1 - x0 < 4:
        return bbox
    pix = page.get_pixmap(dpi=dpi, clip=pymupdf.Rect(x0, lo, x1, hi), colorspace=pymupdf.csGRAY)
    w, h, smp = pix.width, pix.height, pix.samples
    if not h:
        return bbox
    scale = h / (hi - lo)
    ink = [sum(1 for v in smp[r * w:(r + 1) * w] if v < 120) >= 2 for r in range(h)]
    gap = max(2, round(2.0 * scale))  # rows of white that count as a separation

    def row(y):
        return min(h - 1, max(0, int((y - lo) * scale)))

    def grow(start, step):
        r, blank = start, 0
        while 0 <= r < h:
            blank = blank + 1 if not ink[r] else 0
            if blank >= gap:
                return r - step * (gap - 1)
            r += step
        return None  # ran into the neighbour/limit without a clean gap: leave the edge alone

    ny0, ny1 = y0, y1
    top = row(y0)
    if any(ink[top:top + 2]):
        r = grow(top, -1)
        if r is not None:
            ny0 = lo + r / scale
    bot = row(y1)
    if any(ink[max(0, bot - 1):bot + 1]):
        r = grow(bot, 1)
        if r is not None:
            ny1 = lo + (r + 1) / scale
    return (x0, min(y0, ny0), x1, max(y1, ny1))


def refit_equation_boxes(doc_id: str, chapter_ids: list[str] | None = None) -> list[str]:
    """Apply fit_to_ink to stored equation blocks; blocks whose box grew get their transcription reset so the
    transcribe stage redoes them (and the sections' later stages are invalidated). Returns changed block ids."""
    pdf = open_pdf(doc_id)
    q = "SELECT * FROM blocks WHERE doc_id=? ORDER BY page, y0"
    rows = [r for r in db.q(q, (doc_id,))
            if not chapter_ids or db.q1("SELECT chapter_id FROM sections WHERE id=?", (r["section_id"],))["chapter_id"] in chapter_ids]
    by_page: dict[int, list] = {}
    for r in rows:
        by_page.setdefault(r["page"], []).append(r)
    changed: list[str] = []
    with db.connect() as c:
        for pno, blocks in by_page.items():
            page = pdf[pno - 1]
            for b in blocks:
                if b["block_type"] != "equation":
                    continue
                others = [o for o in blocks if o["id"] != b["id"] and min(o["x1"], b["x1"]) - max(o["x0"], b["x0"]) > 20]
                upper = max([o["y1"] for o in others if o["y1"] <= b["y0"] + 1] or [0.0])
                lower = min([o["y0"] for o in others if o["y0"] >= b["y1"] - 1] or [page.rect.y1])
                nb = fit_to_ink(page, (b["x0"], b["y0"], b["x1"], b["y1"]), upper, lower)
                if nb[1] < b["y0"] - 1.5 or nb[3] > b["y1"] + 1.5:
                    chash = hashlib.sha256(f"{EXTRACTOR_VERSION}|equation|{nb}|{b['raw_text']}".encode()).hexdigest()[:16]
                    flags = [f for f in json.loads(b["flags"]) if f != "box_refit"] + ["box_refit"]
                    c.execute("UPDATE blocks SET y0=?, y1=?, content_hash=?, transcription_method='none', transcribed_md=NULL,"
                              " confidence=0, flags=? WHERE id=?", (nb[1], nb[3], chash, json.dumps(flags), b["id"]))
                    c.execute("UPDATE sections SET stage='extracted' WHERE id=?", (b["section_id"],))
                    crop = block_crop_path(doc_id, b["id"])
                    if crop.exists():
                        crop.unlink()
                    changed.append(b["id"])
    return changed


def crop_png(doc_id: str, page_no: int, bbox, dpi: int | None = None, pdf: pymupdf.Document | None = None) -> bytes:
    pdf = pdf or open_pdf(doc_id)
    page = pdf[page_no - 1]
    r = pymupdf.Rect(bbox).normalize()
    if r.height < 6:  # degenerate box: pad vertically so there is something to look at
        r = pymupdf.Rect(r.x0, r.y0 - 4, r.x1, r.y1 + 4)
    r = r & page.rect
    if r.is_empty or r.width < 2 or r.height < 2:
        raise ValueError(f"empty crop region {bbox} on page {page_no}")
    return page.get_pixmap(dpi=dpi or settings.render_dpi, clip=r).tobytes("png")


def block_crop_path(doc_id: str, block_id: str) -> Path:
    return settings.doc_dir(doc_id) / "crops" / f"{block_id}.png"


def _merge_by_y(main: list[PageBlock], extra: list[PageBlock]) -> list[PageBlock]:
    """Insert margin blocks into reading order next to the main-column block at the same height."""
    out = list(main)
    for m in extra:
        idx = next((i for i, b in enumerate(out) if b.bbox[1] > m.bbox[1]), len(out))
        out.insert(idx, m)
    return out


def extract_chapter(doc_id: str, chapter_id: str, force: bool = False) -> int:
    ch = db.q1("SELECT * FROM chapters WHERE id=?", (chapter_id,))
    sections = db.q("SELECT * FROM sections WHERE chapter_id=? ORDER BY ord", (chapter_id,))
    # "already extracted" means the chapter HAS blocks; a stage flag alone is not evidence (a stale
    # 'transcribed' flag on block-less sections once made whole chapters get skipped and come out empty)
    have = db.q1("SELECT COUNT(*) n FROM blocks WHERE section_id IN (SELECT id FROM sections WHERE chapter_id=?)",
                 (chapter_id,))["n"]
    if not force and have > 0:
        return 0
    before: dict[str, tuple[frozenset, str, str]] = {}
    if force:  # per section: its blocks, stage and status, to find out which sections a re-extraction changed
        for s in sections:
            hs = frozenset(r["content_hash"] for r in db.q("SELECT content_hash FROM blocks WHERE section_id=?", (s["id"],)))
            before[s["id"]] = (hs, s["stage"], s["status"])
    by_number = {s["number"]: s for s in sections}
    # born-digital notes: the outline anchors say exactly where each section starts, so text is split by position
    # (headings in such notes have no fixed style to detect). Scans (no anchors) switch at a detected heading.
    anchors = [((s["page_start"], s["start_y"] or 0.0), s) for s in sections]
    positional = any(s["start_y"] is not None for s in sections)
    pdf = open_pdf(doc_id)
    current = sections[0]
    assigned: list[tuple[str, PageBlock, bool]] = []
    for pno in range(ch["page_start"], ch["page_end"] + 1):
        page = pdf[pno - 1]
        main, margin = split_margin(lines_from_pymupdf(page.get_text("dict")))
        rows = group_rows(main)
        pblocks = segment_page(rows, pno, page.rect.width, page.rect.height, in_exercises=bool(current["is_exercises"]))
        if margin and rows:
            geo = estimate_geometry(rows, page.rect.height)
            mblocks = [m for m in margin_blocks(margin, pno, geo.left, geo.header_bottom or 60.0)
                       if m.kind != "figure" or ink_fraction(page, m.bbox) > 0.01]
            pblocks = _merge_by_y(pblocks, mblocks)
        for b in pblocks:
            if b.kind == "equation":
                others = [o for o in pblocks if o is not b and min(o.bbox[2], b.bbox[2]) - max(o.bbox[0], b.bbox[0]) > 20]
                upper = max([o.bbox[3] for o in others if o.bbox[3] <= b.bbox[1] + 1] or [0.0])
                lower = min([o.bbox[1] for o in others if o.bbox[1] >= b.bbox[3] - 1] or [page.rect.y1])
                b.bbox = fit_to_ink(page, b.bbox, upper, lower)
        first_text_seen = False
        for b in pblocks:
            if positional:
                at = (pno, b.bbox[1] + 2)
                current = next((s for key, s in reversed(anchors) if key <= at), sections[0])
            elif b.kind == "heading" and b.heading_number in by_number:
                current = by_number[b.heading_number]
            if "empty_gap_visual" in b.flags and ink_fraction(page, b.bbox) < 0.004:
                continue  # blank band, nothing to transcribe
            cont = False
            if b.kind in ("paragraph", "exercise") and not first_text_seen:
                cont = not b.starts_paragraph
            if b.kind in ("paragraph", "exercise", "equation", "heading"):
                first_text_seen = True
            assigned.append((current["id"], b, cont))

    with db.connect() as c:
        keep: dict[str, tuple] = {}
        if force:
            for r in c.execute("SELECT content_hash, transcribed_md, transcription_method, confidence, flags FROM blocks"
                               " WHERE section_id IN (SELECT id FROM sections WHERE chapter_id=?)", (chapter_id,)):
                if r["transcribed_md"]:
                    keep[r["content_hash"]] = (r["transcribed_md"], r["transcription_method"], r["confidence"], r["flags"])
            c.execute("DELETE FROM search_fts WHERE section_id IN (SELECT id FROM sections WHERE chapter_id=?)", (chapter_id,))
            c.execute("DELETE FROM blocks WHERE section_id IN (SELECT id FROM sections WHERE chapter_id=?)", (chapter_id,))
        per_page: dict[int, int] = {}
        ords: dict[str, int] = {}
        for sid, b, cont in assigned:
            k = per_page.get(b.page, 0)
            per_page[b.page] = k + 1
            bid = f"{doc_id}-p{b.page:04d}-{k:02d}"
            ords[sid] = ords.get(sid, 0) + 1
            chash = hashlib.sha256(f"{EXTRACTOR_VERSION}|{b.kind}|{b.bbox}|{b.text}".encode()).hexdigest()[:16]
            prev = keep.get(chash)
            btype = b.kind
            c.execute(
                "INSERT OR REPLACE INTO blocks(id,doc_id,section_id,ord,page,x0,y0,x1,y1,block_type,raw_text,math_score,"
                "eq_number,content_hash,continues_prev,transcribed_md,transcription_method,confidence,flags)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (bid, doc_id, sid, ords[sid], b.page, *b.bbox, btype,
                 b.text if b.kind != "exercise" else b.text, b.math_score, b.eq_number or b.exercise_number, chash,
                 int(cont), prev[0] if prev else None, prev[1] if prev else "none", prev[2] if prev else 0.0,
                 prev[3] if prev else db.jdump(b.flags)))
        for s in sections:
            after = frozenset(r["content_hash"] for r in c.execute("SELECT content_hash FROM blocks WHERE section_id=?",
                                                                    (s["id"],)))
            if force and s["id"] in before and before[s["id"]][0] == after:
                continue  # same blocks as before: keep its finished stages
            # fresh blocks: every later stage of this section must run again (its finished jobs go back to the queue)
            c.execute("UPDATE sections SET stage='extracted', status='pending' WHERE id=?", (s["id"],))
            if force:
                c.execute("UPDATE jobs SET status='pending', attempts=0, error=NULL, worker=NULL WHERE target_id=?"
                          " AND kind IN ('transcribe','optimize','verify')", (s["id"],))
    if not assigned:
        raise RuntimeError(f"no text blocks found in chapter {ch['number']} (pages {ch['page_start']}–{ch['page_end']})")
    return len(assigned)
