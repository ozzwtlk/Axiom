import { useEffect, useRef, useState, type CSSProperties } from "react";
import type { AskAction } from "../api/types";

export interface SelectionSnapshot {
  text: string;
  sectionId: string;
  blockIds: string[];
  rect: { top: number; bottom: number; left: number; width: number };
}

const ACTIONS: { action: AskAction; label: string }[] = [
  { action: "explain", label: "Explain" },
  { action: "simpler", label: "Simpler" },
  { action: "steps", label: "Steps" },
  { action: "why", label: "Why?" },
  { action: "physical", label: "Physical meaning" },
  { action: "example", label: "Example" },
  { action: "notation", label: "Notation" },
  { action: "prerequisites", label: "Prerequisites" },
];

/**
 * Plain text of a range with KaTeX turned back into its TeX source ($..$ / $$..$$) instead of the
 * duplicated HTML+MathML glyph text that Selection.toString() yields. UI chrome is dropped.
 */
export function selectionText(range: Range): string {
  const frag = range.cloneContents();
  const texOf = (el: Element) => el.querySelector('annotation[encoding="application/x-tex"]')?.textContent ?? null;
  frag.querySelectorAll(".katex-display").forEach((el) => {
    const t = texOf(el);
    if (t !== null) el.replaceWith(`\n$$${t}$$\n`);
  });
  frag.querySelectorAll(".katex").forEach((el) => {
    const t = texOf(el);
    if (t !== null) el.replaceWith(`$${t}$`);
  });
  const chrome = ".katex-mathml, .unit-meta, .source-panel, .kind-icon, .equation-number, .ai-tag";
  frag.querySelectorAll(chrome).forEach((el) => el.parentNode?.removeChild(el));
  frag.querySelectorAll("p, li, div.unit, .equation, h2, h3").forEach((el) => el.append("\n"));
  return (frag.textContent ?? "")
    .replace(/[ \t]+/g, " ")
    .replace(/\s*\n\s*/g, "\n")
    .trim();
}

/** Capture the current selection if it lies inside `root`. Block ids come from the DOM units it touches. */
export function snapshotSelection(root: HTMLElement): SelectionSnapshot | null {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) return null;
  const range = sel.getRangeAt(0);
  if (!root.contains(range.commonAncestorContainer)) return null;
  const text = selectionText(range);
  if (text.length < 2) return null;

  const blockIds = new Set<string>();
  let sectionId: string | null = null;
  root.querySelectorAll<HTMLElement>("[data-block-ids]").forEach((el) => {
    if (range.intersectsNode(el)) {
      (el.dataset.blockIds ?? "").split(" ").filter(Boolean).forEach((b) => blockIds.add(b));
      if (!sectionId) sectionId = el.dataset.sectionId ?? null;
    }
  });
  if (!sectionId) {
    const startEl =
      range.startContainer instanceof Element ? range.startContainer : range.startContainer.parentElement;
    sectionId = startEl?.closest<HTMLElement>("[data-section-id]")?.dataset.sectionId ?? null;
  }
  if (!sectionId) return null;
  const r = range.getBoundingClientRect();
  return {
    text: text.slice(0, 4000),
    sectionId,
    blockIds: [...blockIds],
    rect: { top: r.top, bottom: r.bottom, left: r.left, width: r.width },
  };
}

interface Props {
  snap: SelectionSnapshot;
  onAsk: (action: AskAction, question?: string) => void;
  onClose: () => void;
}

export function AskToolbar({ snap, onAsk, onClose }: Props) {
  const [custom, setCustom] = useState("");
  const [showCustom, setShowCustom] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const coarse = typeof window !== "undefined" && window.matchMedia?.("(pointer: coarse)").matches;

  // Position: above selection on desktop, below on touch (native selection menu sits above).
  const W = Math.min(440, window.innerWidth - 16);
  const left = Math.max(8, Math.min(window.innerWidth - W - 8, snap.rect.left + snap.rect.width / 2 - W / 2));
  const above = !coarse && snap.rect.top > 120;
  const style: CSSProperties = above
    ? { left, width: W, bottom: window.innerHeight - snap.rect.top + 8 }
    : { left, width: W, top: Math.min(window.innerHeight - 140, snap.rect.bottom + 12) };

  useEffect(() => {
    const fn = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", fn);
    return () => window.removeEventListener("keydown", fn);
  }, [onClose]);

  return (
    <div
      ref={ref}
      className="ask-toolbar"
      style={style}
      role="toolbar"
      aria-label="Ask AI about selection"
      // keep the text selection alive when clicking buttons
      onMouseDown={(e) => {
        if ((e.target as HTMLElement).tagName !== "INPUT") e.preventDefault();
      }}
    >
      <div className="ask-toolbar-row">
        <span className="ask-toolbar-title">Ask AI</span>
        {ACTIONS.map((a) => (
          <button type="button" key={a.action} onClick={() => onAsk(a.action)}>
            {a.label}
          </button>
        ))}
        <button type="button" onClick={() => setShowCustom((s) => !s)} aria-expanded={showCustom}>
          Ask…
        </button>
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Close">
          ×
        </button>
      </div>
      {showCustom && (
        <form
          className="ask-custom"
          onSubmit={(e) => {
            e.preventDefault();
            if (custom.trim()) onAsk("custom", custom.trim());
          }}
        >
          <input
            autoFocus
            value={custom}
            onChange={(e) => setCustom(e.target.value)}
            placeholder="Your question about the selection…"
          />
          <button type="submit" disabled={!custom.trim()}>
            Ask
          </button>
        </form>
      )}
    </div>
  );
}
