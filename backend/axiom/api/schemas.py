"""HTTP API response/request models. Mirrored by frontend/src/api/types.ts — keep in sync."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Status = Literal["pending", "running", "done", "failed", "partial"]
Coverage = Literal["preserved", "rewritten", "merged", "expandable", "omitted", "pending"]
UnitKind = Literal[
    "heading", "prose", "equation", "definition", "theorem", "result", "warning",
    "example", "remark", "aside", "figure", "exercise_ref",
]


class DocumentSummary(BaseModel):
    id: str
    title: str
    page_count: int
    created_at: str
    chapters_total: int
    chapters_done: int
    completed: bool = False
    cover_revision: str | None = None


class SectionSummary(BaseModel):
    id: str
    number: str  # "1.3"
    title: str
    page_start: int  # 1-based PDF page index
    page_end: int
    status: Status
    completed: bool = False
    is_exercises: bool = False


class ChapterSummary(BaseModel):
    id: str
    number: int
    title: str
    page_start: int
    page_end: int
    status: Status
    completed: bool = False
    sections: list[SectionSummary]


class DocumentDetail(DocumentSummary):
    chapters: list[ChapterSummary]


class Flag(BaseModel):
    kind: Literal["low_confidence", "ambiguous_notation", "verification_failed", "ocr_uncertain", "info",
                  "unverified_claim", "reverted"]
    message: str


class UnitSource(BaseModel):
    block_ids: list[str]
    pages: list[int]
    coverage: Coverage
    note: str | None = None


class StudyNote(BaseModel):
    kind: Literal["key", "intuition", "pitfall", "recall", "check"]
    md: str  # AI-written, always labelled as such


class Unit(BaseModel):
    id: str
    kind: UnitKind
    role: str  # semantic classification, e.g. "essential_definition", "historical_aside"
    min_level: Literal[1, 2, 3]
    text_md: str  # Markdown with $inline$ and $$display$$ KaTeX math (standard level)
    fast_md: str | None = None  # condensed version for Fast Track; fall back to text_md
    deep_md: str | None = None  # AI-added deeper explanation (always labelled AI in UI)
    latex: str | None = None  # equation units: display LaTeX (without $$)
    eq_number: str | None = None  # "1.29"
    annotation_md: str | None = None  # equation units: short AI note on meaning
    title: str | None = None  # e.g. definition term, theorem name
    ai_generated: bool = False  # true when the text is an AI rewrite (not verbatim source)
    source: UnitSource
    flags: list[Flag] = Field(default_factory=list)
    figure_url: str | None = None
    # independent faithfulness check: verified | flagged | reverted | unchecked; None = not applicable / not run yet
    verification: Literal["verified", "flagged", "reverted", "unchecked"] | None = None
    notes: list[StudyNote] = Field(default_factory=list)  # AI study notes (Standard + Deep Dive)
    has_visualization: bool = False  # a saved visualization exists for this unit


class ExerciseSummary(BaseModel):
    id: str
    number: str
    statement_md: str
    page: int


class SectionView(BaseModel):
    id: str
    document_id: str
    chapter_number: int
    number: str
    title: str
    status: Status
    summary_md: str | None  # short reader digest when available, else the per-window summaries
    verified: bool = False  # faithfulness check has run for this section
    units: list[Unit]
    exercises: list[ExerciseSummary]
    prev_section_id: str | None
    next_section_id: str | None


class SourceBlock(BaseModel):
    id: str
    document_id: str
    section_id: str
    page: int
    bbox: tuple[float, float, float, float]
    block_type: str
    raw_text: str  # OCR / text-layer output as extracted
    transcribed_md: str | None  # vision transcription (if any)
    transcription_method: Literal["text_layer", "vision_ocr", "none"]
    confidence: float
    eq_number: str | None
    image_url: str


class StageCount(BaseModel):
    stage: str
    done: int
    total: int


class ProgressReport(BaseModel):
    document_id: str
    pages_total: int
    sections_total: int
    sections_done: int
    sections_failed: int
    stages: list[StageCount]
    llm_calls: int
    llm_cache_hits: int
    local_seconds: float
    premium_calls: int
    premium_tokens: int
    jobs: list[dict]


class AskRequest(BaseModel):
    section_id: str
    selection: str
    block_ids: list[str] = Field(default_factory=list)
    action: Literal[
        "explain", "simpler", "steps", "why", "physical", "example", "notation", "prerequisites", "custom"
    ] = "explain"
    question: str | None = None
    model: str | None = None  # "auto" / None = routing settings; or a model id ("local", "claude-code:sonnet", ...)


class ContextItem(BaseModel):
    kind: str  # "selection" | "nearby_block" | "section_summary" | "chapter_summary" | "notation" | ...
    ref: str
    title: str


class AskResponse(BaseModel):
    answer_md: str
    model: str
    cached: bool
    context_used: list[ContextItem]


class Exercise(BaseModel):
    id: str
    document_id: str
    chapter_number: int
    number: str
    statement_md: str
    page: int
    block_ids: list[str]
    hints_available: int = 5
    related_section_ids: list[str] = Field(default_factory=list)


class HintRequest(BaseModel):
    level: int
    model: str | None = None


class HintResponse(BaseModel):
    level: int
    hint_md: str
    model: str
    cached: bool


class SearchHit(BaseModel):
    section_id: str
    section_title: str
    block_id: str
    snippet: str
    page: int
