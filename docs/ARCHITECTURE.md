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
            missing/flat outline → try a numbered printed contents table, with page offsets checked against headings
extract     OCR/text layer → lines with bbox+font size → semantic blocks              (deterministic)
            strip running heads/page numbers, detect display equations by "(n.m)"
            tags, captions, footnotes; score each block for "math suspicion"
            display-math boxes are grown while their edge cuts through ink (fit_to_ink), never
            into a neighbouring block
transcribe  math-bearing blocks: crop image + OCR hint → vision LLM → Markdown/LaTeX  (local VLM)
            on scanned books every paragraph too (OCR mangles Greek letters inside prose)
            LaTeX that KaTeX rejects → one retry told the error, then deterministic syntax repair
            + quality control (blocks): page numbers → artifact, text cut out as images → text, lines
              caught twice → removed                                              (deterministic)
optimize    windows of consecutive blocks (never split equation from its paragraph)  (local LLM)
            → units: role, min_level, fast/standard/deep text, coverage mapping
              standard = teaching text (bold key terms/claims, steps, cited equations);
              deep = AI derivation steps ($$…$$, never numbered), meaning, limits, links;
              notes = ≤3 study notes (key/intuition/pitfall/recall/check); diagram = optional idea
            + coverage audit (every block mapped), equation integrity, KaTeX parse    (deterministic)
            → ≤2 "diagram" jobs per section on the model in Settings (default Codex), own thread,
              in parallel with the pipeline; a skipped/rejected diagram never fails the section
verify      independent faithfulness check of every AI-written unit against its     (local LLM, reasoning;
            source blocks; contradicted text → reverted to the original, contradicted   premium if profile allows)
            fast/deep version, study notes, equation annotation → dropped (AI-added equation steps are
            checked step by step), dropped conditions / unsupported claims → flagged.
            Then a short reader digest of the section, the cross-reference check (references without a
            target are logged in the stats) and a final coverage audit.
quality     after optimize and verify (verification/quality.py): LaTeX repair / drop / revert, stray
            LaTeX wrapped, repeats and cut-off texts removed; open issues (verbatim passages, broken
            transcriptions, unchecked batches, coverage) keep the section 'partial'      (deterministic)
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
| Visualizations | saved in `visualizations`, anchored to source blocks (`anchor_blocks`) so re-optimizing moves them to the unit that now shows the passage (none fits → kept as `detached:<section>:…`, re-attached later); shown automatically in the reader; "save as file" download. Model writes plain HTML/JS; static screen (no network/storage/parent access/eval/external URLs); served by the server inside its own document with `Content-Security-Policy: sandbox allow-scripts; default-src 'none'; connect-src 'none' …`; embedded in `<iframe sandbox="allow-scripts">` without `allow-same-origin` |
| Anki | Section jobs draft and review standalone reasoning, ansatz, derivation and figure cards using verified/source units; defaults to the local model, with a per-run model picker. Chapter and book export caps are half their page counts, rounded up; longer sections are processed in batches, and selection favors breadth across sections. `.apkg` and AnkiConnect include genuine PDF figure crops; `.txt` is text-only. Notes use stable source-based IDs. Rebuilding or importing does not delete existing notes in Anki. |

### Mathematical integrity rules (enforced in code, not prompts)
1. Equation units always carry the **transcribed** LaTeX of the source equation; the optimizer can only attach annotations. Any rewrite of an equation is stored separately and marked `ai_generated`.
2. Every equation and transcribed block keeps a link to its **source image crop**; the reader can always show the original scan.
3. Transcriptions of scanned math are labelled `transcription: vision-ocr` with a confidence derived from evidence (OCR-vs-transcription token agreement, LaTeX parse success, equation-number match), not from model self-reports.
4. Every source block ends up with a coverage status: `preserved | rewritten | merged | expandable | omitted | pending`. Omission requires a justification string. The audit is stored.

### Reading progress

