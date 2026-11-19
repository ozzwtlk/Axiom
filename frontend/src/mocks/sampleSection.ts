// Fixtures used when the app runs with ?mock=1 (no backend needed).
import type {
  AskResponse, DocumentDetail, DocumentSummary, Exercise, HintResponse, ProgressReport,
  SearchHit, SectionView, SourceBlock, Unit,
} from "../api/types";

export const MOCK_DOC_ID = "mockdoc01";

function u(p: Partial<Unit> & Pick<Unit, "id" | "kind" | "text_md">): Unit {
  return {
    role: "body",
    min_level: 2,
    fast_md: null,
    deep_md: null,
    latex: null,
    eq_number: null,
    annotation_md: null,
    title: null,
    ai_generated: false,
    source: { block_ids: [`b-${p.id}`], pages: [17], coverage: "preserved", note: null },
    flags: [],
    figure_url: null,
    verification: null,
    ...p,
  };
}

export const sampleSection: SectionView = {
  id: "s-1-2",
  document_id: MOCK_DOC_ID,
  chapter_number: 1,
  number: "1.2",
  title: "Space and Time, Separately and Together",
  status: "done",
  verified: true,
  summary_md:
    "Inertial frames, the spacetime interval $\\Delta s^2$, and why it is the same for every inertial observer.",
  units: [
    u({
      id: "u1", kind: "prose", min_level: 1, role: "motivation",
      text_md:
        "Special relativity is a theory of the structure of spacetime. Its two postulates are that the laws of physics take the same form in every inertial frame, and that the speed of light $c$ is the same in every inertial frame. Everything else in this section is a consequence of taking these postulates seriously.",
      fast_md: "SR rests on two postulates: physics is the same in all inertial frames, and $c$ is frame-independent.",
      ai_generated: false,
      source: { block_ids: ["b-u1a", "b-u1b"], pages: [17, 18], coverage: "preserved", note: null },
    }),
    u({
      id: "u2", kind: "definition", min_level: 1, role: "essential_definition", title: "Inertial frame",
      text_md:
        "An **inertial frame** is a coordinate system $x^\\mu = (ct, x, y, z)$ in which free particles move in straight lines at constant speed, i.e. $d^2 x^i / dt^2 = 0$ for $i = 1,2,3$.",
      source: { block_ids: ["b-u2"], pages: [18], coverage: "rewritten", note: "Merged two sentences into one definition." },
      ai_generated: true,
    }),
    u({
      id: "u3", kind: "prose", min_level: 2, role: "derivation_step",
      text_md:
        "Consider two events separated by $\\Delta t$ in time and $\\Delta x^i$ in space. Individually these depend on the frame. The combination below, however, does not:",
    }),
    u({
      id: "u4", kind: "equation", min_level: 1, role: "key_equation",
      text_md: "",
      latex: "(\\Delta s)^2 = -(c\\Delta t)^2 + (\\Delta x)^2 + (\\Delta y)^2 + (\\Delta z)^2",
      eq_number: "1.1",
      annotation_md: "The **spacetime interval**: the one quantity all inertial observers agree on.",
      source: { block_ids: ["b-u4"], pages: [18], coverage: "preserved", note: null },
      flags: [{ kind: "info", message: "Transcribed by vision-ocr, confidence 0.97 (equation number matched)." }],
    }),
    u({
      id: "u5", kind: "warning", min_level: 1, role: "sign_convention", title: "Sign convention",
      text_md:
        "This book uses the **mostly-plus** signature $\\eta_{\\mu\\nu} = \\mathrm{diag}(-1, +1, +1, +1)$. Many particle-physics texts use $(+,-,-,-)$; formulas like $p_\\mu p^\\mu = -m^2c^2$ flip sign between conventions.",
      source: { block_ids: ["b-u5"], pages: [19], coverage: "preserved", note: null },
    }),
    u({
      id: "u6", kind: "aside", min_level: 3, role: "historical_aside", title: "Historical note",
      text_md:
        "Minkowski's 1908 lecture opened with: \"Henceforth space by itself, and time by itself, are doomed to fade away into mere shadows, and only a kind of union of the two will preserve an independent reality.\"",
      source: { block_ids: ["b-u6"], pages: [19], coverage: "expandable", note: null },
    }),
    u({
      id: "u7", kind: "result", min_level: 1, role: "key_result", title: "Invariance of the interval",
      text_md:
        "For any two inertial frames, $(\\Delta s')^2 = (\\Delta s)^2$. The transformations preserving it are exactly the Lorentz transformations $x^{\\mu'} = \\Lambda^{\\mu'}{}_{\\nu} x^\\nu$ (plus translations).",
      deep_md:
        "Why does invariance of $\\Delta s^2$ follow from constancy of $c$? A light ray has $\\Delta s^2 = 0$ in one frame, hence in all frames. Linearity of the transformation then forces $\\Delta s'^2 = \\lambda\\, \\Delta s^2$, and isotropy plus the group property fix $\\lambda = 1$.",
      source: { block_ids: ["b-u7"], pages: [19, 20], coverage: "merged", note: null },
    }),
    u({
      id: "u8", kind: "example", min_level: 2, role: "worked_example", title: "Example: time dilation",
      text_md:
        "A clock at rest in frame $S'$ ticks with $\\Delta x' = 0$. In frame $S$ it moves at speed $v$, so $\\Delta x = v\\Delta t$. Equating intervals,\n\n$$-c^2\\Delta t'^2 = -c^2\\Delta t^2 + v^2 \\Delta t^2 \\quad\\Rightarrow\\quad \\Delta t = \\gamma\\,\\Delta t', \\qquad \\gamma = \\frac{1}{\\sqrt{1 - v^2/c^2}}.$$",
      flags: [{ kind: "ambiguous_notation", message: "Source writes Δt'² without parentheses; read as (Δt')²." }],
    }),
    u({
      id: "u9", kind: "remark", min_level: 2, role: "remark",
      text_md:
        "Proper time $\\tau$ along a timelike path satisfies $d\\tau^2 = -ds^2/c^2$; with $c=1$ (used from now on) this is simply $d\\tau^2 = -ds^2$.",
    }),
    u({
      id: "u10", kind: "prose", min_level: 3, role: "detail",
      text_md: "A malformed formula from OCR renders as a visible fallback: $\\frac{1}{$ — the rest of the page keeps working.",
      flags: [{ kind: "verification_failed", message: "LaTeX parse check failed for this block." }],
      source: { block_ids: ["b-u10"], pages: [20], coverage: "pending", note: null },
    }),
  ],
  exercises: [
    {
      id: "ex-1-1",
      number: "1.1",
      statement_md:
        "Consider an inertial frame $S$ with coordinates $x^\\mu = (t, x, y, z)$, and a frame $S'$ with coordinates $x^{\\mu'}$ related by a boost with velocity parameter $v$ in the $y$-direction. Write the transformation matrix $\\Lambda^{\\mu'}{}_{\\nu}$ and verify that $\\eta_{\\mu'\\nu'}\\Lambda^{\\mu'}{}_{\\rho}\\Lambda^{\\nu'}{}_{\\sigma} = \\eta_{\\rho\\sigma}$.",
      page: 48,
    },
  ],
  prev_section_id: null,
  next_section_id: "s-1-3",
};

