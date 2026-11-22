"""Cross-references: every "(2.13)", "Figure 1.5", "Section 2.4" in the text links to its target.

The index is built from the *source blocks* (equation numbers, figure captions) and the outline, so it exists as
soon as a chapter is extracted and does not depend on the optimizer. Targets are block ids, which are stable
across re-optimizing; the reader resolves a block to the unit that currently shows it.

`unresolved` lists references in the reader text whose target is not in the book's processed part (often a
later chapter that has not been processed yet, sometimes an OCR misreading of the number).

The reference patterns are shared with the frontend (`frontend/src/lib/xrefs.ts`); keep them in sync.
"""

from __future__ import annotations

import json
import re

from axiom.storage import db

EQ_REF = re.compile(r"\((\d{1,2}\.\d{1,3}[a-z]?)\)")
FIG_REF = re.compile(r"\b(?:Fig(?:ure)?s?\.?)\s+(\d{1,2}\.\d{1,3})", re.I)
SEC_REF = re.compile(r"(?:\bSections?|\bSec\.|§)\s*(\d{1,2}\.\d{1,2})\b")
CAPTION = re.compile(r"^\W*(?:FIGURE|Figure|Fig\.?)\s+(\d{1,2}\.\d{1,3})")
_MATH = re.compile(r"\$\$.+?\$\$|\$[^$\n]+\$", re.S)


def _md(b) -> str:
    return b["transcribed_md"] if b["transcribed_md"] is not None else (b["raw_text"] or "")


def _display(md: str) -> str | None:
    m = re.search(r"\$\$(.+?)\$\$", md or "", re.S)
    return m.group(1).strip() if m else None


def xrefs(doc_id: str) -> dict:
    equations: dict[str, dict] = {}
    figures: dict[str, dict] = {}
    last_figure: dict[str, str] = {}  # section -> latest figure block (a caption belongs to the figure above it)
    for b in db.q("SELECT id, section_id, page, block_type, eq_number, transcribed_md, raw_text FROM blocks"
                  " WHERE doc_id=? AND block_type IN ('equation','figure','caption') ORDER BY section_id, ord", (doc_id,)):
        if b["block_type"] == "equation" and b["eq_number"]:
            equations.setdefault(b["eq_number"], {"block_id": b["id"], "section_id": b["section_id"], "page": b["page"],
                                                  "latex": _display(_md(b))})
        elif b["block_type"] == "figure":
            last_figure[b["section_id"]] = b["id"]
        elif b["block_type"] == "caption":
            m = CAPTION.match(_md(b))
            if m and m.group(1) not in figures:
                fig = last_figure.pop(b["section_id"], None)
                figures[m.group(1)] = {"block_id": b["id"], "section_id": b["section_id"], "page": b["page"],
                                       "caption": _md(b)[:400],
                                       "image_url": f"/api/blocks/{fig}/image" if fig else None}
    sections = {}
    chapters = {}
    for s in db.q("SELECT s.id, s.number, s.title, c.number AS cn FROM sections s JOIN chapters c ON c.id=s.chapter_id"
                  " WHERE s.doc_id=? ORDER BY c.number, s.ord", (doc_id,)):
        if s["number"]:
            sections.setdefault(s["number"], {"section_id": s["id"], "title": s["title"]})
        chapters.setdefault(str(s["cn"]), {"section_id": s["id"], "title": s["title"]})

    unresolved: dict[str, dict] = {}
    for r in db.q("SELECT u.section_id, u.data FROM units u JOIN sections s ON s.id=u.section_id WHERE s.doc_id=?",
                  (doc_id,)):
        u = json.loads(r["data"])
        text = " ".join(filter(None, [u.get("text_md"), u.get("fast_md"), u.get("deep_md"), u.get("annotation_md"),
                                      *[n.get("md") for n in u.get("notes") or []]]))
        if u.get("kind") == "equation":  # an equation unit's own number is not a reference
            text = text.replace(f"({u.get('eq_number')})", " ")
        text = _MATH.sub(" ", text)
        for kind, rx, known in (("equation", EQ_REF, equations), ("figure", FIG_REF, figures),
                                ("section", SEC_REF, sections)):
            for m in rx.finditer(text):
                if m.group(1) not in known:
                    unresolved.setdefault(f"{kind}:{m.group(1)}", {"kind": kind, "ref": m.group(1),
                                                                    "section_id": r["section_id"]})
    return {"equations": equations, "figures": figures, "sections": sections, "chapters": chapters,
            "unresolved": sorted(unresolved.values(), key=lambda x: (x["kind"], x["ref"]))}
