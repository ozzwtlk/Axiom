// Mirrors backend/axiom/api/schemas.py exactly — keep in sync.
// Python `X | None` (without default) -> `X | null`; fields with defaults are still always serialized
// by FastAPI, so they are typed as required here.

export type Status = "pending" | "running" | "done" | "failed" | "partial";
export type Coverage = "preserved" | "rewritten" | "merged" | "expandable" | "omitted" | "pending";
export type UnitKind =
  | "heading" | "prose" | "equation" | "definition" | "theorem" | "result" | "warning"
  | "example" | "remark" | "aside" | "figure" | "exercise_ref";

export interface DocumentSummary {
  id: string;
  title: string;
  page_count: number;
  created_at: string;
  chapters_total: number;
  chapters_done: number;
  completed: boolean;
  cover_revision: string | null;
  reading_section_ids: string[];
}

export interface SectionSummary {
  id: string;
  number: string; // "1.3"
  title: string;
  page_start: number; // 1-based PDF page index
  page_end: number;
  status: Status;
  completed: boolean;
  is_exercises: boolean;
}

export interface ChapterSummary {
  id: string;
  number: number;
  title: string;
  page_start: number;
  page_end: number;
  status: Status;
  completed: boolean;
  sections: SectionSummary[];
}

export interface DocumentDetail extends DocumentSummary {
  chapters: ChapterSummary[];
}

export type FlagKind =
  | "low_confidence" | "ambiguous_notation" | "verification_failed" | "ocr_uncertain" | "info"
  | "unverified_claim" | "reverted";

export interface Flag {
  kind: FlagKind;
  message: string;
}

export interface UnitSource {
  block_ids: string[];
  pages: number[];
  coverage: Coverage;
  note: string | null;
}

export type Level = 1 | 2 | 3;

export type NoteKind = "key" | "intuition" | "pitfall" | "recall" | "check";

/** AI-written study note (shown at Standard and Deep Dive, always labelled). */
export interface StudyNote {
  kind: NoteKind;
  md: string;
}

export interface Unit {
  id: string;
  kind: UnitKind;
  role: string;
  min_level: Level;
  text_md: string;
  fast_md: string | null;
  deep_md: string | null;
  latex: string | null;
  eq_number: string | null;
  annotation_md: string | null;
  title: string | null;
  ai_generated: boolean;
  source: UnitSource;
  flags: Flag[];
  figure_url: string | null;
  /** independent faithfulness check; null = not applicable / not run yet */
  verification: "verified" | "flagged" | "reverted" | "unchecked" | null;
  notes: StudyNote[];
  /** a saved visualization exists for this unit */
  has_visualization: boolean;
}

// ---------------------------------------------------------------- cross-references
export interface XrefTarget {
  block_id: string;
  section_id: string;
  page: number;
}
export interface Xrefs {
  equations: Record<string, XrefTarget & { latex: string | null }>;
  figures: Record<string, XrefTarget & { caption: string; image_url: string | null }>;
  sections: Record<string, { section_id: string; title: string }>;
  chapters: Record<string, { section_id: string; title: string }>;
  unresolved: { kind: "equation" | "figure" | "section"; ref: string; section_id: string }[];
}

export interface ExerciseSummary {
  id: string;
  number: string;
  statement_md: string;
  page: number;
}

export interface SectionView {
  is_exercises?: boolean;
  id: string;
  document_id: string;
  chapter_number: number;
  number: string;
  title: string;
  status: Status;
  summary_md: string | null;
  /** the independent faithfulness check has run for this section */
  verified: boolean;
  units: Unit[];
  exercises: ExerciseSummary[];
  prev_section_id: string | null;
  next_section_id: string | null;
}

export interface SourceBlock {
  id: string;
  document_id: string;
  section_id: string;
  page: number;
  bbox: [number, number, number, number];
  block_type: string;
  raw_text: string;
  transcribed_md: string | null;
  transcription_method: "text_layer" | "vision_ocr" | "none";
  confidence: number;
  eq_number: string | null;
  image_url: string;
}

export interface StageCount {
  stage: string;
  done: number;
  total: number;
}

export interface ProgressReport {
  document_id: string;
  pages_total: number;
  sections_total: number;
  sections_done: number;
  sections_failed: number;
  stages: StageCount[];
  llm_calls: number;
  llm_cache_hits: number;
  local_seconds: number;
  premium_calls: number;
  premium_tokens: number;
  jobs: Record<string, unknown>[];
}

export type AskAction =
  | "explain" | "simpler" | "steps" | "why" | "physical" | "example" | "notation" | "prerequisites" | "custom";

export interface AskRequest {
  section_id: string;
  selection: string;
  block_ids?: string[];
  action?: AskAction;
  question?: string | null;
  /** "auto" / undefined = the routing in Settings; otherwise a model id such as "claude-code:sonnet" */
  model?: string;
  /** concurrent chapters/sections; the server rejects it for the local model */
  parallel?: boolean;
}

export interface ContextItem {
  kind: string;
  ref: string;
  title: string;
}

export interface AskResponse {
  answer_md: string;
  model: string;
  cached: boolean;
  context_used: ContextItem[];
}

