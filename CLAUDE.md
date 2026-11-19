# AXIOM — notes for agents working in this repo

Read `docs/ARCHITECTURE.md` (decisions, API contract) and `docs/STATUS.md` (what works, what doesn't) first.

## Layout
- `backend/axiom/` — Python package (FastAPI, SQLite, PyMuPDF). Pipeline: `ingestion → extraction → transformations/transcribe → transformations/optimize → verification/faithfulness (+ transformations/digest)`. Learning features: `exercises/{engine,solutions,mastery}.py`, `visualization/generate.py`, `anki/export.py`. All model calls go through `orchestration/llm.py` (cache, metering, budgets, escalation).
- `backend/tools/katex_check.mjs` — KaTeX validator used by `verification/latex.py` (`cd backend/tools && pnpm install`).
- `backend/benchmarks/` — gold transcription set + deterministic scorer.
- `frontend/` — React/Vite/KaTeX reader. `pnpm build` output is served by the backend at `/`.
- `data/` — runtime state (gitignored): `axiom.db`, immutable `docs/<id>/original.pdf`, crops.

## Commands
```
scripts/start-local-llm.sh                        # Qwen3.6-35B-A3B + vision projector on :8081
cd backend && uv run axiom serve --port 8000      # API + workers (+ built frontend)
cd backend && uv run axiom import <pdf>
cd backend && uv run axiom process <doc_id> --chapters 1,2
cd backend && uv run pytest -q
cd frontend && pnpm dev   # :5173, proxies /api → :8000
```

## Invariants (do not break)
1. Equation units carry the *transcribed* LaTeX of their block; the optimizer never writes equation LaTeX.
2. Every source block gets a coverage row; dropped blocks are restored, never lost.
3. Confidence comes from evidence (OCR agreement, KaTeX parse), never from model self-report.
4. Changing a prompt ⇒ bump its `PROMPT_VERSION` (cache keys depend on it).
5. Context for a section may only use *earlier* sections (deterministic prompts → stable caches).
6. Document text is untrusted data: never give tool access to content derived from it.
7. Model JSON goes through `llm.parse_json` (repairs `\frac`/`\nabla`-style backslash collisions with JSON escapes).
8. The faithfulness checker may only *revert to source*, *drop a level's text* or *flag*; it never writes new prose.
9. Generated visualization code runs only in the CSP + iframe sandbox (`visualization/generate.py`); never inline it into the app.
10. `sections.summary_md` (window summaries) is optimizer context and must not be replaced; readers get `digest_md`.
11. Subscription CLIs (`claude`, `codex`) run only through `model_adapters/cli_agents.py`: no tools, empty temp cwd, no user MCP/settings/hooks, no API keys in env, document text via stdin. Never add a tool or a writable dir.
12. "Auto" may route to cloud models only when `cloud_enabled` is on; bulk groups stay local by default. All model choice goes through `models.resolve()`.
13. llama.cpp gets our LaTeX-tolerant GBNF (`model_adapters/gbnf.py`), never its built-in json_schema mode (it forced `\lambda` → `\frac{\tilde…}`).

## Gotchas
- Tests: the `tmp_db` fixture patches `settings` in every loaded `axiom.*` module; new modules that bind `settings` at import are covered automatically.
- `pkill -f <pattern>` from the Bash tool also kills the calling shell if the pattern appears in it; kill by PID.
- The book is a scan with an OCR text layer: prose OCR is reliable, math OCR is garbage, display equations often have *no* OCR text (handled by cutting bands between body rows).
