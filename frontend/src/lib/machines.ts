import { useCallback, useEffect, useState } from "react";
import { getMachines, selectMachine } from "../api/client";
import type { MachinesReport } from "../api/types";
import { refreshModels } from "./models";

let cache: MachinesReport | null = null;
const subs = new Set<(r: MachinesReport) => void>();
let timer = 0;

async function poll() {
  try {
    const r = await getMachines();
    const changed = JSON.stringify(r) !== JSON.stringify(cache);
    const machineChanged = cache && cache.effective !== r.effective;
    cache = r;
    if (changed) subs.forEach((f) => f(r));
    if (machineChanged) refreshModels(); // another machine computes now: its models apply
  } catch {
    /* offline: keep the last state */
  }
}

/** Connected computing machines; refreshed every 15 s while any component uses it. */
export function useMachines(): [MachinesReport | null, (name: string) => Promise<void>] {
  const [r, setR] = useState<MachinesReport | null>(cache);
  useEffect(() => {
    subs.add(setR);
    if (!timer) {
      poll();
      timer = window.setInterval(poll, 15000);
    }
    return () => {
      subs.delete(setR);
      if (!subs.size) {
        window.clearInterval(timer);
        timer = 0;
      }
    };
  }, []);
  const choose = useCallback(async (name: string) => {
    cache = await selectMachine(name);
    subs.forEach((f) => f(cache!));
    refreshModels();
  }, []);
  return [r, choose];
}
