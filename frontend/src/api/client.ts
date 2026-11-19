import type {
  AnswerCheck, AskRequest, AskResponse, DocumentDetail, ExerciseMapItem, Quiz, QuizResult, Solution, Visualization, DocumentSummary, Exercise, HintResponse,
  ProcessRequest, ProcessResponse, ProgressReport, SearchHit, SectionView, SourceBlock, ModelsReport, Routing, TaskGroup,
} from "./types";
import * as M from "../mocks/sampleSection";

const BASE = "/api";

/** Mock mode: enabled by `?mock=1` (sticky for the browser tab via sessionStorage), disabled by `?mock=0`. */
export const MOCK: boolean = (() => {
  if (typeof window === "undefined") return false;
  try {
    const q = new URLSearchParams(window.location.search).get("mock");
    if (q === "1") sessionStorage.setItem("axiom.mock", "1");
    if (q === "0") sessionStorage.removeItem("axiom.mock");
    return sessionStorage.getItem("axiom.mock") === "1";
  } catch {
    return new URLSearchParams(window.location.search).get("mock") === "1";
  }
})();

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, init);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body && typeof body.detail === "string") detail = body.detail;
      else if (body && body.detail) detail = JSON.stringify(body.detail);
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, `${res.status} ${detail}`);
  }
  return (await res.json()) as T;
}