export interface Exercise {
  id: string;
  document_id: string;
  chapter_number: number;
  number: string;
  statement_md: string;
  page: number;
  block_ids: string[];
  hints_available: number;
  related_section_ids: string[];
}

export interface HintResponse {
  level: number;
  hint_md: string;
  model: string;
  cached: boolean;
}

export interface SearchHit {
  section_id: string;
  section_title: string;
  block_id: string;
  snippet: string;
  page: number;
}

export type Profile = "economy" | "balanced" | "max";

export interface ProcessRequest {
  chapters: number[];
  profile: Profile;
  /** one model for the whole run ("local", "claude-code:haiku", ...); omitted = Settings */
  model?: string;
}

export interface ProcessResponse {
  job_ids: string[];
}

// ---------------------------------------------------------------- solutions & answer checks
export interface SolutionPart {
  label: string;
  solution_md: string;
  final_answer_md: string;
  answer_type: "expression" | "numeric" | "construction" | "proof" | "explanation";
}
export interface Solution {
  exercise_id: string;
  model: string;
  /** evidence from two independent solutions: consistent | inconsistent | not_comparable */
  status: "consistent" | "inconsistent" | "not_comparable";
  comparison: { label: string; status: string; explanation: string }[];
  solution: { parts: SolutionPart[] };
  alternative: { parts: SolutionPart[] } | null;
}
export interface AnswerCheck {
  verdict: "correct" | "partially_correct" | "incorrect" | "cannot_judge";
  feedback_md: string;
  first_error_md: string;
  model: string;
  reference_status: Solution["status"];
}
export interface ExerciseMapItem {
  id: string;
  number: string;
  chapter_number: number;
  statement_md: string;
  related_section_ids: string[];
}

// ---------------------------------------------------------------- mastery checks
export interface QuizQuestion {
  id: string;
  type: "mcq" | "short";
  prompt_md: string;
  options: string[];
  source_units: string[];
  section_ids: string[];
}
export interface Quiz {
  id: string;
  scope: "section" | "chapter";
  scope_id: string;
  title: string;
  model: string;
  questions: QuizQuestion[];
  dropped_by_blind_check: number;
}
export interface QuizResultItem {
  id: string;
  score: number;
  feedback_md: string;
  correct: number;
  answer_md: string;
  explanation_md: string;
  source_units: string[];
  section_ids: string[];
}
export interface QuizResult {
  quiz_id: string;
  score: number;
  results: QuizResultItem[];
}

// ---------------------------------------------------------------- visualizations
export interface Visualization {
  unit_id: string;
  title: string;
  description_md: string;
  model: string;
  ai_generated: boolean;
  reviewed?: boolean | null;
  review_notes?: string[] | null;
  /** generated automatically while the book was processed */
  automatic?: boolean | null;
}

// ---------------------------------------------------------------- models & routing
export type ModelKind = "local" | "subscription" | "api";
export interface ModelInfo {
  id: string;
  label: string;
  kind: ModelKind;
  vision: boolean;
  available: boolean;
  detail: string;
  calls_today: number | null;
  daily_limit: number | null;
}
export type TaskGroup =
  | "transcribe" | "optimize" | "digest" | "faithfulness"
  | "ask" | "hint" | "solve" | "check" | "quiz" | "visualize" | "anki" | "cover";
export interface GroupInfo {
  id: TaskGroup;
  label: string;
  heavy: boolean;
  vision: boolean;
  help: string;
  /** what "auto" resolves to right now */
  auto: string;
}
export interface ModelsReport {
  models: ModelInfo[];
  groups: GroupInfo[];
  /** machine whose models these are */
  machine?: string | null;
}
export interface Routing {
  groups: Record<TaskGroup, string>;
  premium_default: string;
  cloud_enabled: boolean;
  /** model for diagrams generated automatically during processing, or "off" */
  auto_diagrams: string;
}

// ---------------------------------------------------------------- computing machines (server mode)
export interface MachineModel {
  id: string;
  label: string;
  kind: ModelKind;
  available: boolean;
}
export interface Machine {
  name: string;
  online: boolean;
  last_seen: string;
  info: { hostname?: string; platform?: string };
  models: MachineModel[];
}
export interface MachinesReport {
  /** "server": data on a server, computing on connected machines; "all": everything on this computer */
  mode: "server" | "all" | "worker";
  selected: string;
  effective: string | null;
  workers: Machine[];
}
export interface PendingImport {
  id: string;
  filename: string;
  status: string;
  error: string | null;
}

export interface QualityIssue {
  kind: "not_optimized" | "bad_transcription" | "unchecked" | "empty" | "coverage";
  needs: string;
  message: string;
  units?: string[];
  blocks?: string[];
}

export interface QualitySection {
  section_id: string;
  number: string;
  title: string;
  chapter: number;
  status: Status;
  issues: QualityIssue[];
  checked: boolean;
}

export interface QualityReport {
  sections_processed: number;
  sections_checked: number;
  fixed: Record<string, number>;
  open: Record<string, number>;
  sections: QualitySection[];
}
