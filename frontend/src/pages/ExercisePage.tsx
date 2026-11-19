import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getExercise, getHint, pageImageUrl } from "../api/client";
import type { Exercise, HintResponse } from "../api/types";
import { Md } from "../components/Md";
import { ModelPicker } from "../components/ModelPicker";
import { SolutionPanel } from "../components/SolutionPanel";
import { getModelChoice } from "../lib/models";
import { SourcePanel } from "../components/SourcePanel";
import { exKey, load, save, type ExerciseMark } from "../lib/storage";

export function ExercisePage() {
  const { id = "" } = useParams();
  const [ex, setEx] = useState<Exercise | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [scratch, setScratch] = useState(() => load<string>(exKey(id, "scratch"), ""));
  const [hints, setHints] = useState<HintResponse[]>(() => load<HintResponse[]>(exKey(id, "hints"), []));
  const [mark, setMark] = useState<ExerciseMark>(() =>
    load<ExerciseMark>(exKey(id, "mark"), { completed: false, difficult: false }),
  );
  const [hintLoading, setHintLoading] = useState(false);
  const [hintErr, setHintErr] = useState<string | null>(null);
  const [showSource, setShowSource] = useState(false);
  const saveTimer = useRef(0);

  useEffect(() => {
    setScratch(load<string>(exKey(id, "scratch"), ""));
    setHints(load<HintResponse[]>(exKey(id, "hints"), []));
    setMark(load<ExerciseMark>(exKey(id, "mark"), { completed: false, difficult: false }));
    getExercise(id)
      .then((e) => (setEx(e), setErr(null)))
      .catch((e) => setErr(`Could not load exercise: ${e.message ?? e}`));
  }, [id]);

  const onScratch = (v: string) => {
    setScratch(v);
    window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => save(exKey(id, "scratch"), v), 400);
  };
  useEffect(() => () => window.clearTimeout(saveTimer.current), []);

  const max = ex?.hints_available ?? 5;
  const nextLevel = hints.length + 1;

  const requestHint = async () => {
    if (nextLevel > max || hintLoading) return;
    setHintLoading(true);
    setHintErr(null);
    try {
      // Only ever the next level — never skip ahead to the solution.
      const h = await getHint(id, nextLevel, getModelChoice("hint"));
      const list = [...hints, { ...h, level: nextLevel }];
      setHints(list);
      save(exKey(id, "hints"), list);
    } catch (e) {
      setHintErr(`Hint failed: ${(e as Error).message ?? e}`);
    } finally {
      setHintLoading(false);
    }
  };

  const toggleMark = (k: keyof ExerciseMark) => {
    const m = { ...mark, [k]: !mark[k] };
    setMark(m);
    save(exKey(id, "mark"), m);
  };

  if (err) return <main className="page"><p className="error">{err}</p><Link to="/">← Library</Link></main>;
  if (!ex) return <main className="page"><p className="muted">Loading…</p></main>;

  const backSection = ex.related_section_ids[0];
  return (
    <main className="page page-wide exercise-page">
      <nav className="crumbs">
        <Link to={`/doc/${ex.document_id}`}>Book</Link>
        {backSection && (
          <>
            <span aria-hidden="true">/</span>
            <Link to={`/read/${backSection}`}>Back to reading</Link>
          </>
        )}
      </nav>
      <h1>
        Exercise {ex.number} <span className="muted small">Chapter {ex.chapter_number} · p. {ex.page}</span>
        {mark.completed && <span className="tag tag-done">✓ completed</span>}
      </h1>

      <div className="exercise-grid">
        <div>
          <div className="exercise-statement">
            <Md src={ex.statement_md} />
            <div className="unit-meta">
              <span className="meta-quiet">
                <button type="button" className="source-btn" onClick={() => setShowSource((s) => !s)} aria-expanded={showSource}>
                  original
                </button>
                <a className="source-btn" href={pageImageUrl(ex.document_id, ex.page)} target="_blank" rel="noreferrer">
                  full page {ex.page}
                </a>
              </span>
            </div>
            {showSource && <SourcePanel source={{ block_ids: ex.block_ids, pages: [ex.page], coverage: "preserved", note: null }} />}
          </div>

          <div className="exercise-marks">
            <label>
              <input type="checkbox" checked={mark.completed} onChange={() => toggleMark("completed")} /> Completed
            </label>
            <label>
              <input type="checkbox" checked={mark.difficult} onChange={() => toggleMark("difficult")} /> Difficult
            </label>
          </div>

          <label className="scratch-label" htmlFor="scratch">
            Your work <span className="muted small">saved in this browser · $math$ renders in the preview</span>
          </label>
          <textarea
            id="scratch"
            className="scratchpad"
            value={scratch}
            onChange={(e) => onScratch(e.target.value)}
            onBlur={() => save(exKey(id, "scratch"), scratch)}
            rows={12}
            spellCheck={false}
            placeholder="Work the problem here…"
          />
          {scratch.includes("$") && (
            <details className="scratch-preview">
              <summary>Preview</summary>
              <Md src={scratch} />
            </details>
          )}
        </div>

        <div className="side-col">
          <section className="hints">
            <div className="hints-head">
              <h2>Hints</h2>
              <ModelPicker group="hint" compact />
              <button type="button" className="btn btn-sm" onClick={requestHint} disabled={hintLoading || nextLevel > max}>
                {hintLoading ? "Thinking…" : nextLevel > max ? "No more hints" : `Hint ${nextLevel} of ${max}`}
              </button>
            </div>
            <p className="ai-disclaimer">
              AI-generated, one level at a time. Try the next step yourself before asking.
            </p>
            {hintErr && <p className="error">{hintErr}</p>}
            {hints.length > 0 && (
              <ol className="hint-list">
                {hints.map((h) => (
                  <li key={h.level} className="hint">
                    <div className="hint-head">
                      Hint {h.level} <span className="muted small">· {h.model}{h.cached ? " · cached" : ""}</span>
                    </div>
                    <Md src={h.hint_md} />
                  </li>
                ))}
              </ol>
            )}
            {hints.length > 0 && (
              <button
                type="button"
                className="link-btn small"
                style={{ marginTop: "0.6rem" }}
                onClick={() => {
                  setHints([]);
                  save(exKey(id, "hints"), []);
                }}
              >
                Hide hints and start over
              </button>
            )}
          </section>

          <SolutionPanel exerciseId={id} answer={scratch} />
        </div>
      </div>
    </main>
  );
}
