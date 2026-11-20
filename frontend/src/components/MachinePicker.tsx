import { useMachines } from "../lib/machines";

/** Which machine computes (server mode only): shows the effective machine and lets the user switch. */
export function MachinePicker({ compact = false }: { compact?: boolean }) {
  const [r, choose] = useMachines();
  if (!r || r.mode !== "server") return null;
  const online = r.workers.filter((w) => w.online);
  const eff = r.effective;
  const title = eff
    ? `AI requests and processing run on ${eff}`
    : "No computing machine is connected: reading works, AI features wait until one is";
  return (
    <label className={`machine-picker${eff ? " on" : " off"}${compact ? " compact" : ""}`} title={title}>
      <span className="status-dot" aria-hidden="true" />
      <select value={r.selected} onChange={(e) => choose(e.target.value)} aria-label="Computing machine">
        <option value="auto">{eff ? `Auto · ${eff}` : online.length ? "Auto" : "No machine online"}</option>
        {r.workers.map((w) => (
          <option key={w.name} value={w.name}>
            {w.name}
            {w.online ? "" : " (offline)"}
          </option>
        ))}
      </select>
    </label>
  );
}
