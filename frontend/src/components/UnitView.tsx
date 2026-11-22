import { memo, useState } from "react";
import type { Flag, Level, NoteKind, StudyNote, Unit, UnitKind } from "../api/types";
import { Equation, Md, MdInline } from "./Md";
import { SourcePanel } from "./SourcePanel";
import { VizPanel } from "./VizPanel";
import { Icon } from "./Icon";

/** Text label + glyph per kind, so colour is never the only signal. */
const KIND_META: Partial<Record<UnitKind, { label: string; icon: string }>> = {
  definition: { label: "Definition", icon: "≔" },
  theorem: { label: "Theorem", icon: "∴" },
  result: { label: "Result", icon: "◆" },
  warning: { label: "Caution", icon: "!" },
  example: { label: "Example", icon: "▸" },
  remark: { label: "Remark", icon: "※" },
  aside: { label: "Aside", icon: "…" },
  figure: { label: "Figure", icon: "▭" },
  exercise_ref: { label: "Exercise", icon: "✎" },
};

const FLAG_LABEL: Record<Flag["kind"], string> = {
  low_confidence: "low confidence",
  ambiguous_notation: "notation",
  verification_failed: "verification failed",
  ocr_uncertain: "OCR uncertain",
  info: "info",
  unverified_claim: "unconfirmed claim",
  reverted: "reverted to original",
};

const NOTE_META: Record<NoteKind, { label: string; icon: string }> = {
  key: { label: "Key idea", icon: "★" },
  intuition: { label: "Intuition", icon: "◐" },
  pitfall: { label: "Watch out", icon: "!" },
  recall: { label: "Recall", icon: "↺" },
  check: { label: "Check", icon: "✓" },
};

function StudyNotes({ notes }: { notes: StudyNote[] }) {
  return (
    <aside className="study-notes" aria-label="Study notes (AI-written)">
      {notes.map((n, i) => (
        <div key={i} className={`study-note note-${n.kind}`}>
          <span className="note-label">
            <span aria-hidden="true">{NOTE_META[n.kind].icon}</span> {NOTE_META[n.kind].label}
          </span>
          <Md src={n.md} />
        </div>
      ))}
      <span className="ai-tag" title="Study notes written by the AI, not from the book; checked against the source">
        AI notes
      </span>
    </aside>
  );
}

const VIZ_KINDS = new Set<UnitKind>(["equation", "definition", "result", "theorem", "example", "prose"]);

function FlagChip({ flag }: { flag: Flag }) {
  const [open, setOpen] = useState(false);
  return (
    <span className={`flag flag-${flag.kind}`}>
      <button
        type="button"
        className="flag-chip"
        title={flag.message}
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span aria-hidden="true">{flag.kind === "info" ? "i" : "⚑"}</span> {FLAG_LABEL[flag.kind]}
      </button>
      {open && <span className="flag-message">{flag.message}</span>}
    </span>
  );
}

interface Props {
  unit: Unit;
  level: Level;
  sectionId: string;
}

