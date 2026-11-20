"""Deterministic page segmentation: text-layer lines → semantic source blocks with bounding boxes.

Designed for typeset single-column textbooks (including scanned books with an OCR text layer).
Key observations it relies on (all measured on real pages, see tests/test_layout.py):
  * the body column has a stable left edge; paragraph starts are indented relative to it;
  * display math is indented further, and/or carries a right-aligned tag "(n.m)";
  * OCR frequently produces *no text at all* for display equations, so equation regions are cut
    as vertical bands between body rows rather than taken from OCR text;
  * sub/superscripts appear as spans with markedly smaller font size → math signal.
The column geometry is estimated per page from the data, not hard-coded.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

# OCR often reads the digit 1 as "l" or "I" inside tags: "(l.41)", "(I.44)"
EQ_TAG_RE = re.compile(r"^\(\s*([\dlI]+)\s*\.\s*([\dlI]+)\s*\)[.,;]?$")


def _fix_digits(s: str) -> str:
    return s.replace("l", "1").replace("I", "1")
SECTION_HEAD_RE = re.compile(r"^\s*(\d+)\s*\.\s*(\d+)\s*[•·]\s*(.+)$")
FIGURE_RE = re.compile(r"^\s*FIGURE\s+(\d+\s*\.\s*\d+)", re.I)
EXERCISE_ITEM_RE = re.compile(r"^\s*(\d{1,2})\s*\.\s+\S")
GREEK_OR_SYMBOL = re.compile(r"[µαβγδεζηθικλνξπρστυφχψωΓΔΘΛΞΠΣΦΨΩ∂∇∫∑∏±≤≥≠≈∞→←↔⊗⊕√~^_{}\\|<>=]")


@dataclass
class Span:
    text: str
    size: float
    font: str
    x0: float = 0.0
    x1: float = 0.0


@dataclass
class Line:
    x0: float
    y0: float
    x1: float
    y1: float
    spans: list[Span]

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class Row:
    """Lines sharing a baseline band (sub/superscript fragments merged into their host line)."""
    lines: list[Line]

    @property
    def x0(self) -> float: return min(line.x0 for line in self.lines)
    @property
    def x1(self) -> float: return max(line.x1 for line in self.lines)
    @property
    def y0(self) -> float: return min(line.y0 for line in self.lines)
    @property
    def y1(self) -> float: return max(line.y1 for line in self.lines)

    @property
    def text(self) -> str:
        return " ".join(line.text.strip() for line in sorted(self.lines, key=lambda l: l.x0)).strip()

    @property
    def spans(self) -> list[Span]:
        return [s for line in sorted(self.lines, key=lambda l: l.x0) for s in line.spans]

    @property
    def median_size(self) -> float:
        sizes = [s.size for s in self.spans for _ in range(max(1, len(s.text.strip())))]
        return statistics.median(sizes) if sizes else 0.0

    @property
    def fonts(self) -> set[str]:
        return {s.font for s in self.spans}


@dataclass
class PageBlock:
    page: int  # 1-based
    kind: str  # heading | paragraph | equation | figure | caption | exercise | footnote
    bbox: tuple[float, float, float, float]
    text: str
    math_score: float = 0.0
    eq_number: str | None = None
    heading_number: str | None = None
    exercise_number: str | None = None
    starts_paragraph: bool = True
    flags: list[str] = field(default_factory=list)


@dataclass
class PageGeometry:
    left: float
    right: float
    body_size: float
    line_pitch: float
    header_bottom: float
    footer_top: float


def lines_from_pymupdf(page_dict: dict) -> list[Line]:
    out: list[Line] = []
    for b in page_dict.get("blocks", []):
        if b.get("type") != 0:
            continue
        for ln in b.get("lines", []):
            spans = [Span(s["text"], float(s["size"]), s.get("font", ""), s["bbox"][0], s["bbox"][2])
                     for s in ln["spans"] if s["text"]]
            if not spans or not "".join(s.text for s in spans).strip():
                continue
            x0, y0, x1, y1 = ln["bbox"]
            out.append(Line(x0, y0, x1, y1, spans))
    return out


def split_margin(lines: list[Line]) -> tuple[list[Line], list[Line]]:
    """Separate left-margin material (margin figures/captions, common in textbooks with a wide outer
    margin) from the main column. The main column's left edge is estimated from wide lines."""
    wide = [ln for ln in lines if ln.x1 - ln.x0 > 200]
    if len(wide) < 5:
        return lines, []
    left = statistics.median(sorted(ln.x0 for ln in wide)[: max(3, len(wide) // 4)])
    main, margin = [], []
    for ln in lines:
        if ln.x1 <= left - 6 and ln.x0 < left - 70:
            margin.append(ln)
        elif ln.x0 < left - 70 and any(sp.x1 <= left - 6 for sp in ln.spans):
            # OCR merged a margin line with a body line: split at span level
            ms = [sp for sp in ln.spans if sp.x1 <= left - 6]
            bs = [sp for sp in ln.spans if sp.x1 > left - 6]
            margin.append(Line(ln.x0, ln.y0, max(sp.x1 for sp in ms), ln.y1, ms))
            if bs and "".join(sp.text for sp in bs).strip():
                main.append(Line(min(sp.x0 for sp in bs), ln.y0, ln.x1, ln.y1, bs))
        else:
            main.append(ln)
    return main, margin


def margin_blocks(margin: list[Line], page: int, col_left: float, top: float) -> list["PageBlock"]:
    """Margin captions ("FIGURE n.m ...") become caption blocks; the region above each caption becomes
    a figure block. Other margin text is attached to the nearest caption or dropped as a label."""
    out: list[PageBlock] = []
    rows = sorted((ln for ln in margin if ln.y0 > top - 1), key=lambda l: l.y0)
    groups: list[list[Line]] = []
    for ln in rows:
        if re.match(r"\s*FIGURE\b", ln.text, re.I) or not groups or ln.y0 - groups[-1][-1].y1 > 6:
            groups.append([ln])
        else:
            groups[-1].append(ln)
    prev_bottom = top
    for g in groups:
        text = _dehyphenate_join([ln.text for ln in g])
        y0, y1 = min(ln.y0 for ln in g), max(ln.y1 for ln in g)
        if FIGURE_RE.match(text):
            if y0 - prev_bottom > 20:
                out.append(PageBlock(page, "figure", (20.0, prev_bottom + 1, col_left - 4, y0 - 1), "",
                                     flags=["margin_figure"]))
            out.append(PageBlock(page, "caption", (min(ln.x0 for ln in g), y0, max(ln.x1 for ln in g), y1), text,
                                 flags=["margin"]))
            prev_bottom = y1
    return out


def group_rows(lines: list[Line]) -> list[Row]:
    """Merge lines into rows. A small fragment (sub/superscript, or a part of an equation) joins
    a row if its vertical centre lies within the row's band."""
    rows: list[Row] = []
    for ln in sorted(lines, key=lambda l: (l.yc, l.x0)):
        placed = False
        for r in reversed(rows[-3:]):
            h = max(4.0, min(r.y1 - r.y0, 14.0))
            overlap = min(r.y1, ln.y1) - max(r.y0, ln.y0)
            if overlap > 0.45 * min(h, ln.y1 - ln.y0) and abs(ln.yc - (r.y0 + r.y1) / 2) < 0.6 * h:
                # don't merge two full-width text lines (they are separate rows that touch)
                if (ln.x1 - ln.x0) > 150 and any((x.x1 - x.x0) > 150 for x in r.lines):
                    continue
                r.lines.append(ln)
                placed = True
                break
        if not placed:
            rows.append(Row([ln]))
    rows.sort(key=lambda r: (r.y0, r.x0))
    return rows


def estimate_geometry(rows: list[Row], page_height: float) -> PageGeometry:
    wide = [r for r in rows if r.x1 - r.x0 > 200]
    if not wide:
        lefts = [r.x0 for r in rows] or [0.0]
        return PageGeometry(min(lefts), max((r.x1 for r in rows), default=500.0), 10.0, 12.0, 0.0, page_height)
    left = statistics.median([r.x0 for r in wide])
    # body left edge = the most common left coordinate among wide rows (continuation lines)
    buckets: dict[int, int] = {}
    for r in wide:
        buckets[round(r.x0 / 3)] = buckets.get(round(r.x0 / 3), 0) + 1
    # leftmost bucket with real support (exercise lists have hanging indents: item starts are
    # the minority but define the column edge)
    need = max(2, 0.10 * len(wide))
    supported = [k for k, v in buckets.items() if v >= need]
    # pages with few wide lines (chapter openers, figure pages) may have no supported bucket: keep the median
    left = min(supported) * 3.0 if supported else left
    left = min((r.x0 for r in wide if abs(r.x0 - left) < 4), default=left)
    right = statistics.median([r.x1 for r in wide])
    body_size = statistics.median([r.median_size for r in wide])
    ys = sorted(r.y0 for r in wide)
    pitches = [b - a for a, b in zip(ys, ys[1:]) if 8 < b - a < 20]
    pitch = statistics.median(pitches) if pitches else body_size * 1.25
    header_bottom = 0.0
    first = min(rows, key=lambda r: r.y0)
    # running head: top row in the top band carrying a folio number (e.g. "1.3 Lorentz Transformations 13")
    if first.y1 < 0.11 * page_height and (re.search(r"(^\s*\d{1,4}\s)|(\s\d{1,4}\s*$)", first.text)
                                          or re.search(r"\bChapter\b", first.text)):
        header_bottom = max(r.y1 for r in rows if r.y0 < first.y1 - 2)
    footer_top = page_height
    for r in rows:  # folio at the bottom (chapter openers)
        if r.y0 > 0.88 * page_height and re.fullmatch(r"\s*\d{1,4}\s*", r.text):
            footer_top = min(footer_top, r.y0)
    return PageGeometry(left, right, body_size, pitch, header_bottom, footer_top)


def math_score(row_or_text_spans: list[Span], body_size: float) -> float:
    """Evidence that a text run contains mathematics the OCR layer probably mangled."""
    score = 0.0
    for s in row_or_text_spans:
        t = s.text.strip()
        if not t:
            continue
        if s.size < 0.86 * body_size:
            score += 1.0  # sub/superscript
        if "Italic" in s.font or "Oblique" in s.font:
            if len(t) <= 3:
                score += 0.6  # italic single letters = math variables
        if s.font.startswith("HiddenHorzOCR"):
            score += 0.5
        score += 0.4 * len(GREEK_OR_SYMBOL.findall(t))
    return score


def _is_heading(r: Row, geo: PageGeometry) -> bool:
    if r.x0 > geo.left - 5:
        return False
    # (font is not required: OCR font assignment for headings is inconsistent across pages)
    # starts left of the body margin, so it may be long ("6.7 • THE PENROSE PROCESS AND BLACK-HOLE THERMODYNAMICS"
    # is ~1.2x the column width); only a row running past the right margin is not a heading
    return bool(SECTION_HEAD_RE.match(r.text)) and r.x1 <= geo.right + 12


def _is_body(r: Row, geo: PageGeometry, in_exercises: bool) -> bool:
    indent_max = geo.left + (22 if in_exercises else 17)
    if not (geo.left - 4 <= r.x0 <= indent_max):
        return False
    width = r.x1 - r.x0
    if width < 25 and not re.search(r"[a-z]{3}", r.text):
        return False
    if EQ_TAG_RE.match(r.text.split()[-1] if r.text.split() else "") and width < 60:
        return False
    return True


def _dehyphenate_join(texts: list[str]) -> str:
    out = ""
    for t in texts:
        t = re.sub(r"\s+", " ", t).strip()
        if not t:
            continue
        if out.endswith("-") and t[:1].islower() and not out.endswith(" -"):
            out = out[:-1] + t
        else:
            out = (out + " " + t) if out else t
    return out


def segment_page(rows: list[Row], page: int, page_width: float, page_height: float,
                 in_exercises: bool = False) -> list[PageBlock]:
    geo = estimate_geometry(rows, page_height)
    content = [r for r in rows
               if r.y0 >= geo.header_bottom - 0.5 and r.y1 <= geo.footer_top + 0.5
               and r.median_size < 1.6 * geo.body_size]
    # drop the running head itself
    content = [r for r in content if not (r.y1 <= geo.header_bottom + 0.5)]

    blocks: list[PageBlock] = []
    pending_other: list[Row] = []
    para_rows: list[Row] = []
    caption_rows: list[Row] = []
    prev_bottom = geo.header_bottom if geo.header_bottom else max(0.0, min((r.y0 for r in content), default=0) - 4)

    def flush_para():
        nonlocal para_rows
        if not para_rows:
            return
        text = _dehyphenate_join([r.text for r in para_rows])
        first = para_rows[0]
        starts = first.x0 > geo.left + 6 or bool(in_exercises and EXERCISE_ITEM_RE.match(first.text))
        kind = "paragraph"
        ex_num = None
        if in_exercises:
            m = EXERCISE_ITEM_RE.match(first.text)
            if m and first.x0 < geo.left + 6:
                kind, ex_num = "exercise", m.group(1)
        blocks.append(PageBlock(
            page, kind,
            (min(r.x0 for r in para_rows), min(r.y0 for r in para_rows),
             max(r.x1 for r in para_rows), max(r.y1 for r in para_rows)),
            text, math_score([s for r in para_rows for s in r.spans], geo.body_size),
            starts_paragraph=starts, exercise_number=ex_num,
        ))
        para_rows = []

    def flush_caption():
        nonlocal caption_rows
        if caption_rows:
            blocks.append(PageBlock(page, "caption",
                                    (min(r.x0 for r in caption_rows), min(r.y0 for r in caption_rows),
                                     max(r.x1 for r in caption_rows), max(r.y1 for r in caption_rows)),
                                    _dehyphenate_join([r.text for r in caption_rows])))
            caption_rows = []

    def flush_other(next_top: float, before_caption: bool, next_is_heading: bool = False):
        """Turn a run of non-body rows (or an empty vertical gap) into equation/figure blocks."""
        nonlocal pending_other
        top, bottom = prev_bottom, next_top
        height = bottom - top
        rows_here = pending_other
        pending_other = []
        if height < 1.45 * geo.line_pitch and not rows_here:
            return
        tags = []
        for r in rows_here:
            for ln in r.lines:
                m = EQ_TAG_RE.match(ln.text.strip())
                if m and ln.x0 > geo.left + 0.55 * (geo.right - geo.left):
                    tags.append((ln.yc, f"{_fix_digits(m.group(1))}.{_fix_digits(m.group(2))}"))
        x0, x1 = geo.left - 6, geo.right + 4
        if before_caption or (height > 7 * geo.line_pitch and not tags):
            blocks.append(PageBlock(page, "figure", (x0 - 40, top + 0.5, x1, bottom - 0.5),
                                    " | ".join(r.text for r in rows_here)))
            return
        if not tags:
            if not rows_here and (height < 2.2 * geo.line_pitch or next_is_heading):
                return  # ordinary paragraph / section spacing, nothing there
            blocks.append(PageBlock(page, "equation", (x0, top + 0.5, x1, bottom - 0.5),
                                    " ".join(r.text for r in rows_here), math_score(
                                        [s for r in rows_here for s in r.spans], geo.body_size) + 3,
                                    flags=["untagged_display"] if rows_here else ["empty_gap_visual"]))
            return
        tags.sort()
        cuts = [top] + [(a[0] + b[0]) / 2 for a, b in zip(tags, tags[1:])] + [bottom]
        for i, (_, num) in enumerate(tags):
            y0, y1 = cuts[i], cuts[i + 1]
            text = " ".join(r.text for r in rows_here if y0 <= (r.y0 + r.y1) / 2 < y1)
            spans = [s for r in rows_here if y0 <= (r.y0 + r.y1) / 2 < y1 for s in r.spans]
            blocks.append(PageBlock(page, "equation", (x0, y0 + 0.5, x1, y1 - 0.5), text,
                                    math_score(spans, geo.body_size) + 3, eq_number=num))

    for r in content:
        if _is_heading(r, geo):
            flush_para(); flush_caption()
            flush_other(r.y0, False, next_is_heading=True)
            m = SECTION_HEAD_RE.match(r.text)
            in_exercises = bool(re.search(r"exercises|problems", m.group(3), re.I))
            blocks.append(PageBlock(page, "heading", (r.x0, r.y0, r.x1, r.y1), m.group(3).strip(),
                                    heading_number=f"{m.group(1)}.{m.group(2)}"))
            prev_bottom = r.y1
            continue
        if FIGURE_RE.match(r.text) or (caption_rows and r.y0 - caption_rows[-1].y1 < 0.3 * geo.line_pitch
                                        and _is_body(r, geo, True)):
            flush_para()
            if not caption_rows:
                flush_other(r.y0, True)
            caption_rows.append(r)
            prev_bottom = r.y1
            continue
        flush_caption()
        if _is_body(r, geo, in_exercises):
            gap = r.y0 - prev_bottom
            if pending_other or gap > 1.45 * geo.line_pitch:
                flush_para()
                flush_other(r.y0, False)
            new_para = (r.x0 > geo.left + 6 and not in_exercises) or (
                in_exercises and EXERCISE_ITEM_RE.match(r.text) and r.x0 < geo.left + 6)
            if new_para:
                flush_para()
            para_rows.append(r)
            prev_bottom = r.y1
        else:
            if para_rows:
                flush_para()
            pending_other.append(r)
    flush_para(); flush_caption()
    if pending_other:
        flush_other(max(r.y1 for r in pending_other) + 2, False)
    return blocks