export const sampleSection2: SectionView = {
  id: "s-1-3",
  document_id: MOCK_DOC_ID,
  chapter_number: 1,
  number: "1.3",
  title: "Lorentz Transformations",
  status: "partial",
  verified: false,
  summary_md: "The group of transformations leaving $\\eta_{\\mu\\nu}$ invariant.",
  units: [
    u({
      id: "v1", kind: "prose", min_level: 1, role: "motivation",
      text_md: "We now ask which linear maps $x^\\mu \\to \\Lambda^{\\mu'}{}_\\nu x^\\nu$ leave the interval invariant.",
      source: { block_ids: ["b-v1"], pages: [21], coverage: "preserved", note: null },
    }),
    u({
      id: "v2", kind: "equation", min_level: 1, role: "key_equation",
      text_md: "",
      latex: "\\eta_{\\rho\\sigma} = \\Lambda^{\\mu'}{}_{\\rho}\\,\\eta_{\\mu'\\nu'}\\,\\Lambda^{\\nu'}{}_{\\sigma}",
      eq_number: "1.12",
      source: { block_ids: ["b-v2"], pages: [21], coverage: "preserved", note: null },
    }),
    u({
      id: "v3", kind: "theorem", min_level: 2, role: "structure", title: "Lorentz group",
      text_md: "The matrices satisfying (1.12) form the group $O(3,1)$. Those with $\\det\\Lambda = 1$ and $\\Lambda^{0'}{}_0 \\ge 1$ form the proper orthochronous subgroup $SO^+(3,1)$.",
      source: { block_ids: ["b-v3"], pages: [22], coverage: "rewritten", note: null },
      ai_generated: true,
      flags: [{ kind: "low_confidence", message: "Optimizer confidence 0.62 — compare with original." }],
    }),
  ],
  exercises: [],
  prev_section_id: "s-1-2",
  next_section_id: null,
};

