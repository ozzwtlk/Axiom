import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { changeJob, listJobs, removeJob, type QueuedJob } from "../api/client";
import { useModelsReport } from "../lib/models";

export function JobsPage() {
  const [jobs, setJobs] = useState<QueuedJob[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [history, setHistory] = useState(30);
  const report = useModelsReport();
  useEffect(() => {
    let current = true;
    const poll = async () => {
      try {
        const result = await listJobs();
        if (current) setJobs(result.jobs);
      } catch (e) {
        if (current) setError((e as Error).message);
      }
    };
    void poll();
    const timer = window.setInterval(poll, 2500);
    return () => { current = false; window.clearInterval(timer); };
  }, []);
  const action = async (id: string, run: () => Promise<void>) => {
    setBusy(id);
    setError(null);
    try {
      await run();
      setJobs((await listJobs()).jobs);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };
  const running = jobs?.filter((j) => j.status === "running") ?? [];
  const pending = jobs?.filter((j) => j.status === "pending") ?? [];
  const past = jobs?.filter((j) => j.status !== "pending" && j.status !== "running") ?? [];
  const byId = new Map(jobs?.map((job) => [job.id, job]));
  const render = (j: QueuedJob, i?: number) => {
    const blocked = j.depends_on.filter((id) => {
      const job = byId.get(id.replace(/^~/, ""));
      return id.startsWith("~") ? job?.status === "running" || job?.status === "pending" : job?.status !== "done";
    });
    const choices = (report?.models ?? []).filter((m) => j.kind === "cover" ? m.id.startsWith("codex:") : j.kind !== "transcribe" || m.vision);
    return <li className="card queue-job" key={j.id}>
      <div>
        <strong>{i !== undefined && `${i + 1}. `}{j.kind}</strong> · {j.status}
        {!!j.parallel && <span className="tag">Parallel run</span>}
        <div className="small">
          {j.title ? <Link to={`/doc/${j.doc_id}`}>{j.title}</Link> : "PDF inbox"} · <code>{j.target_id}</code>
        </div>
        <div className="small muted">
          <code>{j.id}</code>{j.worker ? ` · ${j.worker}` : ""}{j.model_group && ` · ${j.model ?? "AI model settings"}`}
        </div>
        {!!blocked.length && j.status === "pending" && <div className="small muted">
          Depends on {blocked.map((dep) => dep.startsWith("~") ? `${dep.slice(1)} (order only)` : dep).join(", ")}
        </div>}
        {j.error && <p className="small error">{j.error.split("\n")[0]}</p>}
      </div>
      {j.status === "pending" && <div className="queue-actions">
        {j.model_group && <label className="field small">Model for {j.id}
          <select value={j.model ?? ""} disabled={busy !== null}
                  onChange={(event) => { const model = event.target.value; void action(j.id, () => changeJob(j.id, { action: "model", model })); }}>
            {j.kind !== "cover" && <option value="">AI model settings</option>}
            {j.kind !== "cover" && !choices.some((m) => m.id === "local") && <option value="local">Local model</option>}
            {j.model && j.model !== "local" && !choices.some((m) => m.id === j.model) && <option value={j.model}>{j.model}</option>}
            {choices.map((m) => <option key={m.id} value={m.id}>{m.label}{m.available ? "" : " (offline)"}</option>)}
          </select>
        </label>}
        <button type="button" className="btn btn-sm btn-quiet" disabled={busy !== null || i === 0}
                onClick={() => action(j.id, () => changeJob(j.id, { action: "move_first" }))}>Move to front</button>
        <button type="button" className="link-btn small" disabled={busy !== null} onClick={() => {
          if (window.confirm("Remove this waiting job and cancel any waiting jobs that require its output?")) {
            void action(j.id, () => removeJob(j.id));
          }
        }}>Remove</button>
      </div>}
    </li>;
  };
  return <main className="page page-wide">
    <div className="page-head"><div><span className="eyebrow">Compute</span><h1>Jobs</h1></div></div>
    <p className="muted">Waiting jobs are shown in priority order. Workers can skip blocked jobs and run eligible work concurrently. To enable parallel chapter processing, select a compatible premium model when queuing chapters from a book.</p>
    {error && <p className="error" role="alert">{error}</p>}
    {jobs === null ? <p className="muted">Loading…</p> : <>
      <h2>Running ({running.length})</h2>
      {running.length ? <ul className="queue-list">{running.map((j) => render(j))}</ul> : <p className="muted">No running jobs.</p>}
      <h2>Waiting ({pending.length})</h2>
      {pending.length ? <ol className="queue-list">{pending.map(render)}</ol> : <p className="muted">Queue is empty.</p>}
      {!!past.length && <details>
        <summary>History ({past.length})</summary>
        <ul className="queue-list">{past.slice(0, history).map((j) => render(j))}</ul>
        {past.length > history && <button className="btn btn-sm" onClick={() => setHistory((n) => n + 30)}>Show more</button>}
      </details>}
    </>}
  </main>;
}
