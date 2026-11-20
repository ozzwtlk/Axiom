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


@dataclass
class OutlineChapter:
    number: int
    title: str
    page_start: int
    page_end: int
    sections: list[OutlineSection]


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


def parse_outline(toc: list[list], page_count: int) -> list[OutlineChapter]:
    """Build chapters from a PDF outline. Level-1 entries named 'CHAPTER n ...' are chapters,
    their level-2 children 'n.m ...' are sections. Non-chapter level-1 entries bound page ranges."""
    chapters: list[OutlineChapter] = []
    level1_starts = sorted({e[2] for e in toc if e[0] == 1 and e[2] > 0})
    current: OutlineChapter | None = None
    for level, title, page, *_ in toc:
        if level == 1:
            current = None
            m = CHAPTER_RE.match(title)
            if m:
                nxt = [p for p in level1_starts if p > page]
                end = (nxt[0] - 1) if nxt else page_count
                current = OutlineChapter(int(m.group(1)), _smart_title(m.group(2)), page, end, [])
                chapters.append(current)
        elif level == 2 and current is not None:
            m = SECTION_RE.match(title)
            if m:
                current.sections.append(
                    OutlineSection(f"{m.group(1)}.{m.group(2)}", _smart_title(m.group(3)), page)
                )
    for ch in chapters:
        for i, s in enumerate(ch.sections):
            s.page_end = ch.sections[i + 1].page_start if i + 1 < len(ch.sections) else ch.page_end
        if not ch.sections:
            ch.sections.append(OutlineSection(f"{ch.number}.0", ch.title, ch.page_start, ch.page_end))
    return chapters


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
    chapters = parse_outline(pdf.get_toc(), pdf.page_count)
    if not chapters:  # no usable outline: single chapter, single section (documented limitation)
        chapters = [OutlineChapter(1, doc_title, 1, pdf.page_count,
                                   [OutlineSection("1.0", doc_title, 1, pdf.page_count)])]
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as c:
        c.execute(
            "INSERT INTO documents(id,title,filename,sha256,page_count,created_at,meta) VALUES (?,?,?,?,?,?,?)",
            (doc_id, doc_title, src.name, digest, pdf.page_count, now, db.jdump({"toc_entries": len(pdf.get_toc()), "scanned": is_scanned(pdf)})),
        )
        for ch in chapters:
            cid = f"{doc_id}-c{ch.number}"
            c.execute("INSERT INTO chapters(id,doc_id,number,title,page_start,page_end) VALUES (?,?,?,?,?,?)",
                      (cid, doc_id, ch.number, ch.title, ch.page_start, ch.page_end))
            for i, s in enumerate(ch.sections):
                c.execute(
                    "INSERT INTO sections(id,doc_id,chapter_id,ord,number,title,page_start,page_end,is_exercises)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (f"{doc_id}-s{s.number}", doc_id, cid, i, s.number, s.title, s.page_start, s.page_end,
                     int(bool(re.search(r"\b(exercises|problems)\b", s.title, re.I)))),
                )
    return doc_id


def open_pdf(doc_id: str) -> pymupdf.Document:
    return pymupdf.open(files.fetch(f"docs/{doc_id}/original.pdf"))
