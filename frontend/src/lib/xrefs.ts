/**
 * Cross-reference links: "(2.13)", "Figure 1.5", "Section 2.4", "Chapter 3" in rendered text become links to
 * their target, with a hover preview (see XrefPreview in the reader). Only references that exist in the book's
 * index are linked, so "(0.5)" in prose stays plain text.
 *
 * Patterns mirror backend/axiom/verification/xrefs.py — keep them in sync.
 */
import type { Xrefs } from "../api/types";

let index: Xrefs | null = null;
let version = 0;
const listeners = new Set<() => void>();

export function setXrefs(x: Xrefs | null) {
  index = x;
  version++;
  listeners.forEach((l) => l());
}
export function getXrefIndex(): Xrefs | null {
  return index;
}
export function xrefVersion(): number {
  return version;
}
export function subscribeXrefs(l: () => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

export type XrefKind = "eq" | "fig" | "sec" | "ch";

export interface ResolvedRef {
  kind: XrefKind;
  ref: string;
  sectionId: string;
  blockId?: string;
}

export function resolveRef(kind: XrefKind, ref: string): ResolvedRef | null {
  if (!index) return null;
  if (kind === "eq") {
    const t = index.equations[ref];
    return t ? { kind, ref, sectionId: t.section_id, blockId: t.block_id } : null;
  }
  if (kind === "fig") {
    const t = index.figures[ref];
    return t ? { kind, ref, sectionId: t.section_id, blockId: t.block_id } : null;
  }
  const t = kind === "sec" ? index.sections[ref] : index.chapters[ref];
  return t ? { kind, ref, sectionId: t.section_id } : null;
}

// One alternation, so overlapping matches are impossible. Groups: 1 eq, 2 fig, 3 section, 4 chapter.
const REF_RE =
  /\((\d{1,2}\.\d{1,3}[a-z]?)\)|\b(?:Fig(?:ure)?s?\.?)\s+(\d{1,2}\.\d{1,3})|(?:\bSections?|\bSec\.|§)\s*(\d{1,2}\.\d{1,2})\b|\bChapter\s+(\d{1,2})\b/gi;

function anchor(kind: XrefKind, ref: string, text: string): string {
  const r = resolveRef(kind, ref);
  if (!r) return text;
  return `<a class="xref xref-${kind}" href="/read/${encodeURIComponent(r.sectionId)}" data-xref="${kind}:${ref}">${text}</a>`;
}

function linkText(s: string): string {
  return s.replace(REF_RE, (m, eq?: string, fig?: string, sec?: string, ch?: string) => {
    if (eq) return anchor("eq", eq, m);
    if (fig) return anchor("fig", fig, m);
    if (sec) return anchor("sec", sec, m);
    if (ch) return anchor("ch", ch, m);
    return m;
  });
}

/**
 * Link references in rendered HTML. Runs on markdown-it output *before* math is substituted back, so math
 * (still opaque placeholders) is never touched; text inside <a>, <code> and <pre> is left alone.
 */
export function linkifyHtml(html: string): string {
  if (!index) return html;
  let skip = 0;
  return html
    .split(/(<[^>]+>)/)
    .map((part) => {
      if (part.startsWith("<")) {
        const m = /^<(\/?)(a|code|pre)\b/i.exec(part);
        if (m) skip = Math.max(0, skip + (m[1] ? -1 : 1));
        return part;
      }
      return skip ? part : linkText(part);
    })
    .join("");
}
