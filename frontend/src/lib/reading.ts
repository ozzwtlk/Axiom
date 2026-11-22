import { load, save } from "./storage";

export type ReadingProgress = Record<string, number>;
const key = (docId: string) => `axiom.reading.${docId}`;
export const loadReading = (docId: string) => load<ReadingProgress>(key(docId), {});

export function recordReading(docId: string, updates: ReadingProgress) {
  const prior = loadReading(docId);
  let changed = false;
  for (const [id, percent] of Object.entries(updates)) {
    if (prior[id] === undefined || percent > prior[id]) {
      prior[id] = percent;
      changed = true;
    }
  }
  if (changed) save(key(docId), prior);
}

export function summarizeReading(ids: string[], progress: ReadingProgress) {
  const touched = ids.some((id) => progress[id] !== undefined);
  const finished = ids.length > 0 && ids.every((id) => progress[id] === 100);
  const sum = ids.reduce((n, id) => n + (progress[id] ?? 0), 0);
  return { touched, finished, percent: finished ? 100 : Math.min(99, Math.floor(sum / (ids.length || 1))) };
}

/** Use the bottom of the reading area, but don't count a section merely peeking in at the bottom. */
export function visibleReading(top: number, bottom: number, viewport: number): number | null {
  if (top > viewport * 0.5 || bottom <= 72 || bottom <= top) return null;
  return Math.min(100, Math.floor(100 * (viewport - 16 - top) / (bottom - top)));
}
