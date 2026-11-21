import { memo, useSyncExternalStore } from "react";
import { renderInline, renderMarkdown, renderMath } from "../lib/markdown";
import { subscribeXrefs, xrefVersion } from "../lib/xrefs";

/** Re-render when the cross-reference index arrives (references become links). */
function useXrefs() {
  useSyncExternalStore(subscribeXrefs, xrefVersion);
}

/** Block Markdown + math, rendered from the memoized cache. */
export const Md = memo(function Md({ src, className }: { src: string | null | undefined; className?: string }) {
  useXrefs();
  return <div className={className ? `md ${className}` : "md"} dangerouslySetInnerHTML={{ __html: renderMarkdown(src) }} />;
});

export const MdInline = memo(function MdInline({ src, className }: { src: string | null | undefined; className?: string }) {
  useXrefs();
  return <span className={className} dangerouslySetInnerHTML={{ __html: renderInline(src) }} />;
});

/** Display equation with right-aligned number "(1.29)". */
export const Equation = memo(function Equation({ latex, eqNumber }: { latex: string; eqNumber: string | null }) {
  return (
    <div className="equation">
      <div className="equation-math" dangerouslySetInnerHTML={{ __html: renderMath(latex, true) }} />
      {eqNumber && <span className="equation-number">({eqNumber})</span>}
    </div>
  );
});
