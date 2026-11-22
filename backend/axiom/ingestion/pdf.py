"""PDF import: immutable copy, hashing, outline → chapter/section structure."""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pymupdf

from axiom.config import settings
from axiom.storage import db, files

CHAPTER_RE = re.compile(r"^\s*(?:CHAPTER|Chapter)\s+(\d+)\s*[:.\-–]?\s*(.*)$")
SECTION_RE = re.compile(r"^\s*(\d+)\s*\.\s*(\d+)\s*[•·.:\-–]?\s*(.*)$")


@dataclass
class OutlineSection:
    number: str
    title: str
    page_start: int  # 1-based
    page_end: int = 0
    start_y: float | None = None  # top of the section on its first page (outline anchor), None = unknown


@dataclass
class OutlineChapter:
    number: int
    title: str
    page_start: int
    page_end: int
    sections: list[OutlineSection]
    start_y: float | None = None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _smart_title(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    if s.isupper():
        small = {"a", "an", "and", "as", "at", "of", "the", "to", "in", "on", "or", "for"}
        words = s.lower().split(" ")
        s = " ".join(w if (i and w in small) else w[:1].upper() + w[1:] for i, w in enumerate(words))
        s = re.sub(r"'S\b", "'s", s)
    return s


# entries that bound page ranges but are not reading material
SKIP_RE = re.compile(r"^\s*(?:(?:table of )?contents|index|bibliography|references|acknowledg\w*|about the authors?|"
                     r"list of (?:figures|tables)|cover|title page)\s*$", re.I)
NUMBERED_RE = re.compile(r"^\s*(?:chapter\s+|lecture\s+)?(\d+)(?:\s*\.\s*(\d+))?\.?\s+(\S.*)$", re.I)
_NUM_LINE = re.compile(r"^\s*(?:chapter\s+|lecture\s+)?(\d+)(?:\s*\.\s*(\d+))?\.?\s*$", re.I)


def _anchor_y(pdf, page: int, dest, title: str | None = None) -> float | None:
    """Where a section starts on its page (top-down y). The outline anchor, corrected to the printed title when
    that is found on the page (anchors are sometimes off); None when neither is known."""
    to = (dest or {}).get("to") if isinstance(dest, dict) else None
    if pdf is None or not (1 <= page <= pdf.page_count):
        return None
    p = pdf[page - 1]
    y = None
    if to is not None and not (to.x == 0 and to.y == 0):
        y = max(0.0, min(float((pymupdf.Point(to) * p.transformation_matrix).y), p.rect.height))
    if y is None:
        return None  # page-only outline (scans): keep heading-based splitting
    key = _norm(re.sub(r"^\s*\d+(?:\.\d+)*\.?\s+", "", title or ""))[:12]
    if key:
        hits, nums = [], []
        for blk in p.get_text("dict")["blocks"]:
            for ln in blk.get("lines", []):
                raw = "".join(sp["text"] for sp in ln["spans"])
                if _NUM_LINE.match(raw):
                    nums.append(ln["bbox"][1])
                t = _norm(re.sub(r"^\s*\d+(?:\.\d+)*\.?\s*", "", raw))
                if t and t.startswith(key):
                    hits.append(ln["bbox"][1])
        # a heading number printed on its own line just above the title belongs to the section
        hits = [min([n for n in nums if 0 <= h - n < 30] + [h]) for h in hits]
        if hits:  # the occurrence nearest to the anchor (a title can also appear in a running head)
            y = min(hits, key=lambda h: abs(h - y)) - 2
    return max(0.0, y)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _printed_number(pdf, page: int, title: str, y: float | None) -> tuple[int, int | None] | None:
    """The number printed with an unnumbered outline title ("1.2." on the line above "Evaluation of ...", or
    "4 Gray Body Factors"), so section numbers match the book's own equation numbers and references."""
    if pdf is None or not (1 <= page <= pdf.page_count):
        return None
    key = _norm(title)[:24]
    if not key:
        return None
    lines = []
    for b in pdf[page - 1].get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            t = "".join(s["text"] for s in ln["spans"]).strip()
            if t:
                lines.append((ln["bbox"][1], t))
    lines.sort()
    for i, (ly, t) in enumerate(lines):
        if y is not None and ly < y - 40:
            continue
        m = NUMBERED_RE.match(t)
        if m and _norm(m.group(3)).startswith(key[:12]) and _norm(t).find(key[:12]) >= 0:
            return int(m.group(1)), int(m.group(2)) if m.group(2) else None
        if _norm(t).startswith(key[:12]):
            for py, prev in reversed(lines[max(0, i - 2):i]):
                m = _NUM_LINE.match(prev)
                if m and ly - py < 30:  # the number printed right above the title, not a page number
                    return int(m.group(1)), int(m.group(2)) if m.group(2) else None
            return None
    return None


def parse_outline(toc: list[list], page_count: int, pdf=None) -> list[OutlineChapter]:
    """Chapters and sections from the PDF outline.

    Books: level-1 "CHAPTER n ..." with level-2 "n.m ..." children (front matter only bounds page ranges).
    Otherwise (lecture notes, tutorials): every level-1 entry is a chapter and its level-2 entries are its sections,
    numbered as printed (from the title, or from the page when the outline title has no number), else in order.
    Unnumbered entries before the first numbered chapter form a "Front matter" chapter 0. With `pdf`, the outline
    anchors give each section's exact start on its page, so extraction can split sections mid-page."""
    entries = [(e[0], e[1], e[2], e[3] if len(e) > 3 else None) for e in toc if e[2] > 0]
    level1_starts = sorted({p for lv, _, p, _ in entries if lv == 1})

    def end_of(page: int) -> int:
        nxt = [p for p in level1_starts if p > page]
        return (nxt[0] - 1) if nxt else page_count

    chapters = _book_chapters(entries, end_of)
    if not chapters:
        chapters = _generic_chapters(entries, end_of, pdf)
    for ch in chapters:
        ch.page_end = max(ch.page_end, ch.page_start)
        for i, s in enumerate(ch.sections):
            s.page_end = ch.sections[i + 1].page_start if i + 1 < len(ch.sections) else ch.page_end
        if not ch.sections:
            ch.sections.append(OutlineSection(f"{ch.number}.0", ch.title, ch.page_start, ch.page_end, ch.start_y))
    return chapters


def _book_chapters(entries, end_of) -> list[OutlineChapter]:
    chapters: list[OutlineChapter] = []
    current: OutlineChapter | None = None
    for level, title, page, _ in entries:
        if level == 1:
            current = None
            m = CHAPTER_RE.match(title)
            if m:
                current = OutlineChapter(int(m.group(1)), _smart_title(m.group(2)), page, end_of(page), [])
                chapters.append(current)
        elif level == 2 and current is not None:
            m = SECTION_RE.match(title)
            if m:
                current.sections.append(OutlineSection(f"{m.group(1)}.{m.group(2)}", _smart_title(m.group(3)), page))
    return chapters


def _generic_chapters(entries, end_of, pdf) -> list[OutlineChapter]:
    # level-1 entries with their level-2 children
    groups: list[tuple[tuple, list[tuple]]] = []
    for e in entries:
        if e[0] == 1:
            groups.append((e, []))
        elif e[0] == 2 and groups:
            groups[-1][1].append(e)
    groups = [g for g in groups if not SKIP_RE.match(g[0][1])]
    if not groups:
        return []

    # when the outline titles carry numbers ("1 The principles", "1.1 A theory"), they are the numbering: an
    # unnumbered title (Prologue, References) is then front/back matter, not something to look up on the page
    def explicit(level: int, title: str):
        m = NUMBERED_RE.match(title)
        return m if m and (level == 1) == (m.group(2) is None) else None
    titled = {lv: any(explicit(lv, e[1]) for e in entries if e[0] == lv) for lv in (1, 2)}

    def number_of(level: int, title: str, page: int, y) -> tuple[tuple[int, int | None] | None, str]:
        m = explicit(level, title)
        if m:
            return (int(m.group(1)), int(m.group(2)) if m.group(2) else None), m.group(3)
        return (None if titled[level] else _printed_number(pdf, page, title, y)), title

    heads = []
    for (lv, title, page, dest), kids in groups:
        y = _anchor_y(pdf, page, dest, title)
        num, clean = number_of(1, title, page, y)
        heads.append((num[0] if num and num[1] is None else None, _smart_title(clean), page, y, kids))
    numbered = [h for h in heads if h[0] is not None]
    if len(numbered) < max(1, len(heads) // 3):  # no real numbering: number in order
        heads = [(i + 1, *h[1:]) for i, h in enumerate(heads)]

    chapters: list[OutlineChapter] = []
    front: OutlineChapter | None = None
    used: set[int] = set()
    seen_numbered = False
    for n, title, page, y, kids in heads:
        if n is None and not seen_numbered:  # Abstract, Prologue, Notation ... before chapter 1
            if front is None:
                front = OutlineChapter(0, "Front matter", page, end_of(page), [], y)
                chapters.append(front)
            front.sections.append(OutlineSection(f"0.{len(front.sections) + 1}", title, page, start_y=y))
            front.page_end = end_of(page)
            continue
        seen_numbered = True
        if n is None or n in used:  # appendices / duplicates: after everything else
            n = max(used | {0}) + 1
        used.add(n)
        ch = OutlineChapter(n, title, page, end_of(page), [], y)
        chapters.append(ch)
        snums: set[str] = set()
        for k, (_, stitle, spage, sdest) in enumerate(k for k in kids if not SKIP_RE.match(k[1])):
            sy = _anchor_y(pdf, spage, sdest, stitle)
            snum, sclean = number_of(2, stitle, spage, sy)
            num = f"{n}.{snum[1]}" if snum and snum[0] == n and snum[1] is not None else f"{n}.{k + 1}"
            if num in snums:
                num = f"{n}.{k + 1}" if f"{n}.{k + 1}" not in snums else f"{n}.{k + 1}b"
            snums.add(num)
            if not ch.sections and _starts_before(page, y, spage, sy):
                # the chapter's own opening text (abstract, overview) before its first section
                ch.sections.append(OutlineSection(f"{n}.0", title, page, start_y=y))
                snums.add(f"{n}.0")
            ch.sections.append(OutlineSection(num, _smart_title(sclean), spage, start_y=sy))
    return chapters


def _starts_before(page: int, y, spage: int, sy) -> bool:
    """Is there room for text between a chapter heading and its first section heading?"""
    if spage > page + 1:
        return True
    if spage == page + 1:
        return y is not None and y < 500
    return y is not None and sy is not None and sy - y > 90


def is_scanned(pdf: pymupdf.Document, samples: int = 12) -> bool:
    """A page is 'scanned' if one image covers ~the whole page (the text layer is then OCR, whose math -- and
    Greek letters inside prose -- cannot be trusted)."""
    n = pdf.page_count
    idx = sorted({int(i * n / samples) for i in range(samples)})
    hits = 0
    for i in idx:
        page = pdf[i]
        area = page.rect.width * page.rect.height
        for info in page.get_image_info():
            x0, y0, x1, y1 = info["bbox"]
            if (x1 - x0) * (y1 - y0) > 0.8 * area:
                hits += 1
                break
    return hits >= 0.6 * len(idx)


def import_pdf(src: Path, title: str | None = None) -> str:
    """Copy the PDF into the data dir (never modified afterwards) and register its structure.
    Idempotent: importing the same file twice returns the same document id."""
    digest = sha256_file(src)
    doc_id = digest[:12]
    if db.q1("SELECT id FROM documents WHERE id=?", (doc_id,)):
        return doc_id
    ddir = settings.doc_dir(doc_id)
    ddir.mkdir(parents=True, exist_ok=True)
    dst = ddir / "original.pdf"
    if not dst.exists():
        shutil.copy2(src, dst)
        dst.chmod(0o444)
    files.publish(f"docs/{doc_id}/original.pdf")  # worker: the server keeps the original
    pdf = pymupdf.open(dst)
    meta_title = (pdf.metadata or {}).get("title") or ""
    doc_title = title or meta_title or src.stem
    chapters = parse_outline(pdf.get_toc(False), pdf.page_count, pdf)
    if not chapters:  # no usable outline: single chapter, single section (documented limitation)
        chapters = [OutlineChapter(1, doc_title, 1, pdf.page_count,
                                   [OutlineSection("1.0", doc_title, 1, pdf.page_count)])]
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as c:
        c.execute(
            "INSERT INTO documents(id,title,filename,sha256,page_count,created_at,meta) VALUES (?,?,?,?,?,?,?)",
            (doc_id, doc_title, src.name, digest, pdf.page_count, now, db.jdump({"toc_entries": len(pdf.get_toc()), "scanned": is_scanned(pdf)})),
        )
        _insert_structure(c, doc_id, chapters)
    return doc_id


def _insert_structure(c, doc_id: str, chapters: list[OutlineChapter]) -> None:
    for ch in chapters:
        cid = f"{doc_id}-c{ch.number}"
        c.execute("INSERT INTO chapters(id,doc_id,number,title,page_start,page_end) VALUES (?,?,?,?,?,?)",
                  (cid, doc_id, ch.number, ch.title, ch.page_start, ch.page_end))
        for i, s in enumerate(ch.sections):
            c.execute(
                "INSERT INTO sections(id,doc_id,chapter_id,ord,number,title,page_start,page_end,is_exercises,start_y)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"{doc_id}-s{s.number}", doc_id, cid, i, s.number, s.title, s.page_start, s.page_end,
                 int(bool(re.search(r"\b(exercises|problems)\b", s.title, re.I))), s.start_y),
            )


def restructure(doc_id: str, force: bool = False) -> dict:
    """Read the outline again (after the outline parser improved). Only for documents nothing was extracted from
    yet, unless `force`: then all derived data of the document (blocks, units, jobs, ...) is discarded."""
    d = db.q1("SELECT title, page_count FROM documents WHERE id=?", (doc_id,))
    if not d:
        raise KeyError(doc_id)
    n_blocks = db.q1("SELECT COUNT(*) n FROM blocks WHERE doc_id=?", (doc_id,))["n"]
    if n_blocks and not force:
        raise RuntimeError(f"{n_blocks} blocks were already extracted; pass force to discard them")
    pdf = open_pdf(doc_id)
    chapters = parse_outline(pdf.get_toc(False), pdf.page_count, pdf)
    if not chapters:
        return {"chapters": 0, "changed": False}
    with db.connect(immediate=True) as c:
        if not c.execute("SELECT 1 FROM documents WHERE id=?", (doc_id,)).fetchone():
            raise KeyError(doc_id)
        if c.execute("SELECT 1 FROM jobs WHERE doc_id=? AND status='running'", (doc_id,)).fetchone():
            raise RuntimeError("Wait for running jobs to finish before restructuring this document")
        secs = [r["id"] for r in c.execute("SELECT id FROM sections WHERE doc_id=?", (doc_id,)).fetchall()]
        marks = ",".join("?" * len(secs)) or "NULL"
        for t in ("units", "coverage", "notation", "concepts", "study_cards"):
            c.execute(f"DELETE FROM {t} WHERE section_id IN ({marks})", secs)
        c.execute("DELETE FROM search_fts WHERE doc_id=?", (doc_id,))
        c.execute("DELETE FROM exercises WHERE doc_id=?", (doc_id,))
        c.execute("DELETE FROM visualizations WHERE doc_id=?", (doc_id,))
        c.execute("DELETE FROM blocks WHERE doc_id=?", (doc_id,))
        c.execute("DELETE FROM jobs WHERE doc_id=? AND status!='running'", (doc_id,))
        c.execute("DELETE FROM sections WHERE doc_id=?", (doc_id,))
        c.execute("DELETE FROM chapters WHERE doc_id=?", (doc_id,))
        _insert_structure(c, doc_id, chapters)
    return {"chapters": len(chapters), "sections": sum(len(ch.sections) for ch in chapters), "changed": True}


def open_pdf(doc_id: str) -> pymupdf.Document:
    return pymupdf.open(files.fetch(f"docs/{doc_id}/original.pdf"))
