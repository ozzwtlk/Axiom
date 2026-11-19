// localStorage helpers that never throw (private mode, quota, disabled storage).

export function load<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? fallback : (JSON.parse(raw) as T);
  } catch {
    return fallback;
  }
}

export function save(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* ignore */
  }
}

export interface ReadingPosition {
  sectionId: string;
  unitId: string | null;
  at: number;
}

export const posKey = (docId: string) => `axiom.pos.${docId}`;
export const loadPosition = (docId: string) => load<ReadingPosition | null>(posKey(docId), null);
export const savePosition = (docId: string, p: ReadingPosition) => save(posKey(docId), p);

export type ExerciseMark = { completed: boolean; difficult: boolean };
export const exKey = (id: string, what: "scratch" | "hints" | "mark") => `axiom.ex.${id}.${what}`;
