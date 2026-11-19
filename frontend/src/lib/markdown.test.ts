import { describe, expect, it } from "vitest";
import { excerpt, extractMath, renderInline, renderMarkdown, renderMath, _cacheSizes } from "./markdown";

describe("extractMath", () => {
  it("extracts all four delimiters", () => {
    const { math } = extractMath("a $x$ b $$y$$ c \\(z\\) d \\[w\\]");
    expect(math).toEqual([
      { tex: "x", display: false },
      { tex: "y", display: true },
      { tex: "z", display: false },
      { tex: "w", display: true },
    ]);
  });

  it("keeps math source byte-identical", () => {
    const { math } = extractMath("Let $a_b * c_d$ and $$\\frac{1}{2}$$.");
    expect(math[0].tex).toBe("a_b * c_d");
    expect(math[1].tex).toBe("\\frac{1}{2}");
  });

  it("does not treat currency-like dollars as math", () => {
    expect(extractMath("costs $5 and $6 total").math).toHaveLength(0);
  });

  it("leaves math in code spans alone", () => {
    expect(extractMath("use `$x$` literally").math).toHaveLength(0);
  });
});

describe("renderMarkdown", () => {
  it("does not turn underscores/asterisks inside math into emphasis", () => {
    const html = renderMarkdown("Here $a_b * c_d$ and $x_1 * y_1$ end.");
    expect(html).not.toContain("<em>");
    expect(html).not.toContain("<strong>");
    expect(html).toContain('class="katex"');
    // the MathML annotation carries the original source
    expect(html).toContain("a_b * c_d");
  });

  it("renders display fractions intact", () => {
    const html = renderMarkdown("$$\\frac{1}{2}$$");
    expect(html).toContain("katex-display");
    expect(html).toContain("\\frac{1}{2}");
    expect(html).toContain("mfrac");
  });

  it("still renders markdown around math", () => {
    const html = renderMarkdown("**bold** and _em_ with $\\alpha$");
    expect(html).toContain("<strong>bold</strong>");
    expect(html).toContain("<em>em</em>");
    expect(html).toContain('class="katex"');
  });

  it("malformed LaTeX does not throw and shows a visible fallback", () => {
    let html = "";
    expect(() => (html = renderMarkdown("bad $\\frac{$ math"))).not.toThrow();
    expect(html).toContain("math-error");
    expect(html).toContain("$\\frac{$");
    expect(html).toContain("bad");
    expect(html).toContain("math");
  });

  it("unmatched dollar stays literal", () => {
    expect(renderMarkdown("price $ alone")).toContain("price $ alone");
  });

  it("escapes raw HTML from untrusted content", () => {
    const html = renderMarkdown("<script>alert(1)</script> $x$");
    expect(html).not.toContain("<script>");
  });

  it("memoizes", () => {
    renderMarkdown("memo $q$");
    const before = _cacheSizes().block;
    renderMarkdown("memo $q$");
    expect(_cacheSizes().block).toBe(before);
  });
});

describe("renderMath / renderInline", () => {
  it("renderMath never throws on garbage", () => {
    expect(() => renderMath("\\begin{align", true)).not.toThrow();
    expect(renderMath("\\begin{align", true)).toContain("math-error");
  });
  it("renderInline has no paragraph wrapper", () => {
    const h = renderInline("Term $x_i$");
    expect(h.startsWith("<p>")).toBe(false);
    expect(h).toContain("katex");
  });
});

describe("inline math punctuation", () => {
  it("keeps a period after inline math on the same line", () => {
    const html = renderMarkdown("at a specific point $p$. The set");
    expect(html).toMatch(/<span class="math-nowrap"><span class="katex">[\s\S]*<\/span>\.<\/span> The set/);
    expect(renderMarkdown("$p$ and $q$")).not.toContain("math-nowrap");
  });
});

describe("excerpt", () => {
  it("never cuts inside a formula", () => {
    const s = "Use electron volts ($1 \\text{ eV} = 1.6 \\times 10^{-12}$ erg) for the mass";
    const e = excerpt(s, 40);
    expect((e.match(/\$/g) ?? []).length % 2).toBe(0);
    expect(e.endsWith("…")).toBe(true);
    expect(excerpt("short", 40)).toBe("short");
  });
});
