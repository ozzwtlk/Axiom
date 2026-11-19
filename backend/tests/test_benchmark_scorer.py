"""Unit tests for the deterministic transcription scorer (pure functions, no DB)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.score_transcription import (
    critical_errors,
    index_placements,
    math_trees,
    normalize,
    normalize_tokens,
    primed_symbols,
    sign_counts,
    similarity,
    split_segments,
    structural_warnings,
    symbol_set,
    tokenize_math,
)


def same(a: str, b: str) -> bool:
    return normalize_tokens(a) == normalize_tokens(b)


def kinds(gold: str, model: str) -> set[str]:
    return {e["kind"] for e in critical_errors(gold, model)}


# ---------------------------------------------------------------- tokenizer / segments

def test_tokenize_math_basic():
    assert tokenize_math(r"\frac{a}{b}") == [r"\frac", "{", "a", "}", "{", "b", "}"]
    assert tokenize_math(r"x^{\mu'}") == ["x", "^", "{", r"\mu", "'", "}"]
    assert tokenize_math(r"\begin {aligned} a \end{aligned}") == [r"\begin{aligned}", "a", r"\end{aligned}"]
    assert tokenize_math(r"\Lambda _ { \ \nu }")[3] == "\\ "


def test_tokenize_text_inside_math_keeps_words():
    toks = tokenize_math(r"\text{if } x")
    assert toks == [r"\text", "{", "if", "}", "x"]


def test_split_segments():
    segs = split_segments(r"where $a^\mu$ is $$x=1$$ done")
    assert [k for k, _ in segs] == ["text", "math", "text", "math", "text"]


# ---------------------------------------------------------------- normalizer equivalences

@pytest.mark.parametrize("a,b", [
    (r"$$x^2 = y_1,$$", r"$$ x ^ { 2 } = y _ { 1 } $$"),                      # spaces, braces, trailing punct
    (r"$$a\,b\;c\quad d$$", r"$$abcd$$"),                                    # spacing commands
    (r"$$\left( \frac{1}{2} \right)$$", r"$$(\frac12)$$"),                  # \left/\right, single-token args
    (r"$$\mathbf{F} = m\boldsymbol{a}$$", r"$${\bf F} = m \mathbf a$$"),    # bold variants
    (r"$$\Lambda^{\mathrm{T}}$$", r"$$\Lambda^T$$"),                          # \mathrm{T} vs T
    (r"$$\Lambda^{\mathsf{T}}$$", r"$$\Lambda^{T}$$"),
    (r"$${\textstyle\frac{1}{2}}x$$", r"$$\tfrac{1}{2}x$$"),                  # style switches, \tfrac
    (r"$$x' = t$$", r"$$x^{\prime} = t$$"),                                  # prime spellings
    (r"$$\mu'_1$$", r"$$\mu_{1}^{\prime}$$"),
    (r"$$\Lambda^{\mu'}{}_{\nu}$$", r"$$\Lambda_{\ \nu}^{\mu'}$$"),          # staggering spelled differently
    (r"$$\delta^{\mu'}_\mu$$", r"$$\delta_{\mu}^{\mu^\prime}$$"),
    (r"$$\begin{pmatrix} -1 & 0 \\ 0 & 1 \end{pmatrix}$$",
     r"$$\left( \begin{array}{cc} {{-1}} & {{0}} \\ {{0}} & {{1}} \end{array} \right)$$"),
    (r"$$\begin{aligned} t' &= a \\ x' &= b. \end{aligned}$$",
     "$$\n\\begin{aligned}\nt' & = a \\\\\nx' &= b .\n\\end{aligned}\n$$"),
    (r"$$\begin{cases} 1 & \text{if } x \\ 0 & \text{otherwise.} \end{cases}$$",
     r"$$\left\{ \begin{array}{ll} 1 & \mathrm{if} \; x \\ 0 & \mathrm{o t h e r w i s e .} \end{array} \right.$$"),
    (r"$$x \to y$$", r"$$x \rightarrow y$$"),
    (r"$$\operatorname{tanh} x$$", r"$$\tanh x$$"),
    (r"$$a = b \tag{1.3}$$", r"$$a = b$$"),
    (r"For a **trace** $x$.", r"For a trace $x$."),                            # markdown bold in prose
])
def test_normalizer_equivalent(a, b):
    assert same(a, b), (normalize(a), normalize(b))


@pytest.mark.parametrize("a,b", [
    (r"$$x - y$$", r"$$x + y$$"),
    (r"$$x^\mu$$", r"$$x_\mu$$"),
    (r"$$x^{\mu'}$$", r"$$x^\mu$$"),
    (r"$${(a+b)}^2$$", r"$$a+b^2$$"),
    (r"$$\phi$$", r"$$\varphi$$"),
    (r"$$\begin{pmatrix} 1 & 0 \end{pmatrix}$$", r"$$\begin{pmatrix} 1 \\ 0 \end{pmatrix}$$"),
])
def test_normalizer_keeps_real_differences(a, b):
    assert not same(a, b)


# ---------------------------------------------------------------- similarity

def test_similarity_bounds():
    assert similarity([], []) == 1.0
    assert similarity(["a", "b"], ["a", "b"]) == 1.0
    assert similarity(["a", "b"], ["a", "c"]) == 0.5
    assert similarity(["a"], []) == 0.0


# ---------------------------------------------------------------- critical-error checks

def test_sign_error_detected():
    gold = r"$$(\Delta s)^2 = -(c\Delta t)^2 + (\Delta x)^2$$"
    assert "sign" in kinds(gold, r"$$(\Delta s)^2 = (c\Delta t)^2 + (\Delta x)^2$$")
    assert "sign" not in kinds(gold, r"$$( \Delta s ) ^ { 2 } = - ( c \Delta t ) ^ { 2 } + ( \Delta x ) ^ { 2 }$$")
    assert sign_counts(math_trees(r"$$a \pm b - c$$"))[r"\pm"] == 1


def test_prime_error_detected():
    gold = r"$$\Lambda^{\mu}{}_{\nu'}\Lambda^{\nu'}{}_{\rho} = \delta^\mu_\rho$$"
    model = r"$$\Lambda_{\ \nu'}^{\mu'}\Lambda_{\ \rho}^{\nu'} = \delta_\rho^\mu$$"
    assert kinds(gold, model) == {"prime"}
    assert primed_symbols(math_trees(r"$$\mu'_1 + t'$$")) == primed_symbols(math_trees(r"$$\mu_1^\prime + t^{\prime}$$"))


def test_index_placement_error_detected():
    gold = r"$$\omega_{\mu'} = \Lambda^{\nu}{}_{\mu'}\omega_\nu$$"
    model = r"$$\omega_{\mu'} = \Lambda_{\nu}{}^{\mu'}\omega_\nu$$"
    assert "index_placement" in kinds(gold, model)
    pl = index_placements(math_trees(gold))
    assert pl[(r"\Lambda", r"\nu", "up")] == 1 and pl[(r"\Lambda", r"\mu", "down")] == 1


def test_symbol_set_difference_detected():
    assert "symbols" in kinds(r"$$\partial_\mu \phi$$", r"$$d_\mu \phi$$")
    assert "symbols" in kinds(r"$$\epsilon$$", r"$$\varepsilon$$")
    assert r"\wedge" in symbol_set(math_trees(r"$$A \wedge B$$"))
    assert "*" in symbol_set(math_trees(r"$$**A$$"))


def test_matrix_shape_detected():
    assert "matrix_shape" in kinds(r"$$\begin{pmatrix} 1 & 0 \\ 0 & 1 \end{pmatrix}$$",
                                   r"$$\begin{pmatrix} 1 & 0 & 0 \\ 0 & 1 & 0 \end{pmatrix}$$")


def test_clean_transcription_has_no_critical_errors():
    gold = r"$$x^{\mu'} = \Lambda^{\mu'}{}_{\nu} x^\nu,$$"
    model = r"$$x ^ { \mu ^ { \prime } } = \Lambda ^ { \mu ^ { \prime } } { } _ { \nu } x ^ { \nu } ,$$"
    assert critical_errors(gold, model) == []
    assert same(gold, model)


def test_index_order_warning():
    gold = r"$$\Lambda^{\mu}{}_{\mu'}\phi$$"
    assert structural_warnings(gold, r"$$\Lambda_{\mu'}^{\mu}\phi$$")            # stagger information lost
    assert not structural_warnings(gold, r"$$\Lambda_{\ \mu'}^{\mu}\phi$$")      # leading-space idiom keeps it
    assert not structural_warnings(gold, r"$$\Lambda^{\mu}{}_{\mu'}\phi$$")