export const mockSections: Record<string, SectionView> = {
  [sampleSection.id]: sampleSection,
  [sampleSection2.id]: sampleSection2,
};

export const mockDocument: DocumentDetail = {
  id: MOCK_DOC_ID,
  title: "Spacetime and Geometry (sample)",
  page_count: 513,
  created_at: "2026-11-19T10:00:00Z",
  chapters_total: 2,
  chapters_done: 0,
  chapters: [
    {
      id: "c-1", number: 1, title: "Special Relativity and Flat Spacetime", page_start: 17, page_end: 64, status: "partial",
      sections: [
        { id: "s-1-1", number: "1.1", title: "Prelude", page_start: 17, page_end: 17, status: "pending", is_exercises: false },
        { id: "s-1-2", number: "1.2", title: "Space and Time, Separately and Together", page_start: 17, page_end: 20, status: "done", is_exercises: false },
        { id: "s-1-3", number: "1.3", title: "Lorentz Transformations", page_start: 21, page_end: 25, status: "partial", is_exercises: false },
        { id: "s-1-4", number: "1.4", title: "Vectors", page_start: 25, page_end: 29, status: "running", is_exercises: false },
        { id: "s-1-x", number: "1.12", title: "Exercises", page_start: 48, page_end: 50, status: "pending", is_exercises: true },
      ],
    },
    {
      id: "c-2", number: 2, title: "Manifolds", page_start: 65, page_end: 109, status: "pending",
      sections: [
        { id: "s-2-1", number: "2.1", title: "Gravity as Geometry", page_start: 65, page_end: 70, status: "pending", is_exercises: false },
      ],
    },
  ],
};

export const mockDocuments: DocumentSummary[] = [
  {
    id: mockDocument.id, title: mockDocument.title, page_count: mockDocument.page_count,
    created_at: mockDocument.created_at, chapters_total: 2, chapters_done: 0,
  },
];

let progressTick = 0;
export function mockProgress(): ProgressReport {
  progressTick = Math.min(progressTick + 1, 6);
  const t = progressTick;
  return {
    document_id: MOCK_DOC_ID,
    pages_total: 513,
    sections_total: 5,
    sections_done: 1 + Math.floor(t / 3),
    sections_failed: 0,
    stages: [
      { stage: "extract", done: 5, total: 5 },
      { stage: "transcribe", done: Math.min(5, 2 + t), total: 5 },
      { stage: "optimize", done: Math.min(5, 1 + Math.floor(t / 2)), total: 5 },
      { stage: "verify", done: Math.min(5, 1 + Math.floor(t / 3)), total: 5 },
    ],
    llm_calls: 40 + 7 * t,
    llm_cache_hits: 12 + t,
    local_seconds: 310.4 + 41.7 * t,
    premium_calls: 0,
    premium_tokens: 0,
    jobs: t >= 6 ? [{ id: "job1", status: "done" }] : [{ id: "job1", status: "running", stage: "optimize" }],
  };
}

