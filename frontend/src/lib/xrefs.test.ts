import { afterEach, describe, expect, it } from "vitest";
import { renderMarkdown } from "./markdown";
import { setXrefs } from "./xrefs";
import type { Xrefs } from "../api/types";

const index: Xrefs = {
  equations: { "2.13": { block_id: "b-eq", section_id: "s2.1", page: 40, latex: "a=b" } },
  figures: { "1.5": { block_id: "b-fig", section_id: "s1.4", page: 20, caption: "FIGURE 1.5 A light cone", image_url: null } },
  sections: { "2.4": { section_id: "s2.4", title: "Tangent spaces" } },
  chapters: { "3": { section_id: "s3.1", title: "Curvature" } },
  unresolved: [],
};

describe("cross-reference links", () => {
  afterEach(() => setXrefs(null));

  it("links known equations, figures, sections and chapters", () => {
    setXrefs(index);
    const html = renderMarkdown("Using (2.13) and Figure 1.5, see Section 2.4 and Chapter 3.");
    expect(html).toContain('data-xref="eq:2.13"');
    expect(html).toContain('data-xref="fig:1.5"');
    expect(html).toContain('data-xref="sec:2.4"');
    expect(html).toContain('data-xref="ch:3"');
  });

  it("leaves unknown numbers, math and code alone", () => {
    setXrefs(index);
    const html = renderMarkdown("A ratio (0.5), math $(2.13)$ and code `(2.13)`.");
    expect(html).not.toContain("xref");
  });

  it("re-renders when the index arrives", () => {
    const before = renderMarkdown("see (2.13)");
    expect(before).not.toContain("xref");
    setXrefs(index);
    expect(renderMarkdown("see (2.13)")).toContain("xref");
  });
});