Reading progress is stored per browser, independently of processing status. It tracks the furthest visible point in each non-exercise section at the chosen reading level. Chapter and book percentages average the eligible sections; unread sections count as zero. Empty chapter openings and exercise sections are excluded. Untouched books have no reading-status label.

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
| GET | `/documents/{doc_id}` | `DocumentDetail` (chapters → sections with status and generated cover revision) |
| DELETE | `/documents/{doc_id}` | `204`; deletes AXIOM's library copy and derived state. Refused while any job for the book is queued or running. |
| POST / GET | `/documents/{doc_id}/cover` / `/documents/{doc_id}/cover.png` | Queue a Codex cover design / read the locally rendered PNG; Codex receives only the title and a variation seed. |
| GET | `/jobs` | Running work, queue priority order, dependencies, model overrides and history |
| PATCH / DELETE | `/jobs/{job_id}` | Waiting jobs: change model, move to front, or remove. Running jobs: PATCH `{action: "abort"}` or `{action: "restart", model}` requests a stop at the next safe checkpoint. Abort cancels waiting hard dependents; restart retains the job ID and dependencies, reuses saved checkpoints, and queues the job with the new model. In-flight calls can finish. |
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

| POST | `/documents/{doc_id}/anki/generate[?regenerate=true]` body `{model?}` | Queue study-card generation for verified sections. The chosen model drafts and reviews the cards; without an override, use the Anki routing setting. Skip sections already generated unless `regenerate=true`. |
| GET | `/documents/{doc_id}/anki?format=apkg\|tsv\|json&deck=&chapters=` | `.apkg` / `.txt` download (also written to `data/exports/`) or card preview |
| POST | `/documents/{doc_id}/anki/push` body `{deck, chapters?}` | `{added, updated, deck}` via AnkiConnect (127.0.0.1:8765) |
| GET / POST | `/exercises/{id}/solution` | stored solution (GET, 404 if none) / generate it (POST, minutes) |
| POST | `/exercises/{id}/check` body `{answer_md}` | `{verdict, feedback_md, first_error_md, reference_status}` |
| GET | `/documents/{doc_id}/exercise-map` | exercises with their related section ids |
| POST | `/quizzes` body `{scope: section\|chapter, scope_id, n, variant}` | quiz without answer keys |
| POST | `/quizzes/{quiz_id}/grade` body `{answers: {q1: 0, q2: "text"}}` | per-question score, key, explanation, sources |
| GET | `/documents/{doc_id}/mastery` | `{section_id: best score}` |
| GET / POST | `/units/{unit_id}/visualization` | metadata / generate (`?regenerate=true`) |
| GET | `/units/{unit_id}/visualization.html[?download=1]` | the sandboxed page (CSP `sandbox allow-scripts`, no network); `download` = attachment |
| GET | `/documents/{doc_id}/quality` | quality control report: fixes per kind, open issues per section (what still needs a model) |
| POST | `/documents/{doc_id}/quality` | `{rerun, profile, model}`: "quality" job per processed section (deterministic fixes); `rerun` re-queues transcribe → optimize → verify for sections with open issues |
| GET | `/documents/{doc_id}/xrefs` | cross-reference index from source blocks: equations, figures, sections, chapters → block/section ids; `unresolved` references |

TypeScript shapes: see `frontend/src/api/types.ts` (mirrors `backend/axiom/api/schemas.py`).


## Deployment roles (docs/DEPLOY.md)

`AXIOM_ROLE` = `all` (default: everything in one process on one PC) | `server` | `worker`.

* **server** (`axiom serve --role server`, Docker image `docker/Dockerfile`): owns `data/` (SQLite, PDFs, image
  cache), serves the reader and every read endpoint, never computes. Endpoints that compute call
  `require_compute()`; on the server that returns a marker response and the middleware in `api/internal.py`
  forwards the original request over the chosen worker's WebSocket. GET `…/image` results are stored server-side.
  Uploads go to `data/inbox/` + an `import` job.
* **worker** (`axiom worker`): `db.init_remote()` makes `db.q/q1/connect()` run on the server
  (`/internal/db/run|begin|end`, server-held `BEGIN IMMEDIATE` transactions, rows returned as `RemoteRow`);
  `files.fetch/publish` move PDFs. It executes forwarded requests against its own in-process app
  (`httpx.ASGITransport`) and runs the job loop. `jobs.can_run()`: only the selected machine (or anyone under
  auto) claims jobs, and only jobs whose model it has; `jobs.worker` records the claimant so a disconnect requeues
  exactly its jobs (honoring persisted abort/restart requests). Each claim has a run token so a stale attempt cannot finish a newer one.
* Selection: `meta['compute.selected']` = `auto` | machine name; auto prefers a machine reporting a local model.