export function mockBlock(id: string): SourceBlock {
  return {
    id,
    document_id: MOCK_DOC_ID,
    section_id: "s-1-2",
    page: 18,
    bbox: [72, 140, 520, 210],
    block_type: id === "b-u4" ? "equation" : "paragraph",
    raw_text:
      id === "b-u4"
        ? "(As)2 = -(cAt)2 + (Ax)2 + (Ay)2 + (Az)2 (1.1)"
        : "Raw OCR text of block " + id + " as extracted from the text layer, with its original hyphen-\nation and line breaks.",
    transcribed_md: id === "b-u4" ? "$$(\\Delta s)^2 = -(c\\Delta t)^2 + (\\Delta x)^2 + (\\Delta y)^2 + (\\Delta z)^2$$" : null,
    transcription_method: id === "b-u4" ? "vision_ocr" : "text_layer",
    confidence: id === "b-u4" ? 0.97 : 1,
    eq_number: id === "b-u4" ? "1.1" : null,
    image_url: `/api/blocks/${id}/image`,
  };
}

// A tiny inline SVG used as "scan crop" in mock mode.
export function mockBlockImage(id: string): string {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="480" height="70"><rect width="100%" height="100%" fill="#f4f1ea"/><text x="12" y="40" font-family="serif" font-size="16" fill="#333">[scan crop of ${id}]</text></svg>`;
  return "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
}

export function mockAsk(selection: string, action: string): AskResponse {
  return {
    answer_md:
      `**${action}** — about “${selection.slice(0, 80)}”:\n\nThe interval $\\Delta s^2 = -c^2\\Delta t^2 + \\Delta \\vec x^2$ plays the role that distance plays in Euclidean geometry. Under a boost, $\\Delta t$ and $\\Delta x$ mix,\n\n$$\\Delta t' = \\gamma\\left(\\Delta t - \\frac{v\\,\\Delta x}{c^2}\\right),$$\n\nbut the combination is unchanged.`,
    model: "mock-local (Qwen3.6-35B-A3B)",
    cached: action === "explain",
    context_used: [
      { kind: "selection", ref: "sel", title: "Your selection" },
      { kind: "nearby_block", ref: "b-u4", title: "Eq. (1.1)" },
      { kind: "section_summary", ref: "s-1-2", title: "§1.2 summary" },
      { kind: "notation", ref: "eta", title: "η_{μν} = diag(−1,1,1,1)" },
    ],
  };
}

export const mockExercise: Exercise = {
  id: "ex-1-1",
  document_id: MOCK_DOC_ID,
  chapter_number: 1,
  number: "1.1",
  statement_md: sampleSection.exercises[0].statement_md,
  page: 48,
  block_ids: ["b-ex-1-1"],
  hints_available: 5,
  related_section_ids: ["s-1-2", "s-1-3"],
};

const HINTS = [
  "Start from the boost along $x$ and permute the roles of $x$ and $y$.",
  "With $\\gamma = 1/\\sqrt{1-v^2}$, the nonzero entries are $\\Lambda^{0'}{}_0 = \\Lambda^{2'}{}_2 = \\gamma$ and $\\Lambda^{0'}{}_2 = \\Lambda^{2'}{}_0 = -\\gamma v$.",
  "Write the condition as a matrix product $\\Lambda^T \\eta \\Lambda = \\eta$.",
  "Only the $(t,y)$ block is nontrivial: compute $-\\gamma^2 + \\gamma^2 v^2$.",
  "Since $\\gamma^2(1-v^2) = 1$, the $(t,y)$ block of $\\Lambda^T\\eta\\Lambda$ equals $\\mathrm{diag}(-1,1)$ and the other entries are untouched.",
];
export function mockHint(level: number): HintResponse {
  return { level, hint_md: HINTS[Math.max(0, Math.min(4, level - 1))], model: "mock-local", cached: false };
}

export function mockSearch(q: string): SearchHit[] {
  const ql = q.toLowerCase();
  const hits: SearchHit[] = [];
  for (const s of Object.values(mockSections)) {
    for (const unit of s.units) {
      const text = unit.text_md || unit.latex || "";
      const i = text.toLowerCase().indexOf(ql);
      if (ql && i >= 0) {
        hits.push({
          section_id: s.id,
          section_title: `${s.number} ${s.title}`,
          block_id: unit.source.block_ids[0],
          snippet: text.slice(Math.max(0, i - 40), i + 80),
          page: unit.source.pages[0],
        });
      }
    }
  }
  return hits;
}
