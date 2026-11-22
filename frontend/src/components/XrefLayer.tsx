import { useEffect, useState, type RefObject } from "react";
import { getXrefIndex, resolveRef, type XrefKind } from "../lib/xrefs";
import { Equation, Md } from "./Md";

interface Hover {
  kind: XrefKind;
  ref: string;
  x: number;
  y: number;
}

/**
 * Click and hover behaviour for cross-reference links (`a.xref`) inside `root`: a click jumps to the target
 * (loading its section if needed, raising the level if the target is hidden); hovering shows the equation,
 * figure or section title without leaving the page.
 */
export function XrefLayer({ root, go }: { root: RefObject<HTMLElement>; go: (sectionId: string, blockId?: string) => void }) {
  const [hover, setHover] = useState<Hover | null>(null);

  useEffect(() => {
    const el = root.current;
    if (!el) return;
    const find = (e: Event) => (e.target as HTMLElement | null)?.closest?.("a.xref") as HTMLAnchorElement | null;
    const parse = (a: HTMLAnchorElement) => {
      const [kind, ref] = (a.dataset.xref ?? "").split(":") as [XrefKind, string];
      return { kind, ref };
    };
    const onClick = (e: MouseEvent) => {
      const a = find(e);
      if (!a || e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
      const { kind, ref } = parse(a);
      const r = resolveRef(kind, ref);
      if (!r) return;
      e.preventDefault();
      setHover(null);
      go(r.sectionId, r.blockId);
    };
    let timer = 0;
    const onOver = (e: MouseEvent) => {
      const a = find(e);
      if (!a) return;
      window.clearTimeout(timer);
      const { kind, ref } = parse(a);
      const box = a.getBoundingClientRect();
      timer = window.setTimeout(() => setHover({ kind, ref, x: box.left, y: box.bottom }), 180);
    };
    const onOut = (e: MouseEvent) => {
      if (!find(e)) return;
      window.clearTimeout(timer);
      timer = window.setTimeout(() => setHover(null), 120);
    };
    el.addEventListener("click", onClick);
    el.addEventListener("mouseover", onOver);
    el.addEventListener("mouseout", onOut);
    return () => {
      window.clearTimeout(timer);
      el.removeEventListener("click", onClick);
      el.removeEventListener("mouseover", onOver);
      el.removeEventListener("mouseout", onOut);
    };
  }, [root, go]);

  if (!hover) return null;
  const idx = getXrefIndex();
  if (!idx) return null;
  let body: JSX.Element | null = null;
  if (hover.kind === "eq") {
    const t = idx.equations[hover.ref];
    if (t) body = t.latex ? <Equation latex={t.latex} eqNumber={hover.ref} /> : <p className="muted">Equation ({hover.ref}), p. {t.page}</p>;
  } else if (hover.kind === "fig") {
    const t = idx.figures[hover.ref];
    if (t)
      body = (
        <>
          {t.image_url && <img src={t.image_url} alt={`Figure ${hover.ref}`} />}
          <Md src={t.caption} className="small" />
        </>
      );
  } else {
    const t = hover.kind === "sec" ? idx.sections[hover.ref] : idx.chapters[hover.ref];
    if (t) body = <p><strong>{hover.kind === "sec" ? `§${hover.ref}` : `Chapter ${hover.ref}`}</strong> {t.title}</p>;
  }
  if (!body) return null;
  const left = Math.max(8, Math.min(hover.x, window.innerWidth - 440));
  return (
    <div className="xref-preview" role="tooltip" style={{ left, top: hover.y + 6 }}>
      {body}
      <div className="xref-hint muted small">Click to jump there</div>
    </div>
  );
}
