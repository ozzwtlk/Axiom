import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { getQuality, MOCK, runQuality } from "../api/client";
import type { QualityReport } from "../api/types";

const FIXED: Record<string, string> = {
  page_number: "page numbers removed",
  text_shown_as_image: "text regions no longer shown as images",
  repeated_text: "repeated passages removed",
  latex_repaired: "LaTeX repairs",
  latex_dropped: "unrenderable AI notes removed",
  latex_reverted: "rewrites with broken LaTeX reverted",
  cut_off_text: "cut-off texts fixed",
};

const OPEN: Record<string, string> = {
  not_optimized: "shown verbatim (AI rewrite failed)",
  bad_transcription: "transcriptions to read again",
  unchecked: "not yet fact-checked",
  empty: "without text",
  coverage: "with missing passages",
};

/** Quality control: what the automatic checks fixed, which sections still need a model, and one button for each. */
export function QualityPanel({
  docId, model, profile, tick, onStarted,
}: { docId: string; model: string; profile: string; tick: number; onStarted: () => void }) {
  const [q, setQ] = useState<QualityReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (MOCK) return;
    getQuality(docId).then(setQ).catch(() => setQ(null));
  }, [docId, tick]);

  if (!q || q.sections_processed === 0) return null;
  const go = async (rerun: boolean) => {
    setBusy(true);
    setErr(null);
    try {
      await runQuality(docId, { rerun, profile, ...(model !== "auto" ? { model } : {}) });
      onStarted();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  const fixed = Object.entries(q.fixed).filter(([, n]) => n > 0);
  const openKinds = Object.entries(q.open).filter(([, n]) => n > 0);
  const needsModel = q.sections.filter((s) => s.issues.length || s.status === "partial" || s.status === "failed");
  return (
    <section className="card side-card quality-card" aria-label="Quality check">
      <h3>Quality check</h3>
      <p className="small muted">
        {q.sections_checked < q.sections_processed
          ? `${q.sections_processed - q.sections_checked} of ${q.sections_processed} processed sections not checked yet.`
          : `All ${q.sections_processed} processed sections checked.`}
      </p>
      {fixed.length > 0 && (
        <ul className="quality-list small">
          {fixed.map(([k, n]) => (
            <li key={k}>
              <span className="quality-n ok">{n}</span> {FIXED[k] ?? k}
            </li>
          ))}
        </ul>
      )}
      {needsModel.length > 0 ? (
        <>
          <p className="small">
            <strong>{needsModel.length}</strong> section{needsModel.length === 1 ? "" : "s"} still need the model
            {openKinds.length > 0 && (
              <>
                {": "}
                {openKinds.map(([k, n], i) => (
                  <span key={k}>
                    {i > 0 && ", "}
                    {n} {OPEN[k] ?? k}
                  </span>
                ))}
              </>
            )}
            .{" "}
            <button type="button" className="link-btn" onClick={() => setOpen((o) => !o)}>
              {open ? "hide" : "show"}
            </button>
          </p>
          {open && (
            <ul className="quality-sections small">
              {needsModel.map((s) => (
                <li key={s.section_id}>
                  {s.status === "failed" ? (
                    <span>
                      {s.number} {s.title}
                    </span>
                  ) : (
                    <Link to={`/read/${s.section_id}`}>
                      {s.number} {s.title}
                    </Link>
                  )}
                  <span className="muted">
                    {" — "}
                    {s.issues.length ? s.issues.map((i) => i.message).join("; ") : s.status === "failed" ? "failed" : "not checked yet"}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </>
      ) : (
        q.sections_checked > 0 && <p className="small">Nothing left to fix.</p>
      )}
      <div className="quality-actions">
        <button type="button" className="btn btn-quiet btn-sm" disabled={busy} onClick={() => go(false)}
                title="Run the automatic checks again on every processed section (no model, takes seconds)">
          Check again
        </button>
        {needsModel.length > 0 && (
          <button type="button" className="btn btn-sm" disabled={busy} onClick={() => go(true)}
                  title="Check, then re-run only the parts that need a model, with the model chosen above (the rest comes from the cache)">
            Fix with the model
          </button>
        )}
      </div>
      {err && <p className="error small">{err}</p>}
    </section>
  );
}
