import { useEffect, useState } from "react";
import { getRouting, saveRouting } from "../api/client";
import type { ModelInfo, Routing, TaskGroup } from "../api/types";
import { modelLabel, refreshModels, useModelsReport } from "../lib/models";

const KIND: Record<string, string> = { local: "On this computer", subscription: "Your subscription", api: "Paid API" };

export function SettingsPage() {
  const report = useModelsReport();
  const [routing, setRouting] = useState<Routing | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  useEffect(() => {
    getRouting().then(setRouting).catch((e) => setErr(String(e.message ?? e)));
  }, []);

  const apply = async (update: Parameters<typeof saveRouting>[0], what: string) => {
    setErr(null);
    try {
      setRouting(await saveRouting(update));
      refreshModels();
      setSaved(what);
      window.setTimeout(() => setSaved((s) => (s === what ? null : s)), 2000);
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const models = report?.models ?? [];
  const premium = models.filter((m) => m.kind !== "local");

  return (
    <main className="page page-wide settings-page">
      <div className="page-head">
        <div>
          <span className="eyebrow">Settings</span>
          <h1>AI models</h1>
        </div>
        {saved && <span className="tag tag-done">✓ saved: {saved}</span>}
      </div>
      {err && <p className="error">{err}</p>}

      <section className="settings-grid">
        <div className="card side-card">
          <h3>Available models</h3>
          <ul className="model-list">
            {models.map((m) => (
              <ModelRow key={m.id} m={m} />
            ))}
            {!report && <li className="muted">Checking…</li>}
          </ul>
          <p className="small muted">
            Claude Code and Codex run through their own command-line tools with your subscription login — no API keys.
            They run with every tool switched off, in an empty folder, so text from a book can never make them run
            commands or read files. Subscriptions have usage limits; AXIOM caps each at a daily number of calls.
          </p>
        </div>

        <div className="card side-card">
          <h3>Cloud models</h3>
          {routing && (
            <>
              <label className="switch-row big">
                <span>
                  <strong>Let “Auto” use Claude Code / Codex</strong>
                  <span className="muted">
                    Short reasoning jobs (questions, hints, solutions, answer checks, quizzes, visualizations) then go to
                    your subscription models; the selected passage and its context are sent to Anthropic or OpenAI.
                    Long bulk jobs always stay on the local model unless you change them below.
                  </span>
                </span>
                <input
                  type="checkbox"
                  checked={routing.cloud_enabled}
                  onChange={(e) => apply({ cloud_enabled: e.target.checked }, "cloud models")}
                />
              </label>
              <hr className="divider" />
              <label className="field">
                Preferred premium model for “Auto”
                <select
                  value={routing.premium_default}
                  onChange={(e) => apply({ premium_default: e.target.value }, "preferred model")}
                >
                  <option value="auto">Automatic (Claude Code · Sonnet, then Codex)</option>
                  {premium.map((m) => (
                    <option key={m.id} value={m.id} disabled={!m.available}>
                      {m.label}
                      {m.available ? "" : " (not available)"}
                    </option>
                  ))}
                </select>
              </label>
            </>
          )}
        </div>
      </section>

      <section className="card routing-card">
        <h3>Who does what</h3>
        <p className="small muted">
          “Auto” keeps token-heavy work local and sends short reasoning tasks to a premium model when cloud models are
          on. You can also switch the model right next to each AI button; that choice wins over this table.
        </p>
        <table className="routing-table">
          <thead>
            <tr>
              <th>Task</th>
              <th>Model</th>
              <th className="hide-sm">Auto uses now</th>
            </tr>
          </thead>
          <tbody>
            {report?.groups.map((g) => (
              <tr key={g.id}>
                <td>
                  <strong>{g.label}</strong> {g.heavy && <span className="tag">token-heavy</span>}
                  {g.help && <div className="small muted">{g.help}</div>}
                </td>
                <td>
                  <select
                    value={routing?.groups[g.id as TaskGroup] ?? "auto"}
                    onChange={(e) => apply({ groups: { [g.id]: e.target.value } }, g.label)}
                    aria-label={`Model for ${g.label}`}
                  >
                    <option value="auto">Auto</option>
                    {models
                      .filter((m) => !g.vision || m.vision)
                      .map((m) => (
                        <option key={m.id} value={m.id} disabled={!m.available}>
                          {m.label}
                          {m.available ? "" : " (not available)"}
                        </option>
                      ))}
                  </select>
                </td>
                <td className="hide-sm muted small">{modelLabel(report, g.auto)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </main>
  );
}

function ModelRow({ m }: { m: ModelInfo }) {
  return (
    <li className="model-row">
      <span className={`status-dot ${m.available ? "on" : "off"}`} aria-hidden="true" />
      <span className="model-row-main">
        <strong>{m.label}</strong>
        <span className="small muted">
          {KIND[m.kind]}
          {m.detail ? ` · ${m.detail}` : ""}
          {m.calls_today != null ? ` · ${m.calls_today}/${m.daily_limit} calls today` : ""}
        </span>
      </span>
      <span className={`tag ${m.available ? "tag-done" : ""}`}>{m.available ? "ready" : "unavailable"}</span>
    </li>
  );
}
