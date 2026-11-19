"""Deterministic scorer for vision-OCR transcriptions against a hand-made gold set.

Usage (from backend/):
    uv run python benchmarks/score_transcription.py [--db PATH] [--gold PATH] [--out DIR]

The database is opened strictly read-only. No LLM is involved: scoring is
normalisation + token edit distance + structural "critical error" checks, plus an
optional sympy equivalence test when a LaTeX parser backend is importable.
Always exits 0.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DEFAULT_DB = REPO / "data" / "axiom.db"
DEFAULT_GOLD = HERE / "gold" / "gold.jsonl"
DEFAULT_OUT = HERE / "results"

# --------------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------------

GREEK = {
    "alpha", "beta", "gamma", "delta", "epsilon", "varepsilon", "zeta", "eta", "theta", "vartheta", "iota",
    "kappa", "lambda", "mu", "nu", "xi", "pi", "varpi", "rho", "varrho", "sigma", "varsigma", "tau",
    "upsilon", "phi", "varphi", "chi", "psi", "omega", "Gamma", "Delta", "Theta", "Lambda", "Xi", "Pi",
    "Sigma", "Upsilon", "Phi", "Psi", "Omega",
}
FUNCS = {"sin", "cos", "tan", "cot", "sec", "csc", "sinh", "cosh", "tanh", "coth", "arcsin", "arccos",
         "arctan", "exp", "log", "ln", "det", "tr", "lim", "max", "min", "sup", "inf", "dim", "ker", "deg"}

# Commands taking one mandatory argument.
ARITY1 = {"hat", "tilde", "bar", "dot", "ddot", "vec", "check", "breve", "acute", "grave", "overline",
          "underline", "widehat", "widetilde", "mathbf", "mathcal", "mathbb", "mathfrak", "mathscr",
          "sqrt", "underbrace", "overbrace", "text", "textbf", "textit", "mathrm", "mathsf", "mathit",
          "mathtt", "textrm", "operatorname", "boldsymbol", "bm", "pmb", "emph", "tag", "label"}
ARITY2 = {"frac", "binom", "stackrel", "overset", "underset"}

ALIASES = {
    r"\to": r"\rightarrow", r"\gets": r"\leftarrow", r"\le": r"\leq", r"\ge": r"\geq", r"\ne": r"\neq",
    r"\dots": r"\ldots", r"\dotsc": r"\ldots", r"\dotsb": r"\cdots", r"\ast": "*", r"\lbrace": r"\{",
    r"\rbrace": r"\}", r"\lbrack": "[", r"\rbrack": "]", r"\vert": "|", r"\lvert": "|", r"\rvert": "|",
    r"\tfrac": r"\frac", r"\dfrac": r"\frac", r"\boldsymbol": r"\mathbf", r"\bm": r"\mathbf",
    r"\pmb": r"\mathbf", r"\textbf": r"\mathbf", r"\bf": r"\mathbf", r"\cal": r"\mathcal",
    r"\|": r"\parallel", r"\lt": "<", r"\gt": ">", r"\land": r"\wedge", r"\implies": r"\Rightarrow",
}
# Removed entirely (spacing, sizing, style switches with no semantic content).
DROP = {r"\,", r"\;", r"\:", r"\!", r"\ ", r"\quad", r"\qquad", "~", r"\displaystyle", r"\textstyle",
        r"\scriptstyle", r"\left", r"\right", r"\big", r"\Big", r"\bigg", r"\Bigg", r"\bigl", r"\bigr",
        r"\Bigl", r"\Bigr", r"\biggl", r"\biggr", r"\middle", r"\limits", r"\nolimits", r"\hfill",
        r"\notag", r"\nonumber", r"\enspace", r"\thinspace", r"\medspace", r"\thickspace", r"\hspace",
        r"\rm", r"\it", r"\mathrm"}
# Wrapper commands whose argument is kept but the wrapper itself dropped.
UNWRAP = {"mathrm", "mathsf", "mathit", "mathtt", "textrm", "operatorname", "emph", "textit"}
ALIGN_ENVS = {"aligned", "align", "align*", "eqnarray", "eqnarray*", "split", "gathered", "gather",
              "gather*", "multline", "multline*", "array", "alignat", "alignat*", "flalign", "flalign*"}
MATRIX_ENVS = {"matrix", "pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix", "smallmatrix", "array"}
# Commands that are structure/style only (not compared as "symbols").
NON_SYMBOL = {r"\frac", r"\begin", r"\end", r"\\", r"\mathbf", r"\text", r"\cdots", r"\ldots",
              r"\vdots", r"\ddots", r"\prime", r"\{", r"\}", r"\tag", r"\label", r"\underbrace",
              r"\overbrace", r"\underset", r"\overset", r"\stackrel"}
SYMBOL_CHARS = {"*", "!", "<", ">", "|"}
PUNCT = {".", ",", ";", ":"}
SPACING = {r"\,", r"\;", r"\:", r"\!", r"\ ", r"\quad", r"\qquad", "~"}

# --------------------------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------------------------

_MATH_TOK = re.compile(r"\\[a-zA-Z]+|\\.|[a-zA-Z]|\d|\S", re.DOTALL)
_TEXT_TOK = re.compile(r"[A-Za-z]+(?:['\-][A-Za-z]+)*|\d|\S")
_MATH_SEG = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]|\$(.+?)\$|\\\((.+?)\\\)", re.DOTALL)
_ENV_NAME = re.compile(r"\\(begin|end)\s*\{\s*([a-zA-Z*]+)\s*\}")


def tokenize_math(src: str) -> list[str]:
    """Split LaTeX math into tokens: control words, control symbols, single chars."""
    src = _ENV_NAME.sub(lambda m: f"\\{m.group(1)}{{{m.group(2)}}}", src)
    out: list[str] = []
    pos = 0
    for m in re.finditer(r"\\(begin|end)\{([a-zA-Z*]+)\}|\\(?:text|mbox|textrm|textit)\s*\{([^{}]*)\}", src):
        out += _MATH_TOK.findall(src[pos:m.start()])
        if m.group(1):
            out.append(f"\\{m.group(1)}{{{m.group(2)}}}")
        else:  # text-mode content: whole words are tokens
            out += [r"\text", "{"] + tokenize_text(m.group(3)) + ["}"]
        pos = m.end()
    out += _MATH_TOK.findall(src[pos:])
    return out


def tokenize_text(src: str) -> list[str]:
    src = src.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    src = src.replace("**", " ").replace("__", " ")
    return _TEXT_TOK.findall(src)


def split_segments(md: str) -> list[tuple[str, str]]:
    """Return [(kind, content)] with kind in {'text','math'}."""
    segs: list[tuple[str, str]] = []
    pos = 0
    for m in _MATH_SEG.finditer(md):
        if m.start() > pos:
            segs.append(("text", md[pos:m.start()]))
        segs.append(("math", next(g for g in m.groups() if g is not None)))
        pos = m.end()
    if pos < len(md):
        segs.append(("text", md[pos:]))
    return [(k, c) for k, c in segs if c.strip()]


# --------------------------------------------------------------------------------------------
# Parser -> small tree
# --------------------------------------------------------------------------------------------


@dataclass
class Tok:
    s: str


@dataclass
class Group:
    items: list
    lead_space: bool = False  # `{\ \nu}` idiom: explicit horizontal offset (index staggering)


@dataclass
class Cmd:
    name: str  # with backslash
    args: list  # list[Group]


@dataclass
class Env:
    name: str
    body: list


@dataclass
class Script:
    base: object
    sub: Group | None = None
    sup: Group | None = None
    extra: list = field(default_factory=list)  # further scripts beyond one sub + one sup, in order [(kind, Group)]
    stagger: str = ""  # "^_" / "_^" when the source explicitly staggered sup and sub (via {} or a leading space)


class _Parser:
    def __init__(self, toks: list[str]):
        self.t = toks
        self.i = 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else None

    def next(self):
        tok = self.peek()
        self.i += 1
        return tok

    def parse_seq(self, stop: set[str]) -> list:
        items: list = []
        while self.peek() is not None and self.peek() not in stop:
            node = self.parse_atom()
            if node is None:
                continue
            node = self.parse_scripts(node)
            items.append(node)
        return items

    def parse_group_until(self, close: str) -> Group:
        items = self.parse_seq({close})
        if self.peek() == close:
            self.next()
        return Group(items)

    def parse_arg(self) -> Group:
        # skip dropped tokens between command and argument
        while self.peek() in DROP:
            self.next()
        tok = self.peek()
        if tok is None:
            return Group([])
        if tok == "{":
            self.next()
            lead = self.peek() in SPACING
            g = self.parse_group_until("}")
            g.lead_space = lead and bool(g.items)
            return g
        node = self.parse_atom()
        return Group([node] if node is not None else [])

    def parse_atom(self):
        tok = self.next()
        if tok is None:
            return None
        tok = ALIASES.get(tok, tok)
        if tok in DROP:
            if tok in (r"\left", r"\right") and self.peek() == ".":
                self.next()
            if tok == r"\hspace" and self.peek() == "{":
                self.next()
                self.parse_group_until("}")
            if tok == r"\mathrm":  # \mathrm{X} -> X (handled as unwrap)
                return _unwrap(self.parse_arg())
            return None
        if tok == "{":
            return self.parse_group_until("}")
        if tok == "}":
            return None
        if tok.startswith(r"\begin{"):
            name = tok[7:-1]
            if name in ("array", "alignat", "alignat*") and self.peek() == "{":  # column spec
                self.next()
                self.parse_group_until("}")
            body = self.parse_seq({rf"\end{{{name}}}"})
            if self.peek() == rf"\end{{{name}}}":
                self.next()
            return Env(name, body)
        if tok.startswith("\\") and tok[1:] in ARITY2:
            return Cmd(tok, [self.parse_arg(), self.parse_arg()])
        if tok.startswith("\\") and tok[1:] in ARITY1:
            name = tok[1:]
            opt = None
            if name == "sqrt" and self.peek() == "[":
                self.next()
                opt = self.parse_group_until("]")
            arg = self.parse_arg()
            if name in ("tag", "label"):
                return None
            if name in UNWRAP:
                if name == "operatorname":
                    word = "".join(_flat_strings(arg))
                    if word in FUNCS:
                        return Tok("\\" + word)
                return _unwrap(arg)
            if name == "text":
                return Group([Tok(w) for w in tokenize_text(" ".join(_flat_strings(arg)))])
            return Cmd(tok, [arg] + ([opt] if opt is not None else []))
        return Tok(tok)

    def parse_scripts(self, base):
        if self.peek() not in ("^", "_", "'"):
            return base
        sc = Script(base)
        order: list[tuple[str, Group]] = []
        explicit_gap = False
        while self.peek() in ("^", "_", "'") or (self.peek() == "{" and self._empty_group_then_script()):
            tok = self.next()
            if tok == "{":  # empty {} between staggered scripts
                self.next()
                explicit_gap = True
                continue
            if tok == "'":
                order.append(("^", Group([Tok(r"\prime")])))
                continue
            order.append(("^" if tok == "^" else "_", self.parse_arg()))
        # merge consecutive sup primes into one sup group
        merged: list[tuple[str, Group]] = []
        for kind, g in order:
            if merged and kind == "^" and merged[-1][0] == "^" and (
                    _is_primes(g) or _is_primes(merged[-1][1])):
                merged[-1] = ("^", Group(merged[-1][1].items + g.items))
            else:
                merged.append((kind, g))
        kinds = [k for k, _ in merged]
        if len(merged) <= 2 and len(set(kinds)) == len(kinds):
            for k, g in merged:
                if k == "^":
                    sc.sup = g
                else:
                    sc.sub = g
            if len(merged) == 2:
                if explicit_gap:
                    sc.stagger = kinds[0] + kinds[1]
                elif merged[1][1].lead_space or merged[0][1].lead_space:
                    # the group with the leading space is visually the later one
                    late = 1 if merged[1][1].lead_space else 0
                    sc.stagger = kinds[1 - late] + kinds[late]
        else:
            sc.extra = merged
        return sc

    def _empty_group_then_script(self) -> bool:
        return (self.i + 2 < len(self.t) and self.t[self.i + 1] == "}" and self.t[self.i + 2] in ("^", "_"))


def _is_primes(g: Group) -> bool:
    return bool(g.items) and all(isinstance(x, Tok) and x.s == r"\prime" for x in g.items)


def _unwrap(g: Group):
    return g if len(g.items) != 1 else g.items[0]


def _flat_strings(node) -> list[str]:
    if isinstance(node, Tok):
        return [node.s]
    if isinstance(node, Group):
        return [s for x in node.items for s in _flat_strings(x)]
    if isinstance(node, Cmd):
        return [s for a in node.args for s in _flat_strings(a)]
    if isinstance(node, Env):
        return [s for x in node.body for s in _flat_strings(x)]
    if isinstance(node, Script):
        out = _flat_strings(node.base)
        for g in (node.sub, node.sup):
            if g is not None:
                out += _flat_strings(g)
        for _, g in node.extra:
            out += _flat_strings(g)
        return out
    return []


_ROMAN = re.compile(r"\\(?:mathrm|textrm|mathit|mbox|rm)\s*\{([^{}]*)\}")
_SPACE_CMD = re.compile(r"\\[,;: !]|\\q?quad|~")


def _roman_words(m: re.Match) -> str:
    """`\\mathrm{ i s \\; a n }` (math mode: spaces ignored, \\; separates words) -> `\\text{is an}`."""
    body = _SPACE_CMD.sub("\x00", m.group(1))
    words = [re.sub(r"\s+", "", w) for w in body.split("\x00")]
    words = [w for w in words if w]
    if sum(len(w) for w in words) <= 1 or not all(re.fullmatch(r"[A-Za-z.,:;]+", w) for w in words):
        return "{" + m.group(1) + "}"
    return r"\text{" + " ".join(words) + "}"


def parse_math(src: str) -> list:
    src = _ROMAN.sub(_roman_words, src)
    src = re.sub(r"\\tag\*?\{[^}]*\}", " ", src)
    src = re.sub(r"\(\s*\d+\s*\.\s*\d+\s*\)\s*$", " ", src.strip())  # stray equation number at end
    items = _Parser(tokenize_math(src)).parse_seq(set())
    return _postprocess(items)


def _postprocess(items: list) -> list:
    """Flatten free groups, recognise delimited matrices, recurse."""
    flat: list = []
    for x in items:
        x = _post_node(x)
        if isinstance(x, Group):
            flat.extend(x.items)
        elif x is not None:
            flat.append(x)
    # ( \begin{array} ... \end{array} ) -> pmatrix ; [ ] -> bmatrix ; \{ array -> cases
    out: list = []
    i = 0
    while i < len(flat):
        x = flat[i]
        if (isinstance(x, Tok) and x.s in ("(", "[", "|") and i + 2 < len(flat) and isinstance(flat[i + 1], Env)
                and flat[i + 1].name in MATRIX_ENVS and isinstance(flat[i + 2], Tok)
                and flat[i + 2].s == {"(": ")", "[": "]", "|": "|"}[x.s]):
            out.append(Env({"(": "pmatrix", "[": "bmatrix", "|": "vmatrix"}[x.s], flat[i + 1].body))
            i += 3
            continue
        if (isinstance(x, Tok) and x.s == r"\{" and i + 1 < len(flat) and isinstance(flat[i + 1], Env)
                and flat[i + 1].name == "array"):
            out.append(Env("cases", flat[i + 1].body))
            i += 2
            continue
        if isinstance(x, Env) and x.name == "array":  # undelimited array: treat as an alignment
            x = Env("aligned", [b for b in x.body if not (isinstance(b, Tok) and b.s == "&")])
        out.append(x)
        i += 1
    return out


def _post_group(g: Group | None) -> Group | None:
    if g is None:
        return None
    return Group(_postprocess(g.items))


def _post_node(x):
    if isinstance(x, Group):
        return Group(_postprocess(x.items))
    if isinstance(x, Cmd):
        return Cmd(x.name, [_post_group(a) for a in x.args])
    if isinstance(x, Env):
        name = x.name
        body = _postprocess(x.body)
        if name in ALIGN_ENVS and name != "array":  # array is resolved by the caller (matrix vs alignment)
            name = "aligned"
        if name in ("matrix", "smallmatrix"):
            name = "matrix"
        if name == "aligned":  # alignment points carry no meaning
            body = [b for b in body if not (isinstance(b, Tok) and b.s == "&")]
        while body and isinstance(body[-1], Tok) and body[-1].s == r"\\":
            body.pop()
        return Env(name, body)
    if isinstance(x, Script):
        base = _post_node(x.base)
        if isinstance(base, Group) and len(base.items) == 1:
            base = base.items[0]
        return Script(base, _post_group(x.sub), _post_group(x.sup), [(k, _post_group(g)) for k, g in x.extra],
                      x.stagger)
    return x


# --------------------------------------------------------------------------------------------
# Serialisation -> canonical token list
# --------------------------------------------------------------------------------------------


def serialize(node) -> list[str]:
    if isinstance(node, list):
        return [t for x in node for t in serialize(x)]
    if isinstance(node, Tok):
        return [node.s]
    if isinstance(node, Group):
        return serialize(node.items)
    if isinstance(node, Cmd):
        out = [node.name]
        if node.name == r"\sqrt" and len(node.args) > 1:
            out += ["["] + serialize(node.args[1]) + ["]"]
        for a in node.args[:1] if node.name == r"\sqrt" else node.args:
            out += ["{"] + serialize(a) + ["}"]
        return out
    if isinstance(node, Env):
        return [rf"\begin{{{node.name}}}"] + serialize(node.body) + [rf"\end{{{node.name}}}"]
    if isinstance(node, Script):
        base = serialize(node.base)
        if isinstance(node.base, (Group, Env)) or len(base) > 1 and not isinstance(node.base, (Cmd, Script)):
            base = ["{"] + base + ["}"]
        out = base
        if node.extra:
            for k, g in node.extra:
                out += [k, "{"] + serialize(g) + ["}"]
            return out
        if node.sub is not None:
            out += ["_", "{"] + serialize(node.sub) + ["}"]
        if node.sup is not None:
            out += ["^", "{"] + serialize(node.sup) + ["}"]
        return out
    return []


def _strip_math_punct(toks: list[str]) -> list[str]:
    """Drop punctuation that ends a math segment, a row, or an environment."""
    out: list[str] = []
    for i, t in enumerate(toks):
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if t in PUNCT and (nxt is None or nxt == r"\\" or nxt.startswith(r"\end{")):
            continue
        out.append(t)
    # punctuation directly before an \end may now be exposed again (e.g. ". \\ \end")
    if out != toks:
        return _strip_math_punct(out)
    return out


def _prenorm(content: str) -> str:
    """Source-level rewrites applied before parsing (both for scoring and for critical-error checks)."""
    content = re.sub(r"\\cdot\s*\\cdot\s*\\cdot", r"\\cdots ", content)  # "· · ·" spelled out
    return re.sub(r"\.\s*\.\s*\.", r"\\ldots ", content)


def normalize_tokens(md: str | None) -> list[str]:
    """Full normalised token stream for a transcription (text words + math tokens, `$` marks math)."""
    if not md:
        return []
    out: list[str] = []
    for kind, content in split_segments(md.strip()):
        if kind == "text":
            out += tokenize_text(content)
        else:
            out += ["$"] + _strip_math_punct(serialize(parse_math(_prenorm(content)))) + ["$"]
    while out and out[-1] in PUNCT:
        out.pop()
    return out


def normalize(md: str | None) -> str:
    return " ".join(normalize_tokens(md))


def math_trees(md: str | None) -> list:
    if not md:
        return []
    return [node for kind, c in split_segments(md) if kind == "math" for node in parse_math(_prenorm(c))]


# --------------------------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------------------------


def levenshtein(a: list[str], b: list[str]) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def similarity(a: list[str], b: list[str]) -> float:
    if not a and not b:
        return 1.0
    return 1.0 - levenshtein(a, b) / max(len(a), len(b))


def _walk(nodes):
    """Yield every node in a tree (pre-order)."""
    stack = list(reversed(nodes if isinstance(nodes, list) else [nodes]))
    while stack:
        n = stack.pop()
        yield n
        kids: list = []
        if isinstance(n, Group):
            kids = n.items
        elif isinstance(n, Cmd):
            kids = [a for a in n.args if a is not None]
        elif isinstance(n, Env):
            kids = n.body
        elif isinstance(n, Script):
            kids = [n.base] + [g for g in (n.sub, n.sup) if g is not None] + [g for _, g in n.extra]
        stack.extend(reversed(kids))


def sign_counts(trees) -> Counter:
    c: Counter = Counter()
    for n in _walk(trees):
        if isinstance(n, Tok) and n.s in ("-", r"\pm", r"\mp", "+"):
            c[n.s] += 1
    return c


def _strip_primes(node):
    if isinstance(node, Script):
        sup = node.sup
        if sup is not None:
            items = [x for x in sup.items if not (isinstance(x, Tok) and x.s == r"\prime")]
            sup = Group(items) if items else None
        return Script(_strip_primes(node.base), node.sub, sup, node.extra)
    return node


def _key(node) -> str:
    return "".join(serialize(_strip_primes(node)))


def _script_groups(sc: Script):
    if sc.extra:
        return list(sc.extra)
    return [(k, g) for k, g in (("_", sc.sub), ("^", sc.sup)) if g is not None]


def primed_symbols(trees) -> Counter:
    """Multiset of symbols carrying a prime, keyed by the primed base (e.g. '\\mu', 't', '\\mu_{1}')."""
    c: Counter = Counter()
    for n in _walk(trees):
        if isinstance(n, Script):
            for k, g in _script_groups(n):
                if k == "^":
                    p = sum(1 for x in g.items if isinstance(x, Tok) and x.s == r"\prime")
                    if p:
                        c[_key(Script(n.base, n.sub if not n.extra else None)) + "'" * p] += 1
    return c


def _index_items(g: Group) -> list[str]:
    out = []
    for x in g.items:
        if isinstance(x, Tok):
            s = x.s
            if s == r"\prime":
                continue
            if re.fullmatch(r"[a-zA-Z0-9]", s) or (s.startswith("\\") and s[1:] in GREEK):
                out.append(s)
        elif isinstance(x, (Script, Cmd)):
            out.append(_key(x))
        elif isinstance(x, Group):
            out += _index_items(x)
    return out


def index_placements(trees) -> Counter:
    """Multiset of (base, index, 'up'|'down') over every script attachment."""
    c: Counter = Counter()
    for n in _walk(trees):
        if isinstance(n, Script):
            base = _key(n.base)
            for k, g in _script_groups(n):
                for idx in _index_items(g):
                    c[(base, idx, "up" if k == "^" else "down")] += 1
    return c


def symbol_set(trees) -> set[str]:
    s: set[str] = set()
    for n in _walk(trees):
        if isinstance(n, Tok):
            if n.s.startswith("\\") and n.s not in NON_SYMBOL or n.s in SYMBOL_CHARS:
                s.add(n.s)
        elif isinstance(n, Cmd) and n.name not in NON_SYMBOL:
            s.add(n.name)
    return s


def matrix_shapes(trees) -> list[tuple[str, int, int]]:
    shapes = []
    for n in _walk(trees):
        if isinstance(n, Env) and n.name in MATRIX_ENVS | {"pmatrix", "bmatrix", "vmatrix", "cases"}:
            rows = [[]]
            for x in n.body:
                if isinstance(x, Tok) and x.s == r"\\":
                    rows.append([])
                else:
                    rows[-1].append(x)
            ncols = max(1 + sum(1 for x in r if isinstance(x, Tok) and x.s == "&") for r in rows)
            shapes.append((n.name, len(rows), ncols))
    return shapes


def staggered_scripts(trees) -> Counter:
    """Scripts whose horizontal sup/sub order was made explicit (e.g. `\\Lambda^{\\mu'}{}_{\\nu}`)."""
    c: Counter = Counter()
    for n in _walk(trees):
        if isinstance(n, Script) and n.stagger:
            c[(_key(Script(n.base, n.sub, n.sup)), n.stagger)] += 1
    return c


def structural_warnings(gold_md: str, model_md: str) -> list[dict]:
    """Non-critical structural differences (reported, not counted as critical)."""
    g, m = math_trees(gold_md), math_trees(model_md)
    out = []
    gs, ms = staggered_scripts(g), staggered_scripts(m)
    lost = gs - ms
    if lost:
        out.append({"kind": "index_order_lost",
                    "detail": f"{sum(lost.values())} staggered tensor(s) not staggered the same way in model: "
                              + ", ".join(f"{k}[{o}]" for k, o in sorted(lost))})
    return out


def critical_errors(gold_md: str, model_md: str) -> list[dict]:
    g, m = math_trees(gold_md), math_trees(model_md)
    errs: list[dict] = []
    gs, ms = sign_counts(g), sign_counts(m)
    for op in ("-", r"\pm", r"\mp"):
        if gs[op] != ms[op]:
            errs.append({"kind": "sign", "detail": f"count of '{op}': gold {gs[op]} vs model {ms[op]}"})
    gp, mp = primed_symbols(g), primed_symbols(m)
    if gp != mp:
        errs.append({"kind": "prime", "detail": f"missing {dict(gp - mp)} extra {dict(mp - gp)}"})
    gi, mi = index_placements(g), index_placements(m)
    moved = []
    for base, idx in {(b, i) for b, i, _ in gi} | {(b, i) for b, i, _ in mi}:
        gu, gd = gi[(base, idx, "up")], gi[(base, idx, "down")]
        mu, md = mi[(base, idx, "up")], mi[(base, idx, "down")]
        if gu + gd == mu + md and (gu, gd) != (mu, md):
            moved.append(f"{base}:{idx} gold up/down={gu}/{gd} model={mu}/{md}")
    if moved:
        errs.append({"kind": "index_placement", "detail": "; ".join(sorted(moved))})
    gsy, msy = symbol_set(g), symbol_set(m)
    if gsy != msy:
        errs.append({"kind": "symbols", "detail": f"missing {sorted(gsy - msy)} extra {sorted(msy - gsy)}"})
    gsh, msh = matrix_shapes(g), matrix_shapes(m)
    if [s[1:] for s in gsh] != [s[1:] for s in msh]:
        errs.append({"kind": "matrix_shape", "detail": f"gold {gsh} vs model {msh}"})
    return errs


# --------------------------------------------------------------------------------------------
# Optional sympy equivalence
# --------------------------------------------------------------------------------------------

_SYMPY_STATE: dict = {}


def _sympy_parser():
    if "fn" in _SYMPY_STATE:
        return _SYMPY_STATE["fn"]
    fn = None
    try:
        import warnings

        from sympy.parsing.latex import parse_latex
        for backend in ("lark", "antlr"):
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    parse_latex("x+1", backend=backend)
                fn = (lambda s, b=backend: parse_latex(s, backend=b))
                _SYMPY_STATE["backend"] = backend
                break
            except Exception:  # noqa: BLE001, S112 - backend missing
                continue
    except Exception:  # noqa: BLE001
        fn = None
    _SYMPY_STATE["fn"] = fn
    return fn


def sympy_equivalent(gold_md: str, model_md: str) -> bool | None:
    """True/False if both sides parse and compare; None if skipped (no parser, unparsable, not applicable)."""
    parse = _sympy_parser()
    if parse is None:
        return None
    gm = [c for k, c in split_segments(gold_md) if k == "math"]
    mm = [c for k, c in split_segments(model_md or "") if k == "math"]
    if len(gm) != 1 or len(mm) != 1:
        return None

    def sides(src: str):
        toks = _strip_math_punct(serialize(parse_math(src)))
        if any(t.startswith(r"\begin") for t in toks):
            return None
        toks = [t for t in toks if t not in (r"\mathbf",)]
        parts, cur = [], []
        for t in toks:
            if t == "=":
                parts.append(cur)
                cur = []
            else:
                cur.append(t)
        parts.append(cur)
        return [" ".join(p) for p in parts]

    try:
        import sympy
        gs, ms = sides(gm[0]), sides(mm[0])
        if not gs or not ms or len(gs) != len(ms) or len(gs) < 2:
            return None
        for a, b in zip(gs, ms):
            d = parse(a) - parse(b)
            if sympy.expand(d) != 0 and sympy.simplify(d) != 0:
                return False
        return True
    except Exception:  # noqa: BLE001 - unparsable LaTeX -> skip
        return None


# --------------------------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------------------------


def score_item(gold: dict, model_md: str | None, method: str) -> dict:
    res = {"block_id": gold["block_id"], "page": gold["page"], "eq_number": gold.get("eq_number"),
           "category": gold["category"], "method": method, "gold_md": gold["gold_md"], "model_md": model_md}
    if not model_md:
        res.update(status="missing")
        return res
    mt = normalize_tokens(model_md)
    # Ambiguous items may list equally acceptable alternative golds; score against the closest one.
    candidates = [gold["gold_md"]] + list(gold.get("gold_alt_md") or [])
    best_md, gt = max(((g, normalize_tokens(g)) for g in candidates), key=lambda p: similarity(p[1], mt))
    res.update(status="scored", exact=gt == mt, similarity=round(similarity(gt, mt), 4),
               critical=critical_errors(best_md, model_md),
               warnings=structural_warnings(best_md, model_md),
               sympy_equiv=sympy_equivalent(best_md, model_md),
               matched_alt=best_md != gold["gold_md"],
               gold_norm=" ".join(gt), model_norm=" ".join(mt))
    return res


def load_model(db_path: Path, ids: list[str]) -> dict[str, tuple[str | None, str]]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        out = {}
        for bid in ids:
            row = con.execute("SELECT transcribed_md, transcription_method FROM blocks WHERE id=?", (bid,)).fetchone()
            out[bid] = (row[0], row[1]) if row else (None, "absent")
        return out
    finally:
        con.close()


def summarize(results: list[dict]) -> dict:
    scored = [r for r in results if r["status"] == "scored"]
    by_cat: dict = {}
    for r in results:
        c = by_cat.setdefault(r["category"], {"n": 0, "scored": 0, "exact": 0, "sim_sum": 0.0, "critical": 0})
        c["n"] += 1
        if r["status"] == "scored":
            c["scored"] += 1
            c["exact"] += r["exact"]
            c["sim_sum"] += r["similarity"]
            c["critical"] += bool(r["critical"])
    for c in by_cat.values():
        c["mean_similarity"] = round(c.pop("sim_sum") / c["scored"], 4) if c["scored"] else None
    sym = [r["sympy_equiv"] for r in scored if r.get("sympy_equiv") is not None]
    return {
        "n_gold": len(results), "n_scored": len(scored), "n_missing": len(results) - len(scored),
        "exact_match_rate": round(sum(r["exact"] for r in scored) / len(scored), 4) if scored else None,
        "mean_similarity": round(sum(r["similarity"] for r in scored) / len(scored), 4) if scored else None,
        "n_with_critical": sum(bool(r["critical"]) for r in scored),
        "critical_items": [r["block_id"] for r in scored if r["critical"]],
        "n_with_warnings": sum(bool(r["warnings"]) for r in scored),
        "sympy_backend": _SYMPY_STATE.get("backend"), "sympy_checked": len(sym),
        "sympy_equivalent": sum(sym),
        "by_category": by_cat,
    }


def render_markdown(summary: dict, results: list[dict], stamp: str) -> str:
    s = summary
    lines = [f"# Transcription benchmark {stamp}", "",
             f"- gold items: {s['n_gold']} (scored {s['n_scored']}, not yet transcribed {s['n_missing']})",
             f"- exact match (normalised): {s['exact_match_rate']}",
             f"- mean token similarity: {s['mean_similarity']}",
             f"- items with critical errors: {s['n_with_critical']}",
             f"- items with non-critical warnings (e.g. tensor index order not encoded): {s['n_with_warnings']}",
             (f"- sympy equivalence: backend={s['sympy_backend']}, checked {s['sympy_checked']}, "
              f"equivalent {s['sympy_equivalent']}"), "",
             "| category | n | scored | exact | mean sim | critical |", "|---|---|---|---|---|---|"]
    for cat, c in sorted(s["by_category"].items()):
        lines.append(f"| {cat} | {c['n']} | {c['scored']} | {c['exact']} | {c['mean_similarity']} | {c['critical']} |")
    lines += ["", "## Critical errors", ""]
    for r in results:
        if r["status"] == "scored" and r["critical"]:
            lines.append(f"- `{r['block_id']}` ({r['category']}, eq {r['eq_number']}):")
            for e in r["critical"]:
                lines.append(f"  - {e['kind']}: {e['detail']}")
            lines.append(f"  - gold: `{r['gold_norm']}`")
            lines.append(f"  - model: `{r['model_norm']}`")
    lines += ["", "## Warnings (non-critical)", ""]
    for r in results:
        if r["status"] == "scored" and r["warnings"]:
            lines.append(f"- `{r['block_id']}`: " + "; ".join(f"{w['kind']}: {w['detail']}" for w in r["warnings"]))
    lines += ["", "## Per item", "", "| block | cat | status | exact | sim | critical |", "|---|---|---|---|---|---|"]
    for r in results:
        if r["status"] == "scored":
            lines.append(f"| {r['block_id']} | {r['category']} | scored | {r['exact']} | {r['similarity']} | "
                         f"{','.join(e['kind'] for e in r['critical']) or '-'} |")
        else:
            lines.append(f"| {r['block_id']} | {r['category']} | {r['status']} | | | |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    try:
        gold = [json.loads(line) for line in args.gold.read_text().splitlines() if line.strip()]
        model = load_model(args.db, [g["block_id"] for g in gold])
        results = [score_item(g, *model[g["block_id"]]) for g in gold]
        summary = summarize(results)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / f"transcription_{stamp}.json").write_text(
            json.dumps({"timestamp": stamp, "summary": summary, "items": results}, indent=2, ensure_ascii=False))
        md = render_markdown(summary, results, stamp)
        (args.out / f"transcription_{stamp}.md").write_text(md)
        print(f"{'block':<26}{'cat':<12}{'status':<9}{'exact':<7}{'sim':<8}critical")
        for r in results:
            if r["status"] == "scored":
                crit = ",".join(e["kind"] for e in r["critical"]) or "-"
                print(f"{r['block_id']:<26}{r['category']:<12}{'scored':<9}{r['exact']!s:<7}"
                      f"{r['similarity']:<8}{crit}")
            else:
                print(f"{r['block_id']:<26}{r['category']:<12}{r['status']:<9}")
        print()
        print(json.dumps({k: v for k, v in summary.items() if k != "by_category"}, indent=2))
        print(f"\nwrote {args.out / f'transcription_{stamp}.json'} and .md")
    except Exception as e:  # noqa: BLE001 - benchmark must never fail the caller
        print(f"scorer error: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
