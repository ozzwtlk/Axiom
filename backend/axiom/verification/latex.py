"""LaTeX validation with the same engine the reader uses (KaTeX, via a small Node helper)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

TOOL = Path(__file__).resolve().parents[2] / "tools" / "katex_check.mjs"

MATH_RE = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]|(?<![\\$])\$(?!\$)(.+?)(?<![\\$])\$|\\\((.+?)\\\)", re.S)


def extract_math(md: str) -> list[tuple[str, bool]]:
    out = []
    for m in MATH_RE.finditer(md or ""):
        if m.group(1) is not None:
            out.append((m.group(1).strip(), True))
        elif m.group(2) is not None:
            out.append((m.group(2).strip(), True))
        elif m.group(3) is not None:
            out.append((m.group(3).strip(), False))
        else:
            out.append((m.group(4).strip(), False))
    return out


def strip_math(md: str) -> str:
    return MATH_RE.sub(" ", md or "")


@lru_cache(maxsize=1)
def available() -> bool:
    return bool(shutil.which("node")) and TOOL.exists() and (TOOL.parent / "node_modules" / "katex").exists()


def check(items: list[tuple[str, bool]]) -> list[str | None]:
    """Return one error message (or None) per (latex, display) item. If KaTeX is unavailable, returns
    None for all items and the caller must treat the result as 'unchecked'."""
    if not items:
        return []
    if not available():
        return [None] * len(items)
    r = subprocess.run(["node", str(TOOL)], input=json.dumps([{"latex": l, "display": d} for l, d in items]),
                       capture_output=True, text=True, timeout=120, cwd=TOOL.parent)
    if r.returncode != 0:
        raise RuntimeError(f"katex_check failed: {r.stderr[:500]}")
    return json.loads(r.stdout)


def check_markdown(md: str) -> list[str]:
    """Errors for all math segments in a Markdown string (also catches unbalanced $)."""
    errs = [e for e in check(extract_math(md)) if e]
    rest = strip_math(md)
    if rest.count("$") % 2 == 1:
        errs.append("unbalanced $ delimiter")
    return errs
