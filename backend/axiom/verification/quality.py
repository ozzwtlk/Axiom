"""Quality control: cheap deterministic checks after every stage. They fix what needs no model and name exactly what
still needs one, so a re-run only redoes those parts (everything else comes from the cache).

Blocks (after transcription, before the optimizer sees the text):
  * page numbers ("- 4 -", "12")                          → block becomes 'artifact' (coverage: omitted, with reason)
  * text cut out as an image (a caption, a line of prose)  → caption / paragraph block, read again
  * a line caught twice (an equation crop that includes the next line, a paragraph repeated in the next block)
                                                           → the copy is removed
Units (after optimize, after verify, or on demand):
  * LaTeX that KaTeX cannot render → deterministic repair (\\eqno, upgreek, \\slashed, letter-spaced words in math,
    unbalanced braces); if that fails: an optional AI field is dropped, an AI rewrite reverts to the source text,
    a transcription is queued to be read again
  * LaTeX outside math ("g_{\\mu\\nu}", "\\textbf{x}", a bare \\begin{aligned}) → wrapped / turned into Markdown
  * repeated sentences / loops → repeats removed; a unit repeating its neighbour → merged into it
Open issues (they keep the section 'partial' and are what a re-run fixes): passages left verbatim because the
optimizer failed, transcriptions that still do not parse, unchecked faithfulness batches, coverage gaps.
The checks never write new prose (invariant 8): they only delete, revert to source or repair LaTeX syntax.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone

from axiom.storage import db
from axiom.verification import latex

log = logging.getLogger("axiom.quality")

FALLBACK_NOTE = "Optimization failed validation"
TEXT_TYPES = ("paragraph", "equation", "caption", "exercise")
FOLIO_RE = re.compile(r"(?:[-–—]\s*)?(?:page\s+)?\d{1,4}(?:\s*[-–—])?", re.I)
CAPTION_START = re.compile(r"\s*(?:FIGURE|Figure|Fig\.)\s*\d+(?:\.\d+)*[.:]?\s+[A-Z(]")  # not "Figure 6.9, such that"
_UPGREEK = ("alpha|beta|gamma|delta|epsilon|varepsilon|zeta|eta|theta|vartheta|iota|kappa|lambda|mu|nu|xi|pi|varpi|"
            "rho|varrho|sigma|varsigma|tau|upsilon|phi|varphi|chi|psi|omega|Gamma|Delta|Theta|Lambda|Xi|Pi|Sigma|"
            "Upsilon|Phi|Psi|Omega")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(b) -> str:
    return b["transcribed_md"] if b["transcribed_md"] is not None else b["raw_text"]


def _words(md: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", latex.strip_math(md or "").lower())


def _plain(t: str) -> str:
    t = re.sub(r"\\[ ,;:!~]|\\q?quad\b|\\(?:mathrm|text|textrm)\b|[\${}~]", " ", t or "")
    return re.sub(r"\s+", " ", t).strip()


def is_folio(b) -> bool:
    """A block holding only a page number."""
    if b["block_type"] not in ("paragraph", "equation", "caption") or b["eq_number"]:
        return False
    t = _plain(_text(b))
    return bool(t) and bool(FOLIO_RE.fullmatch(t))


def _similar(a: list[str], b: list[str]) -> bool:
    return bool(a) and difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.9


# ---------------------------------------------------------------------------------------------------------- LaTeX
def _spaced_text(inner: str) -> str | None:
    """'C a m b r i d g e ~ U n i v' (OCR'd words typeset letter by letter) → 'Cambridge Univ', else None."""
    toks = inner.split()
    if len(toks) < 4 or not all(len(t) == 1 or t in ("\\", "~", "\\,") for t in toks):
        return None
    if sum(t.isalpha() for t in toks) < 0.6 * len(toks) or re.search(r"[_^&%#{}]", inner):
        return None
    word = "".join(" " if t in ("\\", "~", "\\,") else t for t in toks)
    return re.sub(r"\s+", " ", word).strip()


def _spaced_to_text(s: str) -> str:
    def sub(m):
        w = _spaced_text(m.group(1))
        return f"\\text{{{w}}}" if w else m.group(0)
    return re.sub(r"\\(?:mathrm|textrm|text)\s*\{([^{}]*)\}", sub, s)


def repair_tex(s: str) -> str:
    """Deterministic syntax repair of one math segment (never changes what the mathematics says)."""
    s = re.sub(r"\\eqno\b.*$", "", s, flags=re.S).rstrip()
    s = re.sub(r"\\leqno\b.*$", "", s, flags=re.S).rstrip()
    s = re.sub(r"\\up(" + _UPGREEK + r")(?![A-Za-z])", r"\\\1", s)
    s = re.sub(r"\\slashed\s*\{([^{}]*)\}", r"\\not{\1}", s)
    s = re.sub(r"\\slashed\s*(\\[A-Za-z]+|[A-Za-z])", r"\\not{\1}", s)
    s = re.sub(r"\\(?:Bigg|bigg|Big|big)[lrm]?\s*(?=\\(?:sim|approx|simeq|equiv)\b)", "", s)
    s = _spaced_to_text(s)
    for env in re.findall(r"\\begin\{(\w+\*?)\}", s):
        if s.count(f"\\begin{{{env}}}") > s.count(f"\\end{{{env}}}"):
            s += f" \\end{{{env}}}"
    depth = 0
    for ch in re.sub(r"\\[{}]", "", s):
        depth += (ch == "{") - (ch == "}")
        if depth < 0:
            break
    if depth > 0:
        s += "}" * depth
    return s


def _md_outside_math(md: str, fn) -> str:
    """Apply `fn` to the parts of `md` that are not math."""
    out, last = [], 0
    for m in latex.MATH_RE.finditer(md):
        out.append(fn(md[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(fn(md[last:]))
    return "".join(out)


# a token of LaTeX outside math: "g_{\mu\nu}U^\mu", "\omega", "a^{-3}" (ends before trailing punctuation)
_STRAY = re.compile(r"(?<!\S)[^\s$*]*(?:\\[A-Za-z]+|[_^])[^\s$*]*")


def _stray_token(tok: str) -> tuple[str, str]:
    """('h_k^(1)', ',') from 'h_k^(1),': trailing punctuation stays outside, a closing paren only if unmatched."""
    tail = ""
    while tok and (tok[-1] in ",.;:" or (tok[-1] == ")" and tok.count(")") > tok.count("("))):
        tail, tok = tok[-1] + tail, tok[:-1]
    return tok, tail


def normalize_md(md: str) -> str:
    """LaTeX that leaked outside math → Markdown or inline math; displays that are only letter-spaced prose → prose."""
    if not md:
        return md

    def prose_display(m):
        inner = m.group(1) or ""
        bits = re.findall(r"\\(?:mathrm|textrm|text)\s*\{([^{}]*)\}", inner)
        rest = re.sub(r"\\(?:mathrm|textrm|text)\s*\{[^{}]*\}|[\s.,;:?!~]|\\[ ,]", "", inner)
        words = [_spaced_text(b) or (b if not b.strip() else None) for b in bits]
        if bits and not rest and all(w is not None for w in words):
            tail = re.search(r"([.,;:?!])\s*$", inner)
            return " ".join(w for w in words if w).strip() + (tail.group(1) if tail else "")
        return m.group(0)
    md = re.sub(r"\$\$(.+?)\$\$", prose_display, md, flags=re.S)

    def fix(t: str) -> str:
        t = re.sub(r"\\textbf\{([^{}]*)\}", r"**\1**", t)
        t = re.sub(r"\\(?:textit|emph)\{([^{}]*)\}", r"*\1*", t)
        t = re.sub(r"\\(?:ldots|dots)\b", "…", t)
        return re.sub(r"(\\begin\{(\w+\*?)\}.*?\\end\{\2\})", r"$$\1$$", t, flags=re.S)
    return _md_outside_math(md, fix)


def stray_candidates(md: str) -> list[str]:
    """Tokens of LaTeX written outside math (wrapped in $...$ only if KaTeX accepts them)."""
    out: list[str] = []
    _md_outside_math(md or "", lambda t: out.extend(tok for m in _STRAY.finditer(t)
                                                      for tok in [_stray_token(m.group(0))[0]]
                                                      if tok and not re.search(r"https?:|www\.|//", tok)) or t)
    return out


def wrap_stray(md: str, ok: set[str]) -> str:
    def sub(m):
        tok, tail = _stray_token(m.group(0))
        return f"${tok}${tail}" if tok in ok else m.group(0)
    return _md_outside_math(md, lambda t: _STRAY.sub(sub, t))


_END = re.compile(r"[.!?:;)\]}$*`'\"’”…]\s*$")


def complete_sentences(md: str) -> str:
    """Cut a text the model stopped mid-sentence back to its last complete sentence ('' if none is left)."""
    if not md or _END.search(md):
        return md
    cut = max(md.rfind(". "), md.rfind(".\n"), md.rfind("? "), md.rfind("! "), md.rfind("$$\n"))
    if cut < 0:
        return ""
    keep = md[:cut + (2 if md[cut:cut + 2] == "$$" else 1)].rstrip()
    return keep if len(keep) >= 40 and keep.count("$") % 2 == 0 else ""


def _collapse_loops(md: str) -> str:
    """'a b c a b c a b c' → 'a b c' (a run of 3+ identical word sequences; models and OCR both do this)."""
    w = md.split()
    i, out, hit = 0, [], False
    while i < len(w):
        for n in range(2, 16):
            if i + 3 * n <= len(w) and w[i:i + n] == w[i + n:i + 2 * n] == w[i + 2 * n:i + 3 * n] \
                    and len(" ".join(w[i:i + n])) >= 8:
                k = i + n
                while w[k:k + n] == w[i:i + n]:
                    k += n
                out += w[i:i + n]
                i, hit = k, True
                break
        else:
            out.append(w[i])
            i += 1
    return " ".join(out) if hit else md


def _dedupe(md: str) -> str:
    """Remove repeated sentences and loops ("... the field ... the field ... the field")."""
    if not md:
        return md
    md = _collapse_loops(md)
    parts = re.split(r"(?<=[.!?])(\s+)", md)
    seen: set[str] = set()
    out = []
    for i in range(0, len(parts), 2):
        s = parts[i]
        key = re.sub(r"\s+", " ", s).strip().lower()
        if len(key) > 30 and key in seen:
            continue
        seen.add(key)
        out.append(s + (parts[i + 1] if i + 1 < len(parts) else ""))
    return "".join(out).strip()


# --------------------------------------------------------------------------------------------------------- blocks
def _text_in_region(pdf, b) -> str | None:
    """The PDF text inside a figure region if the region is essentially text (lines cover most of its height and
    width); None for a real picture (no text, or a few labels)."""
    import pymupdf
    page = pdf[b["page"] - 1]
    clip = pymupdf.Rect(b["x0"], b["y0"], b["x1"], b["y1"])
    if clip.is_empty or clip.height < 4:
        return None
    lines: dict[tuple, list] = {}
    for w in page.get_text("words", clip=clip):
        lines.setdefault((w[5], w[6]), []).append(w)
    if not lines:
        return None
    rows = sorted((min(w[1] for w in ws), max(w[3] for w in ws), min(w[0] for w in ws), max(w[2] for w in ws),
                   " ".join(w[4] for w in sorted(ws, key=lambda w: w[0]))) for ws in lines.values())
    covered, top = 0.0, clip.y0
    for y0, y1, *_ in rows:
        y0, y1 = max(y0, top, clip.y0), min(y1, clip.y1)
        if y1 > y0:
            covered += y1 - y0
            top = y1
    widths = [x1 - x0 for _, _, x0, x1, _ in rows]
    text = " ".join(r[4] for r in rows)
    if (covered >= 0.55 * clip.height and sum(widths) / len(widths) >= 0.5 * clip.width
            and len(re.findall(r"[A-Za-z]{3,}", text)) >= max(1, len(rows))):
        return re.sub(r"(\w)- (\w)", r"\1\2", text)
    return None


def clean_blocks(section_id: str, pdf=None) -> dict:
    """Block-level fixes. Returns {"fixed": {kind: n}, "retranscribe": [block ids]} — blocks to be read again."""
    blocks = [dict(b) for b in db.q("SELECT * FROM blocks WHERE section_id=? ORDER BY ord", (section_id,))]
    fixed: Counter = Counter()
    upd: dict[str, dict] = {}

    def artifact(b, why: str, kind: str):
        flags = json.loads(b["flags"] or "[]") + [{"kind": "info", "message": why}]
        upd.setdefault(b["id"], {}).update(block_type="artifact", flags=json.dumps(flags))
        b["block_type"] = "artifact"
        fixed[kind] += 1

    for b in blocks:
        if is_folio(b):
            artifact(b, "Page number (not part of the text)", "page_number")

    figs = [b for b in blocks if b["block_type"] == "figure"]
    if figs and pdf is None:
        try:
            from axiom.extraction.extract import open_pdf
            pdf = open_pdf(blocks[0]["doc_id"])
        except Exception as e:  # no PDF here: skip this check, the others still run
            log.info("quality: cannot open the PDF for %s: %s", section_id, e)
    retranscribe: list[str] = []
    for i, b in enumerate(figs if pdf is not None else []):
        txt = _text_in_region(pdf, b)
        if not txt:
            continue
        kind = "caption" if CAPTION_START.match(txt) else "paragraph"
        near = [x for x in blocks if x["block_type"] == "caption" and abs(x["ord"] - b["ord"]) <= 2]
        if kind == "caption" and any(_similar(_words(txt), _words(_text(x))) for x in near):
            artifact(b, "Image of a caption that is already shown as text", "text_shown_as_image")
            continue
        upd.setdefault(b["id"], {}).update(block_type=kind, raw_text=txt, transcribed_md=None,
                                           transcription_method="none")
        b.update(block_type=kind, raw_text=txt, transcribed_md=None, transcription_method="none")
        retranscribe.append(b["id"])
        fixed["text_shown_as_image"] += 1

    # a line caught twice: compare neighbours on the same page (transcribed text only)
    live = [b for b in blocks if b["block_type"] in TEXT_TYPES and b["transcription_method"] != "none"]
    for a, c in zip(live, live[1:]):
        if a["block_type"] == "artifact" or c["block_type"] == "artifact" or a["page"] != c["page"]:
            continue
        ta, tc = _text(a), _text(c)
        if a["block_type"] == "equation" and "$$" in ta:
            tail = ta[ta.rfind("$$") + 2:]
            wt = _words(tail)
            if len(wt) >= 4 and _similar(wt, _words(tc)[:len(wt)]):
                ta = ta[:ta.rfind("$$") + 2]
                upd.setdefault(a["id"], {})["transcribed_md"] = ta
                a["transcribed_md"] = ta
                fixed["repeated_text"] += 1
                continue
        if c["block_type"] == "equation" and "$$" in tc:
            lead = tc[:tc.find("$$")]
            wl = _words(lead)
            if len(wl) >= 4 and _similar(wl, _words(ta)[-len(wl):]):
                tc = tc[tc.find("$$"):]
                upd.setdefault(c["id"], {})["transcribed_md"] = tc
                c["transcribed_md"] = tc
                fixed["repeated_text"] += 1
                continue
        if a["block_type"] == c["block_type"] == "paragraph":
            wa, wc = _words(ta), _words(tc)
            if len(wa) >= 6 and len(wa) <= len(wc) and _similar(wa, wc[:len(wa)]):
                artifact(a, "Repeated at the start of the next passage", "repeated_text")
            elif len(wc) >= 6 and len(wc) < len(wa) and _similar(wc, wa[-len(wc):]):
                artifact(c, "Repeated at the end of the previous passage", "repeated_text")

    if upd:
        with db.connect() as conn:
            for bid, cols in upd.items():
                conn.execute(f"UPDATE blocks SET {', '.join(k + '=?' for k in cols)} WHERE id=?",
                             (*cols.values(), bid))
            row = conn.execute("SELECT value FROM meta WHERE key=?", (f"quality:{section_id}",)).fetchone()
            rep = json.loads(row["value"]) if row else {"fixed": {}, "issues": [], "needs": []}
            rep["fixed_total"] = dict(Counter(rep.get("fixed_total", {})) + fixed)
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (f"quality:{section_id}", json.dumps(rep)))
    return {"fixed": dict(fixed), "retranscribe": retranscribe}


# ---------------------------------------------------------------------------------------------------------- units
def _source_text(u: dict, blocks: dict) -> str:
    return "\n\n".join(_text(blocks[b]) for b in u["block_ids"]
                       if b in blocks and blocks[b]["block_type"] in ("paragraph", "caption", "exercise"))


def _fix_math(units: list[dict], blocks: dict, fixed: Counter, bad_blocks: set[str]) -> None:
    fields = ("text_md", "fast_md", "deep_md", "annotation_md")
    ai_fields = []  # (unit, field) written by the AI: repeats removed, stray LaTeX wrapped
    for u in units:
        for f in fields:
            if not u.get(f):
                continue
            new = normalize_md(u[f])
            if new != u[f]:
                fixed["latex_repaired"] += 1
            if u.get("ai_generated") or f != "text_md":
                ai_fields.append((u, f))
                d = _dedupe(new)
                if d != new:
                    fixed["repeated_text"] += 1
                new = d
                if f != "text_md":  # optional explanations the model stopped mid-sentence: cut to the last sentence
                    c = complete_sentences(new)
                    if c != new:
                        fixed["cut_off_text"] += 1
                        new = c or None
            u[f] = new
    ai_fields = [(u, f) for u, f in ai_fields if u.get(f)]
    cands = sorted({t for u, f in ai_fields for t in stray_candidates(u[f])})
    if cands and latex.available():
        ok = {t for t, e in zip(cands, latex.check([(t, False) for t in cands])) if not e}
        for u, f in ai_fields:
            new = wrap_stray(u[f], ok)
            if new != u[f]:
                fixed["latex_repaired"] += 1
                u[f] = new
    # one KaTeX run for the whole section
    items, where = [], []
    for i, u in enumerate(units):
        if u.get("latex"):
            items.append((u["latex"], True))
            where.append((i, "latex", None, u["latex"]))
        for f in fields:
            for seg, disp in latex.extract_math(u.get(f) or ""):
                items.append((seg, disp))
                where.append((i, f, None, seg))
        for k, n in enumerate(u.get("notes") or []):
            for seg, disp in latex.extract_math(n.get("md") or ""):
                items.append((seg, disp))
                where.append((i, "notes", k, seg))
    if not latex.available():
        return
    errs = latex.check(items)
    bad = [(w, it) for w, it, e in zip(where, items, errs) if e]
    if not bad:
        return
    repaired = [repair_tex(it[0]) for _, it in bad]
    errs2 = latex.check([(r, it[1]) for r, (_, it) in zip(repaired, bad)])
    broken: dict[tuple, bool] = {}
    for (w, it), r, e in zip(bad, repaired, errs2):
        i, f, k, seg = w
        u = units[i]
        if not e:
            fixed["latex_repaired"] += 1
            if f == "latex":
                u["latex"] = r
                _repair_block(blocks, u, seg, r)
            elif f == "notes":
                u["notes"][k]["md"] = u["notes"][k]["md"].replace(seg, r, 1)
            else:
                u[f] = u[f].replace(seg, r, 1)
                if f == "text_md" and not u.get("ai_generated"):
                    _repair_block(blocks, u, seg, r)
        else:
            broken[(i, f, k)] = True
    notes_drop: dict[int, set[int]] = {}
    for (i, f, k) in broken:
        u = units[i]
        if f == "notes":
            notes_drop.setdefault(i, set()).add(k)
        elif f in ("fast_md", "deep_md", "annotation_md"):
            u[f] = None
            fixed["latex_dropped"] += 1
            u.setdefault("flags", []).append({"kind": "info", "message": "An AI explanation was removed because its"
                                              " LaTeX does not render."})
        elif f == "text_md" and u.get("ai_generated") and _source_text(u, blocks):
            u.update(text_md=_source_text(u, blocks), fast_md=None, deep_md=None, notes=[], coverage="preserved",
                     ai_generated=False)
            u.setdefault("flags", []).append({"kind": "reverted", "message": "The rewrite had LaTeX that does not"
                                              " render; showing the original text."})
            fixed["latex_reverted"] += 1
        else:  # a transcription: read it again
            bad_blocks.update(b for b in u["block_ids"] if b in blocks and blocks[b]["block_type"] != "artifact")
    for i, ks in notes_drop.items():
        units[i]["notes"] = [n for k, n in enumerate(units[i]["notes"]) if k not in ks]
        fixed["latex_dropped"] += len(ks)


def _fix_cut_off(units: list[dict], blocks: dict, fixed: Counter) -> None:
    """An AI rewrite that stops mid-sentence (and is not the lead-in to an equation) reverts to the source text."""
    for i, u in enumerate(units):  # an AI rewrite that stops mid-sentence (not a lead-in to an equation): source
        nxt = units[i + 1] if i + 1 < len(units) else None
        t = u.get("text_md") or ""
        if (u.get("ai_generated") and t and not _END.search(t) and not (nxt and nxt["kind"] == "equation")
                and _END.search(_source_text(u, blocks) or "")):
            u.update(text_md=_source_text(u, blocks), fast_md=None, deep_md=None, notes=[], coverage="preserved",
                     ai_generated=False)
            u.setdefault("flags", []).append({"kind": "reverted", "message": "The rewrite was cut off; showing the"
                                              " original text."})
            fixed["cut_off_text"] += 1


def _repair_block(blocks: dict, u: dict, old: str, new: str) -> None:
    for bid in u["block_ids"]:
        b = blocks.get(bid)
        if b and b["transcribed_md"] and old in b["transcribed_md"]:
            b["transcribed_md"] = b["transcribed_md"].replace(old, new, 1)
            b["_dirty"] = True


def _merge_repeats(units: list[dict], fixed: Counter) -> list[dict]:
    """A unit whose text repeats its neighbour's (the same passage caught twice) is merged into the neighbour;
    an equation unit only loses its repeated lead-in words."""
    def grams(u):
        w = _words(u.get("text_md"))
        return {tuple(w[i:i + 5]) for i in range(len(w) - 4)}

    def absorb(into: dict, gone: dict) -> None:
        into["block_ids"] = list(dict.fromkeys(gone["block_ids"] + into["block_ids"]))
        into["_absorbed"] = into.get("_absorbed", []) + [gone["id"]] + gone.get("_absorbed", [])

    out: list[dict] = []
    for u in units:
        prev = out[-1] if out else None
        if prev and prev["kind"] not in ("heading", "figure") and u["kind"] not in ("heading", "figure"):
            gp, gu = grams(prev), grams(u)
            if len(gp) >= 4 and len(gu) >= 4:
                common = len(gp & gu)
                if common >= 0.9 * len(gp) and len(gp) <= len(gu):  # prev's text is repeated in u
                    fixed["repeated_text"] += 1
                    if prev["kind"] == "equation":
                        prev["text_md"] = ""
                    else:
                        absorb(u, out.pop())
                elif common >= 0.9 * len(gu):  # u's text is repeated in prev
                    fixed["repeated_text"] += 1
                    if u["kind"] == "equation":
                        u["text_md"] = ""
                    else:
                        absorb(prev, u)
                        continue
        out.append(u)
    return out


def fix_units(section_id: str, faithfulness: dict | None = None) -> dict:
    """Unit-level fixes, then the list of open issues. `faithfulness`: the verify stats of this run (None: read the
    stored ones if the section was verified). Writes units, coverage, search rows and meta 'quality:<section>'."""
    from axiom.transformations.optimize import _display_latex
    sec = db.q1("SELECT * FROM sections WHERE id=?", (section_id,))
    blocks = {b["id"]: dict(b) for b in db.q("SELECT * FROM blocks WHERE section_id=?", (section_id,))}
    rows = db.q("SELECT id, data FROM units WHERE section_id=? ORDER BY ord", (section_id,))
    units = [dict(json.loads(r["data"]), id=r["id"]) for r in rows]
    fixed: Counter = Counter()
    bad_blocks: set[str] = set()

    kept = []
    for u in units:
        live = [b for b in u["block_ids"] if blocks.get(b, {}).get("block_type") != "artifact"]
        if not live and u["block_ids"]:  # only page numbers / repeats (counted when the block was marked)
            continue
        u["block_ids"] = live or u["block_ids"]
        if not u.get("ai_generated") and u["kind"] == "equation" and len(live) == 1:
            disp, rest = _display_latex(_text(blocks[live[0]]))
            if disp and (u.get("latex"), u.get("text_md") or "") != (disp, rest):
                u["latex"], u["text_md"] = disp, rest
        if u["kind"] == "figure" and u.get("figure_block") and blocks.get(u["figure_block"], {}).get("block_type") in TEXT_TYPES:
            b = blocks[u["figure_block"]]
            u.pop("figure_block")
            u.update(kind="prose", text_md=_text(b), coverage="preserved", ai_generated=False,
                     note="Text that had been cut out as an image")
        kept.append(u)
    units = _merge_repeats(kept, fixed)
    _fix_math(units, blocks, fixed, bad_blocks)
    _fix_cut_off(units, blocks, fixed)
    for u in units:  # loops in a transcription: read again
        if not u.get("ai_generated") and u.get("text_md") and _dedupe(u["text_md"]) != u["text_md"].strip():
            bad_blocks.update(b for b in u["block_ids"] if b in blocks)

    # open issues
    issues: list[dict] = []
    fallback = [u for u in units if FALLBACK_NOTE in (u.get("note") or "")]
    if fallback:
        issues.append({"kind": "not_optimized", "needs": "optimize", "units": [u["id"] for u in fallback],
                       "message": f"{len(fallback)} passage(s) shown verbatim because the AI rewrite failed"})
    if bad_blocks:
        issues.append({"kind": "bad_transcription", "needs": "transcribe", "blocks": sorted(bad_blocks),
                       "units": [u["id"] for u in units if set(u["block_ids"]) & bad_blocks],
                       "message": f"{len(bad_blocks)} transcription(s) with LaTeX that does not render"})
    if faithfulness is None and sec["stage"] == "verified":
        row = db.q1("SELECT value FROM meta WHERE key=?", (f"faithfulness:{section_id}",))
        faithfulness = json.loads(row["value"]) if row else None
    if faithfulness and faithfulness.get("failed_batches"):
        issues.append({"kind": "unchecked", "needs": "verify",
                       "message": f"{faithfulness['failed_batches']} faithfulness batch(es) could not be checked"})
    if not blocks:
        issues.append({"kind": "empty", "needs": "extract", "message": "No text was extracted for this section"})

    _write(sec, units, blocks)
    from axiom.verification.coverage import audit_section
    if not audit_section(section_id)["ok"]:
        issues.append({"kind": "coverage", "needs": "optimize", "message": "Some source passages are not shown"})
    prev = db.q1("SELECT value FROM meta WHERE key=?", (f"quality:{section_id}",))
    total = Counter(json.loads(prev["value"]).get("fixed_total", {})) if prev else Counter()
    total.update(fixed)
    report = {"fixed": dict(fixed), "fixed_total": dict(total), "issues": issues,
              "needs": sorted({i["needs"] for i in issues}), "checked_at": _now()}
    with db.connect() as c:
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (f"quality:{section_id}", json.dumps(report)))
    if fixed:
        log.info("quality %s: fixed %s; open: %s", section_id, dict(fixed), [i["kind"] for i in issues])
    return report


def _write(sec, units: list[dict], blocks: dict) -> None:
    section_id, doc_id = sec["id"], sec["doc_id"]
    with db.connect() as c:
        for b in blocks.values():
            if b.pop("_dirty", False):
                c.execute("UPDATE blocks SET transcribed_md=? WHERE id=?", (b["transcribed_md"], b["id"]))
        old = [r["id"] for r in c.execute("SELECT id FROM units WHERE section_id=?", (section_id,))]
        keep = {u["id"] for u in units}
        for u in units:  # a merged unit takes over its partner's picture if it has none
            for a in u.pop("_absorbed", []):
                if not c.execute("SELECT 1 FROM visualizations WHERE unit_id=?", (u["id"],)).fetchone():
                    c.execute("UPDATE visualizations SET unit_id=? WHERE unit_id=?", (u["id"], a))
        for uid in old:
            if uid not in keep:
                c.execute("DELETE FROM units WHERE id=?", (uid,))
                c.execute("UPDATE visualizations SET unit_id=? WHERE unit_id=?",
                          (f"detached:{section_id}:{uid}", uid))
        c.execute("DELETE FROM coverage WHERE section_id=?", (section_id,))
        c.execute("DELETE FROM search_fts WHERE section_id=? AND block_id LIKE ?", (section_id, section_id + "-u%"))
        cov: dict[str, tuple[str, list[str], str | None]] = {}
        for i, u in enumerate(units):
            uid = u["id"]
            c.execute("UPDATE units SET ord=?, data=? WHERE id=?", (i, db.jdump(u), uid))
            for bid in u["block_ids"]:
                st = "merged" if (len(u["block_ids"]) > 1 and u.get("coverage") not in ("omitted", "expandable")) \
                    else u.get("coverage", "preserved")
                p = cov.get(bid)
                cov[bid] = (p[0] if p else st, (p[1] if p else []) + [uid], u.get("note"))
            text = " ".join(filter(None, [u.get("title"), u.get("text_md"), u.get("latex")]))
            if text.strip():
                c.execute("INSERT INTO search_fts(block_id, section_id, doc_id, text) VALUES (?,?,?,?)",
                          (uid, section_id, doc_id, text))
        for bid, b in blocks.items():
            if b["block_type"] == "artifact" and bid not in cov:
                why = next((f["message"] for f in json.loads(b["flags"] or "[]") if f.get("kind") == "info"),
                           "Not part of the text")
                cov[bid] = ("omitted", [], why)
        for bid, (st, uids, note) in cov.items():
            c.execute("INSERT OR REPLACE INTO coverage(block_id, section_id, status, unit_ids, note) VALUES (?,?,?,?,?)",
                      (bid, section_id, st, db.jdump(uids), note))


# ------------------------------------------------------------------------------------------------- orchestration
def status_for(report: dict, coverage_ok: bool = True) -> str:
    return "done" if coverage_ok and not report["needs"] else "partial"


def check_section(section_id: str) -> dict:
    """On demand (the 'quality' job): block and unit fixes on an already processed section, new status."""
    sec = db.q1("SELECT * FROM sections WHERE id=?", (section_id,))
    if not sec:
        return {"skipped": "section no longer exists"}
    if not db.q1("SELECT 1 FROM units WHERE section_id=? LIMIT 1", (section_id,)):
        return {"skipped": "not processed yet"}
    cb = clean_blocks(section_id)
    rep = fix_units(section_id)
    if cb["retranscribe"]:  # text that was an image: read it, then rewrite the section
        rep["issues"].append({"kind": "bad_transcription", "needs": "transcribe", "blocks": cb["retranscribe"],
                              "message": f"{len(cb['retranscribe'])} text region(s) that were shown as images"})
        rep["needs"] = sorted(set(rep["needs"]) | {"transcribe"})
        with db.connect() as c:
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (f"quality:{section_id}", json.dumps(rep)))
    fixed = Counter(rep["fixed"])
    fixed.update(cb["fixed"])
    with db.connect() as c:
        if sec["status"] in ("done", "partial"):
            c.execute("UPDATE sections SET status=? WHERE id=?", (status_for(rep), section_id))
    return {"fixed": dict(fixed), "needs": rep["needs"]}


def blocks_to_reread(section_id: str) -> list[str]:
    row = db.q1("SELECT value FROM meta WHERE key=?", (f"quality:{section_id}",))
    if not row:
        return []
    return sorted({b for i in json.loads(row["value"])["issues"] if i["kind"] == "bad_transcription"
                   for b in i.get("blocks", [])})


def document_report(doc_id: str) -> dict:
    secs = db.q("SELECT s.id, s.number, s.title, s.status, c.number AS chapter FROM sections s JOIN chapters c "
                "ON c.id=s.chapter_id WHERE s.doc_id=? ORDER BY c.number, s.ord", (doc_id,))
    keys = [f"quality:{s['id']}" for s in secs]
    marks = ",".join("?" * len(keys)) or "NULL"
    reps = {r["key"][8:]: json.loads(r["value"]) for r in db.q(f"SELECT key, value FROM meta WHERE key IN ({marks})", keys)}
    out, fixed, open_kinds = [], Counter(), Counter()
    for s in secs:
        r = reps.get(s["id"])
        if r:
            fixed.update(r.get("fixed_total", {}))
            open_kinds.update(i["kind"] for i in r["issues"])
        if (r and r["issues"]) or s["status"] in ("partial", "failed"):
            out.append({"section_id": s["id"], "number": s["number"], "title": s["title"], "chapter": s["chapter"],
                        "status": s["status"], "issues": (r or {}).get("issues", []),
                        "checked": bool(r)})
    processed = sum(1 for s in secs if s["status"] in ("done", "partial"))
    return {"sections_processed": processed, "sections_checked": sum(1 for s in secs if s["id"] in reps),
            "fixed": dict(fixed), "open": dict(open_kinds), "sections": out}
