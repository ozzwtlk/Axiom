import { useCallback, useEffect, useState } from "react";
import { listModels } from "../api/client";
import type { ModelsReport, TaskGroup } from "../api/types";
import { load, save } from "./storage";

/** One request per page load; Settings calls refreshModels() after saving. */
let report: Promise<ModelsReport> | null = null;
const listeners = new Set<() => void>();

export function modelsReport(): Promise<ModelsReport> {
  if (!report) report = listModels().catch(() => ({ models: [], groups: [] }));
  return report;
}

export function refreshModels(): void {
  report = null;
  listeners.forEach((f) => f());
}

export function useModelsReport(): ModelsReport | null {
  const [r, setR] = useState<ModelsReport | null>(null);
  useEffect(() => {
    let alive = true;
    const pull = () => modelsReport().then((x) => alive && setR(x));
    pull();
    listeners.add(pull);
    return () => {
      alive = false;
      listeners.delete(pull);
    };
  }, []);
  return r;
}

const key = (g: TaskGroup) => `axiom.model.${g}`;

/** The model chosen for a task group in this browser ("auto" = follow Settings). */
export function getModelChoice(g: TaskGroup): string {
  return load<string>(key(g), "auto");
}

export function useModelChoice(g: TaskGroup): [string, (m: string) => void] {
  const [m, setM] = useState(() => getModelChoice(g));
  const set = useCallback(
    (v: string) => {
      setM(v);
      save(key(g), v);
    },
    [g],
  );
  return [m, set];
}

export function modelLabel(r: ModelsReport | null, id: string | null | undefined): string {
  if (!id) return "";
  const m = r?.models.find((x) => x.id === id || (id === "local" && x.kind === "local"));
  return m?.label ?? id;
}
