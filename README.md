# AXIOM

Local-first compiler that turns dense physics textbooks (including old scanned ones) into an optimized,
interactive study text: verbatim mathematics recovered from the scan, rewritten prose at three levels of detail,
full provenance back to the original page, contextual AI questions, and exercises with graduated hints.

Everything runs on your machine with a local model (Qwen3.6-35B-A3B via llama.cpp). Premium models are
optional and off by default.

## Quick start

```bash
./axiom                  # starts the local model + the app, opens http://127.0.0.1:8000
./axiom stop             # stops what it started      (also: status, logs, restart)
./axiom install-desktop  # adds "AXIOM" to your application menu
```

The launcher builds the UI when its sources changed, reuses anything already running, and keeps logs in
`data/logs/`. Without the local model you can still read processed books; processing needs it.

**Server + computing machines:** keep the books on an always-on box such as a Raspberry Pi (Docker), read them
from your phone or laptop, and let your PC or laptop do the computing when it is on. See
[docs/DEPLOY.md](docs/DEPLOY.md):
`docker compose up -d` on the Pi, then `./axiom setup-worker http://<pi>:8080` and `./axiom` on each PC.

Then add a PDF in the library, tick the chapters to process on the book page, and start reading as soon as the
first sections are ready.

### Manual start (development)

```bash
# 1. local model server (OpenAI-compatible, :8081; vision enabled if the mmproj file is present)
scripts/start-local-llm.sh

# 2. backend + workers (also serves the built frontend at http://127.0.0.1:8000)
cd backend && uv sync && (cd tools && pnpm install) && uv run axiom serve
cd frontend && pnpm install && pnpm build      # once, or `pnpm dev` for :5173

# 3. import a book and process chapters (or use the web UI)
cd backend && uv run axiom import "../Carroll - 2014 - Spacetime and geometry an introduction to general relativity.pdf"
curl -X POST localhost:8000/api/documents/<doc_id>/process -H 'content-type: application/json' \
     -d '{"chapters":[1,2],"profile":"economy"}'
uv run axiom status <doc_id>
```

Open `http://127.0.0.1:8000`. Processed sections are readable while others are still processing.

Study tools (document page): **exercise-first practice**, chapter **exams**, Anki **.apkg / .txt download or
AnkiConnect push**; in the reader, a **"Test yourself"** quiz after each section and an optional **"Try first"**
exercise box. In an exercise: graduated hints, **check my answer** (grades your scratchpad), and a worked solution
cross-checked by two independent solves. Any equation or result can get an AI **visualization** (runs in a sandbox).

Flashcards from the command line (written to `data/exports/` unless `--out` is given):

```bash
cd backend && uv run axiom anki <doc_id> --format apkg --deck "Carroll GR"     # or --format tsv | push
```

Reader keys: `1`/`2`/`3` Fast Track / Standard / Deep Dive · `j`/`k` next/prev section · `E` exercises ·
`D` dark mode · `?` help. Select any text → **Ask AI**. Every unit has a source chip → original scan crop + OCR.

## Tests

```bash
cd backend && uv run pytest -q          # core acceptance tests (+ benchmark scorer tests)
cd backend && uv run python benchmarks/score_transcription.py   # transcription accuracy vs gold set
cd frontend && pnpm test
```

See `docs/ARCHITECTURE.md` for design and `docs/STATUS.md` for what works, measured results and limitations.

## AI models: local, Claude Code, Codex

The heavy lifting (transcribing the scan, rewriting sections, summaries, faithfulness checks) runs on the local
model. For short jobs that need careful reasoning (Ask AI, hints, worked solutions, answer checks, quizzes,
visualizations) AXIOM can use your **Claude** and **ChatGPT subscriptions** through the official **Claude Code**
(`claude`) and **Codex** (`codex`) command-line tools. No API keys are needed: install the CLI and sign in once
(`claude`, `codex login`).

- **AI models** page (top bar): shows which models are ready, today's usage, and lets you choose the model for
  every kind of task. Every AI button also has its own model picker, which wins over the table.
- **Auto** keeps token-heavy work local and sends short reasoning jobs to a subscription model (Claude Code ·
  Sonnet first, then Codex), falling back to the local model if none is available or a call fails. Worked
  solutions are written twice by *different* vendors and compared; visualizations are reviewed by the other one.
- Sending passages to Anthropic/OpenAI is **opt-in**: switch on “Let Auto use Claude Code / Codex” on the AI
  models page. Choosing a cloud model explicitly always works.
- The CLIs run with **all tools disabled**, in an empty temporary folder, without your MCP servers, settings or
  hooks, and without API keys in their environment — book text can never make them run commands or read files.
- Subscription plans have limits: AXIOM caps each CLI at `AXIOM_SUBSCRIPTION_DAILY_LIMIT` calls per day
  (default 300) and caches every answer. Other models: `AXIOM_CLAUDE_MODELS=sonnet,opus,haiku`,
  `AXIOM_CODEX_MODELS=default,<model>`.

Paid APIs remain optional: set `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` to add them to the list (per-book call
budget from the profile).
