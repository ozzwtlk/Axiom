# AXIOM — Architecture (v0.1)

Local-first textbook compiler + reader. This document records decisions; keep it current.

## Environment (inspected 2026-11-19)

| Resource | Value |
|---|---|
| OS | CachyOS Linux, kernel 7.3 |
| CPU / RAM | i5-12400F (6C/12T), 31 GiB RAM |
| GPU | AMD RX 6800 (Navi 21), 16 GiB VRAM, Vulkan + ROCm installed |
| Local LLM | `Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf` (MoE, 3B active, 22.8 GB) in `~/.local/share/llm/models/` |
| Vision projector | `mmproj-Qwen3.6-35B-A3B-F16.gguf` (downloaded from unsloth/Qwen3.6-35B-A3B-GGUF) |
| Runtime | llama.cpp b12490 (Vulkan) at `~/.local/opt/llama.cpp-b12490/`, started by `scripts/start-local-llm.sh` (OpenAI-compatible, port 8081, `--fit on` auto-splits MoE experts between VRAM and RAM) |
| Measured speed | ~38 tok/s generation (text, thinking disabled) |
| Ollama | Model dir exists (nemotron-3-nano only), binary not installed — not used |
| Premium | Subscription agents via their official CLIs (`claude -p`, `codex exec`, user's own login, tools disabled, empty temp cwd) — `model_adapters/cli_agents.py`; optional paid APIs when keys are set. Never used for bulk work by default. |
| Also present | Mathematica (`/opt/Mathematica`), Anki2 profile dir, Node 26, uv, pnpm |

## Stack

- **Backend**: Python 3.13, FastAPI, stdlib `sqlite3`, PyMuPDF, Pydantic v2, httpx. In-process persistent job worker (SQLite job table; jobs resume after restart).
- **Frontend**: React + TypeScript + Vite, KaTeX, plain CSS variables (no component library — keeps bundle small and the look restrained).
- **Storage**: `data/` (gitignored): `axiom.db`, `docs/<doc_id>/original.pdf` (immutable copy, doc_id = sha256 prefix), `docs/<doc_id>/pages/*.png`, `docs/<doc_id>/crops/*.png`.

Why not a vector DB: structure (document → chapter → section → block) + SQLite FTS5 + notation registry covers retrieval for now.

## Pipeline (per section; every stage is idempotent and cached)

```
ingest      copy PDF, hash, read outline → chapters/sections with page ranges      (deterministic)
extract     OCR/text layer → lines with bbox+font size → semantic blocks              (deterministic)
            strip running heads/page numbers, detect display equations by "(n.m)"
            tags, captions, footnotes; score each block for "math suspicion"
            display-math boxes are grown while their edge cuts through ink (fit_to_ink), never
            into a neighbouring block
transcribe  math-bearing blocks: crop image + OCR hint → vision LLM → Markdown/LaTeX  (local VLM)
            on scanned books every paragraph too (OCR mangles Greek letters inside prose)
optimize    windows of consecutive blocks (never split equation from its paragraph)  (local LLM)
            → units: role, min_level, fast/standard/deep text, coverage mapping
            + coverage audit (every block mapped), equation integrity, KaTeX parse    (deterministic)
verify      independent faithfulness check of every AI-written unit against its     (local LLM, reasoning;
            source blocks; contradicted text → reverted to the original, contradicted   premium if profile allows)
            fast/deep version → dropped, dropped conditions / unsupported claims → flagged.
            Then a short reader digest of the section and a final coverage audit.
```

Job graph per chapter: `extract → transcribe(s_i) → optimize(s_i) → verify(s_i)`; `optimize(s_i)` also waits
for `optimize(s_{i-1})` (its summary is context). A section is readable after `optimize`; `verified` after `verify`.

### Learning features (on demand, cached, always labelled AI-generated)
| Feature | Evidence / safeguard |
|---|---|
| Hints (5 levels) | reasoning on; the model solves privately first, outputs only the hint |
| Worked solutions | solved twice independently (different prompts), a third call compares final answers: `consistent / inconsistent / not_comparable` shown to the user; on disagreement both are shown |
| Answer checking | student's scratchpad vs reference solution(s); first error pointed out, no full solution; warns when references disagree |
| Mastery checks / chapter exams | questions only from units that passed the faithfulness check, each citing its source units; every multiple-choice question is answered blind (key hidden) and dropped if the blind answer differs; MCQ graded deterministically, short answers by rubric |
| Exercise-first mode | deterministic exercise→section map (shared concepts, cited equation numbers); practice page + "try first" box in the reader |
| Visualizations | model writes plain HTML/JS; static screen (no network/storage/parent access/eval/external URLs); served by the server inside its own document with `Content-Security-Policy: sandbox allow-scripts; default-src 'none'; connect-src 'none' …`; embedded in `<iframe sandbox="allow-scripts">` without `allow-same-origin` |
| Anki | deterministic cards from verified units (flagged units skipped); `.apkg` (genanki, note type "AXIOM Basic", stable GUIDs), `.txt` import file, or AnkiConnect push to the local Anki (updates notes by `AxiomID`). Files are kept in `data/exports/` |

### Mathematical integrity rules (enforced in code, not prompts)
1. Equation units always carry the **transcribed** LaTeX of the source equation; the optimizer can only attach annotations. Any rewrite of an equation is stored separately and marked `ai_generated`.
2. Every equation and transcribed block keeps a link to its **source image crop**; the reader can always show the original scan.
3. Transcriptions of scanned math are labelled `transcription: vision-ocr` with a confidence derived from evidence (OCR-vs-transcription token agreement, LaTeX parse success, equation-number match), not from model self-reports.
4. Every source block ends up with a coverage status: `preserved | rewritten | merged | expandable | omitted | pending`. Omission requires a justification string. The audit is stored.

### Three levels from one structure
Each unit has `min_level ∈ {1,2,3}` and optional `fast_md` / `deep_md`. Level 1 shows units with `min_level=1` using `fast_md` when present; level 2 shows `min_level≤2` with `text_md`; level 3 shows everything plus `deep_md` (AI-added, labelled). No separate per-level generation.

### Model routing (orchestration/router.py)
Routing (`orchestration/models.py`): every LLM task belongs to a group (transcribe, optimize, digest, faithfulness,
ask, hint, solve, check, quiz, visualize). Model ids: `local`, `claude-code:<alias>`, `codex:<model|default>`,
`api:anthropic`, `api:openai`. Resolution order: per-request choice (UI picker; no silent fallback) → group setting
(`meta` key `routing.v1`; falls back to local) → **auto**: token-heavy groups local; short reasoning groups → best
premium (subscriptions first) with local fallback, only when the user enabled cloud models. Solver B and the
visualization reviewer use a second vendor when available (`second_opinion`). Escalation of local output to premium
only on evidence (validation failure after retries). Budgets: daily call cap per subscription CLI; per-book budget
for paid APIs. Every call is metered in `llm_calls` with the model that actually answered.

### Caching
`llm_cache(key = sha256(model_id, prompt_version, task, canonical_input))`. Changing a prompt bumps `prompt_version` → only affected tasks re-run. Block content hashes drive invalidation.

### Security
PDF content is untrusted: it is only ever placed in model *data* positions, never given tool access. Network: only the local LLM endpoint by default; premium endpoints require explicit config.

## HTTP API (contract shared by backend + frontend)

All JSON. Base `/api`.

| Method | Path | Returns |
|---|---|---|
| GET | `/documents` | `DocumentSummary[]` |
| POST | `/documents` (multipart `file`) | `DocumentSummary` |
| GET | `/documents/{doc_id}` | `DocumentDetail` (chapters → sections with status) |
| POST | `/documents/{doc_id}/process` body `{chapters: number[], profile: "economy"\|"balanced"\|"max"}` | `{job_ids: string[]}` |
| GET | `/documents/{doc_id}/progress` | `ProgressReport` |
| GET | `/sections/{section_id}` | `SectionView` |
| GET | `/blocks/{block_id}` | `SourceBlock` |
| GET | `/blocks/{block_id}/image` | PNG crop of the original scan |
| GET | `/documents/{doc_id}/pages/{page}/image` | PNG of a full page (1-based page index) |
| POST | `/ask` body `AskRequest` | `AskResponse` |
| GET | `/exercises/{exercise_id}` | `Exercise` |
| POST | `/exercises/{exercise_id}/hint` body `{level: 1..5}` | `{level, hint_md, model, cached}` |
| GET | `/search?doc_id=&q=` | `SearchHit[]` |

| GET | `/documents/{doc_id}/anki?format=apkg\|tsv\|json&deck=&chapters=` | `.apkg` / `.txt` download (also written to `data/exports/`) or card preview |
| POST | `/documents/{doc_id}/anki/push` body `{deck, chapters?}` | `{added, updated, deck}` via AnkiConnect (127.0.0.1:8765) |
| GET / POST | `/exercises/{id}/solution` | stored solution (GET, 404 if none) / generate it (POST, minutes) |
| POST | `/exercises/{id}/check` body `{answer_md}` | `{verdict, feedback_md, first_error_md, reference_status}` |
| GET | `/documents/{doc_id}/exercise-map` | exercises with their related section ids |
| POST | `/quizzes` body `{scope: section\|chapter, scope_id, n, variant}` | quiz without answer keys |
| POST | `/quizzes/{quiz_id}/grade` body `{answers: {q1: 0, q2: "text"}}` | per-question score, key, explanation, sources |
| GET | `/documents/{doc_id}/mastery` | `{section_id: best score}` |
| GET / POST | `/units/{unit_id}/visualization` | metadata / generate (`?regenerate=true`) |
| GET | `/units/{unit_id}/visualization.html` | the sandboxed page (CSP `sandbox allow-scripts`, no network) |

TypeScript shapes: see `frontend/src/api/types.ts` (mirrors `backend/axiom/api/schemas.py`).