function json(body: unknown): RequestInit {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

function delay<T>(v: T, ms = 150): Promise<T> {
  return new Promise((r) => setTimeout(() => r(structuredClone(v)), ms));
}

const enc = encodeURIComponent;

export function listDocuments(): Promise<DocumentSummary[]> {
  if (MOCK) return delay(M.mockDocuments);
  return request("/documents");
}

export function uploadDocument(file: File): Promise<DocumentSummary> {
  if (MOCK) return delay({ ...M.mockDocuments[0], title: file.name }, 400);
  const fd = new FormData();
  fd.append("file", file);
  return request("/documents", { method: "POST", body: fd });
}

export function getDocument(docId: string): Promise<DocumentDetail> {
  if (MOCK) return delay(M.mockDocument);
  return request(`/documents/${enc(docId)}`);
}

export function processDocument(docId: string, body: ProcessRequest): Promise<ProcessResponse> {
  if (MOCK) return delay({ job_ids: body.chapters.map((c) => `mock-job-${c}`) });
  return request(`/documents/${enc(docId)}/process`, json(body));
}

export function getProgress(docId: string): Promise<ProgressReport> {
  if (MOCK) return delay(M.mockProgress());
  return request(`/documents/${enc(docId)}/progress`);
}

export function getSection(sectionId: string): Promise<SectionView> {
  if (MOCK) {
    const s = M.mockSections[sectionId];
    return s ? delay(s, 250) : Promise.reject(new ApiError(404, "404 section not in mock fixtures"));
  }
  return request(`/sections/${enc(sectionId)}`);
}

export function getBlock(blockId: string): Promise<SourceBlock> {
  if (MOCK) return delay(M.mockBlock(blockId));
  return request(`/blocks/${enc(blockId)}`);
}

export function blockImageUrl(blockId: string): string {
  if (MOCK) return M.mockBlockImage(blockId);
  return `${BASE}/blocks/${enc(blockId)}/image`;
}

export function pageImageUrl(docId: string, page: number): string {
  return `${BASE}/documents/${enc(docId)}/pages/${page}/image`;
}

export function ask(req: AskRequest): Promise<AskResponse> {
  if (MOCK) return delay(M.mockAsk(req.selection, req.action ?? "explain"), 600);
  return request("/ask", json(req));
}

export function getExercise(exerciseId: string): Promise<Exercise> {
  if (MOCK) return delay({ ...M.mockExercise, id: exerciseId });
  return request(`/exercises/${enc(exerciseId)}`);
}

const withModel = (model?: string) => (model && model !== "auto" ? { model } : {});

export function getHint(exerciseId: string, level: number, model?: string): Promise<HintResponse> {
  if (MOCK) return delay(M.mockHint(level), 400);
  return request(`/exercises/${enc(exerciseId)}/hint`, json({ level, ...withModel(model) }));
}

export function search(docId: string, q: string): Promise<SearchHit[]> {
  if (MOCK) return delay(M.mockSearch(q));
  return request(`/search?doc_id=${enc(docId)}&q=${enc(q)}`);
}

// ---------------------------------------------------------------- solutions & answer checks
export function getSolution(exerciseId: string): Promise<Solution> {
  return request(`/exercises/${enc(exerciseId)}/solution`);
}

/** Generates two independent solutions + a comparison: slow (minutes) on the local model. */
export function generateSolution(exerciseId: string, model?: string): Promise<Solution> {
  return request(`/exercises/${enc(exerciseId)}/solution`, json(withModel(model)));
}

export function checkAnswer(exerciseId: string, answer_md: string, model?: string): Promise<AnswerCheck> {
  return request(`/exercises/${enc(exerciseId)}/check`, json({ answer_md, ...withModel(model) }));
}

export function getExerciseMap(docId: string): Promise<ExerciseMapItem[]> {
  return request(`/documents/${enc(docId)}/exercise-map`);
}

// ---------------------------------------------------------------- mastery checks
export function makeQuiz(scope: "section" | "chapter", scope_id: string, variant = 0, n = 6, model?: string): Promise<Quiz> {
  return request("/quizzes", json({ scope, scope_id, variant, n, ...withModel(model) }));
}

export function gradeQuiz(quizId: string, answers: Record<string, number | string>, model?: string): Promise<QuizResult> {
  return request(`/quizzes/${enc(quizId)}/grade`, json({ answers, ...withModel(model) }));
}

export function getMastery(docId: string): Promise<Record<string, number>> {
  return request(`/documents/${enc(docId)}/mastery`);
}

// ---------------------------------------------------------------- anki
export function ankiUrl(docId: string, format: "apkg" | "tsv", deck: string, chapters?: number[]): string {
  const ch = chapters && chapters.length ? `&chapters=${chapters.join(",")}` : "";
  return `${BASE}/documents/${enc(docId)}/anki?format=${format}&deck=${enc(deck)}${ch}`;
}

export function pushToAnki(docId: string, deck: string, chapters?: number[]): Promise<{ added: number; updated: number; deck: string }> {
  return request(`/documents/${enc(docId)}/anki/push`, json({ deck, chapters: chapters ?? null }));
}

// ---------------------------------------------------------------- visualizations
export function getVisualization(unitId: string): Promise<Visualization> {
  return request(`/units/${enc(unitId)}/visualization`);
}

export function makeVisualization(unitId: string, regenerate = false, model?: string): Promise<Visualization> {
  const q = new URLSearchParams();
  if (regenerate) q.set("regenerate", "true");
  if (model && model !== "auto") q.set("model", model);
  return request(`/units/${enc(unitId)}/visualization${q.size ? "?" + q : ""}`, json({}));
}

// ---------------------------------------------------------------- models & routing
export function listModels(): Promise<ModelsReport> {
  if (MOCK)
    return delay({
      models: [{ id: "local", label: "Local model (Qwen, llama.cpp)", kind: "local", vision: true, available: true, detail: "", calls_today: null, daily_limit: null }],
      groups: [],
    } as ModelsReport);
  return request("/models");
}

export function getRouting(): Promise<Routing> {
  return request("/settings/routing");
}

export function saveRouting(update: { groups?: Partial<Record<TaskGroup, string>>; premium_default?: string; cloud_enabled?: boolean }): Promise<Routing> {
  return request("/settings/routing", { method: "PUT", headers: { "content-type": "application/json" },
    body: JSON.stringify({ groups: {}, ...update }) });
}

export function visualizationHtmlUrl(unitId: string): string {
  return `${BASE}/units/${enc(unitId)}/visualization.html`;
}
