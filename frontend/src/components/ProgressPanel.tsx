import type { ProgressReport } from "../api/types";

export function ProgressPanel({ p, polling }: { p: ProgressReport; polling: boolean }) {
  const hitRate = p.llm_calls + p.llm_cache_hits > 0 ? p.llm_cache_hits / (p.llm_calls + p.llm_cache_hits) : 0;
  return (
    <section className="progress-panel" aria-label="Processing">
      <div className="progress-head">
        <h2>Processing</h2>
        <span className="muted small">
          {polling ? (
            <>
              <span className="live-dot" aria-hidden="true" />
              live
            </>
          ) : (
            "idle"
          )}
        </span>
      </div>
      <p className="small">
        {p.sections_done}/{p.sections_total} sections done
        {p.sections_failed > 0 && <span className="error"> · {p.sections_failed} failed</span>} · {p.pages_total} pages
      </p>
      <div className="stages">
        {p.stages.map((s) => (
          <div className="stage" key={s.stage}>
            <span className="stage-name">{s.stage}</span>
            <div className="bar" role="progressbar" aria-valuemin={0} aria-valuemax={s.total} aria-valuenow={s.done}>
              <div style={{ width: `${s.total ? (100 * s.done) / s.total : 0}%` }} />
            </div>
            <span className="stage-count">
              {s.done}/{s.total}
            </span>
          </div>
        ))}
      </div>
      <dl className="metrics">
        <div>
          <dt>LLM calls <span className="measured">measured</span></dt>
          <dd>{p.llm_calls}</dd>
        </div>
        <div>
          <dt>Cache hits <span className="measured">measured</span></dt>
          <dd>
            {p.llm_cache_hits} <span className="muted small">({Math.round(hitRate * 100)}%)</span>
          </dd>
        </div>
        <div>
          <dt>Local compute <span className="measured">measured</span></dt>
          <dd>{formatSeconds(p.local_seconds)}</dd>
        </div>
        <div>
          <dt>Premium calls <span className="measured">measured</span></dt>
          <dd>
            {p.premium_calls}
            {p.premium_tokens > 0 && <span className="muted small"> · {p.premium_tokens.toLocaleString()} tokens</span>}
          </dd>
        </div>
      </dl>
      {p.jobs.length > 0 && (
        <details className="jobs">
          <summary>Jobs ({p.jobs.length})</summary>
          <ul>
            {p.jobs.map((j, i) => (
              <li key={String(j.id ?? i)}>
                <code>{String(j.id ?? i)}</code> {String(j.status ?? "")}
                {j.stage ? <span className="muted"> · {String(j.stage)}</span> : null}
                {j.error ? <span className="error"> · {String(j.error)}</span> : null}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

function formatSeconds(s: number): string {
  if (s < 60) return `${s.toFixed(1)} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${Math.round(s % 60)} s`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

const ACTIVE = new Set(["queued", "pending", "running", "processing", "started"]);
export function jobsRunning(p: ProgressReport | null): boolean {
  return !!p && p.jobs.some((j) => ACTIVE.has(String(j.status ?? "").toLowerCase()));
}