function UnitViewImpl({ unit, level, sectionId }: Props) {
  const [showSource, setShowSource] = useState(false);
  // a saved visualization is part of the text: shown without asking again
  const [showViz, setShowViz] = useState(unit.has_visualization);
  const meta = KIND_META[unit.kind];
  const body = level === 1 ? (unit.fast_md ?? unit.text_md) : unit.text_md;
  const deep = level === 3 ? unit.deep_md : null;
  const usedFast = level === 1 && unit.fast_md != null;
  const isBoxed = !!meta && unit.kind !== "figure" && unit.kind !== "aside" && unit.kind !== "exercise_ref";

  let content: JSX.Element;
  if (unit.kind === "heading") {
    content = (
      <h3 className="unit-heading">
        <MdInline src={unit.title ?? body} />
      </h3>
    );
  } else if (unit.kind === "equation") {
    content = (
      <>
        {unit.latex ? <Equation latex={unit.latex} eqNumber={unit.eq_number} /> : <Md src={body} />}
        {unit.latex && body && <Md src={body} />}
        {unit.annotation_md && level >= 2 && (
          <div className="eq-annotation">
            <span className="ai-tag" title="Short note written by the AI, not from the book">
              AI note
            </span>
            <Md src={unit.annotation_md} />
          </div>
        )}
      </>
    );
  } else if (unit.kind === "figure") {
    content = (
      <figure className="figure">
        {unit.figure_url && <img src={unit.figure_url} alt={unit.title ?? "Figure"} loading="lazy" />}
        <figcaption>
          <Md src={body} />
        </figcaption>
      </figure>
    );
  } else if (unit.kind === "aside") {
    content = (
      <details className="aside-details">
        <summary>
          <span className="kind-label">
            <span className="kind-icon" aria-hidden="true">
              {meta!.icon}
            </span>
            {meta!.label}
          </span>
          {unit.title && (
            <>
              {" · "}
              <MdInline src={unit.title} />
            </>
          )}
        </summary>
        <Md src={body} />
      </details>
    );
  } else {
    content = (
      <>
        {meta && (
          <div className="kind-label">
            <span className="kind-icon" aria-hidden="true">
              {meta.icon}
            </span>
            {meta.label}
            {unit.title && (
              <>
                <span className="kind-sep"> · </span>
                <MdInline src={unit.title} className="kind-title" />
              </>
            )}
          </div>
        )}
        <Md src={body} />
        {unit.kind === "exercise_ref" && (
          <button
            type="button"
            className="link-btn"
            onClick={() => document.getElementById(`exercises-${sectionId}`)?.scrollIntoView({ behavior: "smooth" })}
          >
            See exercises
          </button>
        )}
      </>
    );
  }

  return (
    <div
      className={`unit unit-${unit.kind}${isBoxed ? " unit-boxed" : ""}`}
      id={`unit-${unit.id}`}
      data-unit-id={unit.id}
      data-min-level={unit.min_level}
      data-block-ids={unit.source.block_ids.join(" ")}
      data-section-id={sectionId}
    >
      {content}

      {level >= 2 && unit.notes?.length > 0 && <StudyNotes notes={unit.notes} />}

      {deep && (
        <div className="deep-box">
          <div className="deep-label">
            <Icon name="sparkle" /> Deep Dive · AI-added explanation
          </div>
          <Md src={deep} />
        </div>
      )}

      <div className="unit-meta">
        {unit.flags.map((f, i) => (
          <FlagChip key={i} flag={f} />
        ))}
        <span className="meta-quiet">
          <button
            type="button"
            className={`source-btn cov-${unit.source.coverage}`}
            onClick={() => setShowSource((s) => !s)}
            aria-expanded={showSource}
            title={`Show the original scan · p. ${unit.source.pages.join(", ")} · coverage: ${unit.source.coverage}${unit.source.note ? ` — ${unit.source.note}` : ""}`}
          >
            <Icon name="eye" /> p.{unit.source.pages.join(",") || "?"}
            <span className="cov-text"> · {unit.source.coverage}</span>
          </button>
          {(unit.ai_generated || usedFast) && (
            <span
              className="rewritten"
              title={usedFast ? "Condensed by AI for Fast Track" : "Text rewritten by AI; open the source to compare"}
            >
              {usedFast ? "AI-condensed" : "AI-rewritten"}
            </span>
          )}
          {unit.verification === "verified" && (
            <span
              className="checked"
              title="An independent AI check compared this rewrite with the original text and found no problems"
            >
              ✓ checked
            </span>
          )}
          {(unit.has_visualization || (VIZ_KINDS.has(unit.kind) && level >= 2)) && (
            <button type="button" className="link-btn viz-btn" onClick={() => setShowViz((v) => !v)} aria-expanded={showViz}>
              <Icon name="chart" /> {showViz ? "Hide visualization" : "Visualize"}
            </button>
          )}
        </span>
      </div>

      {showSource && <SourcePanel source={unit.source} />}
      {showViz && <VizPanel unitId={unit.id} />}
    </div>
  );
}

export const UnitView = memo(UnitViewImpl);
