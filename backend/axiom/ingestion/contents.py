"""Recover a printed, numbered table of contents when PDF bookmarks are missing or flat."""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Entry:
    number: str
    title: str
    printed_page: int


_HEAD = re.compile(r"^(?:table\s+of\s+)?contents\s*$", re.I)
_ENTRY = re.compile(r"^(?:(?:chapter|lecture|part)\s+)?(\d+(?:\s*\.\s*\d+)*)(?:[.:])?\s+(\S.*?)\s*$", re.I)
_PAGE = re.compile(r"(?:\s*(?:[.…·]\s*)+|\s+)(\d+)\s*$")
_END = re.compile(r"^(?:references|bibliography|index)\b", re.I)


def _key(title: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", title).casefold() if c.isalnum())


def _entries(lines):
    entries = []
    trailing = None
    for line in lines:
        text = line.strip()
        if re.fullmatch(r"\d+", text) and trailing:
            text = f"{trailing}  {text}"
        trailing = None
        page = _PAGE.search(text)
        if not page:
            if _ENTRY.match(text):
                trailing = text
            continue
        label = text[:page.start()].strip().rstrip(". …·")
        match = _ENTRY.match(label)
        if match:
            number, title = re.sub(r"\s+", "", match[1]), match[2]
            if len(_key(title)) >= 3 and number.count(".") <= 1 and int(page[1]) > 0:
                entries.append(Entry(number, title, int(page[1])))
        elif _END.match(label) and entries:
            entries.append(Entry("", label, int(page[1])))
            break
    return entries


def printed_contents(pdf):
    """Return numbered entries with 1-based PDF pages and optional y anchors, or no reliable result."""
    for index in range(min(pdf.page_count, 16)):
        lines = pdf[index].get_text("text", sort=True).splitlines()
        head = next((i for i, line in enumerate(lines) if _HEAD.fullmatch(line.strip())), None)
        if head is None:
            continue
        entries = _entries(lines[head + 1:])
        if not entries:
            continue
        last_contents = index + 1
        for extra in range(index + 1, min(index + 5, pdf.page_count)):
            if not entries[-1].number:
                break
            more = _entries(pdf[extra].get_text("text", sort=True).splitlines())
            if not more or more[0].printed_page < entries[-1].printed_page:
                break
            # A continuation must look like a contents page, not the first body heading.
            if len(more) < 2 and more[0].number:
                break
            entries.extend(more)
            last_contents = extra + 1
        numbered = list(dict.fromkeys(e.number for e in entries if e.number))
        if len(numbered) < 2:
            continue
        found = _locate(pdf, entries, last_contents)
        if found:
            return found
    return []


def _locate(pdf, entries: list[Entry], last_contents: int):
    @lru_cache(maxsize=160)
    def headings(page):
        p = pdf[page - 1]
        out = []
        for block in p.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                text = "".join(s["text"] for s in line["spans"]).strip()
                m = _ENTRY.match(text)
                y = line["bbox"][1]
                if y < p.rect.height * .94:
                    out.append((_key(text), None, y))
                    if m:
                        out.append((_key(m[2]), re.sub(r"\s+", "", m[1]), y))
                    elif len(out) >= 2:
                        # A number on its own line just above a title is also common in books.
                        prev, _, py = out[-2]
                        if prev.isdecimal() and 0 < y - py < 35:
                            out.append((_key(text), prev, py))
        return out

    def anchor(entry, page):
        if not last_contents < page <= pdf.page_count:
            return None
        title = _key(entry.title)
        hits = [(num != entry.number, y) for key, num, y in headings(page)
                if key == title and (num is None or num == entry.number)]
        return max(0, min(hits)[1] - 1) if hits else None

    evidence = Counter()
    for e in [e for e in entries if e.number][:12]:
        candidates = set(pdf.get_page_numbers(str(e.printed_page)))
        candidates = {p + 1 for p in candidates}
        candidates.update(range(max(last_contents + 1, e.printed_page - 3),
                                min(pdf.page_count, e.printed_page + last_contents + 20) + 1))
        for page in candidates:
            if anchor(e, page) is not None:
                evidence[page - e.printed_page] += 1
    if not evidence:
        return []
    ranked = evidence.most_common()
    offset, matches = ranked[0]
    if matches < 2 or (len(ranked) > 1 and ranked[1][1] == matches):
        return []
    out, seen = [], set()
    for e in entries:
        page = e.printed_page + offset
        if not last_contents < page <= pdf.page_count or e.number in seen:
            continue
        if out and page < out[-1][1]:
            return []
        seen.add(e.number)
        out.append((e, page, anchor(e, page)))
    return out
