import type { TaskGroup } from "../api/types";
import { modelLabel, useModelChoice, useModelsReport } from "../lib/models";

const KIND_NOTE: Record<string, string> = { local: "", subscription: " · subscription", api: " · paid API" };

/** Compact model selector for one task group; "Auto" follows Settings and shows what it resolves to. */
export function ModelPicker({ group, compact = false, label = "AI model for this task" }: { group: TaskGroup; compact?: boolean; label?: string }) {
  const report = useModelsReport();
  const [value, setValue] = useModelChoice(group);
  const g = report?.groups.find((x) => x.id === group);
  const models = (report?.models ?? []).filter((m) => !g?.vision || m.vision);
  return (
    <label className={`model-picker${compact ? " compact" : ""}`} title="Which AI model answers this">
      <span className="model-picker-label">Model</span>
      <select value={value} onChange={(e) => setValue(e.target.value)} aria-label={label}>
        <option value="auto">Auto{g ? ` · ${modelLabel(report, g.auto)}` : ""}</option>
        {models.map((m) => (
          <option key={m.id} value={m.id} disabled={!m.available}>
            {m.label}
            {m.available ? KIND_NOTE[m.kind] : " (not available)"}
          </option>
        ))}
      </select>
    </label>
  );
}
