# AXIOM — status (2026-11-20)

Test book: Carroll, *Spacetime and Geometry* (2014 scan with an OCR text layer), chapters 1–2 (pp. 16–107,
22 sections, 929 source blocks). Bulk processing ran entirely on the local model (Qwen3.6-35B-A3B Q4,
llama.cpp, 2 slots). Interactive features were tested on both the local model and the Claude Code / Codex
subscription CLIs.

## What works (verified on Carroll ch. 1–2)

| Feature | Evidence |
|---|---|
| Import, outline → chapters/sections, scan detection, immutable original | 22 sections from the PDF outline |
| Layout segmentation incl. display equations with no OCR text, eq. tags | tags (1.1)…(2.97) recovered |
| Crop refit (`fit_to_ink`): boxes that cut through ink are grown to the next blank strip | 4 truncated equations fixed, e.g. (1.31) |
| Vision transcription of math and prose (local VLM) | benchmark below |
| Rewrite at 3 levels with verbatim equations and a coverage audit | 22/22 sections pass the audit; **0 of 68 windows fell back** |
| Independent faithfulness check (revert / drop level / flag; never writes prose) | 325 rewritten units checked: 313 verified, 8 flagged, 4 reverted, 0 failed batches |
| Section digests (readers) separate from optimizer window summaries | all 22 sections |
| Exercises (21), page-break joins, 5-level hints | Ex. 2.2 joined across p. 106/107 |
| Worked solutions: two independent solvers (two vendors when available) + comparison | Ex. 2.1: Claude + Codex, “consistent” |
| Answer checking that points to the first error | wrong answer → first error found by both Qwen and Codex |
| Section quizzes / chapter exams, MCQs answered blind to drop bad keys | §2.4: 6/6 kept (Claude); mastery per section |
| Exercise-first mode | practice page + “Try first” in the reader |
| Visualizations in a CSP + iframe sandbox, reviewed by a second model | boost diagram (1.33): numbers checked by hand |
| Anki: `.apkg` (stable GUIDs), `.txt`, AnkiConnect push | `data/exports/AXIOM-12c362ef6925.apkg` (261 cards) |
| Model routing: local / Claude Code / Codex / optional APIs, per task and per request | `/settings` page, pickers next to every AI action |
| One-command launcher | `./axiom` (start/stop/status/logs/install-desktop) |

## Measured results

**Transcription** (`backend/benchmarks`, 69 hand-checked gold items from ch. 1 *and* ch. 2): exact match 65/69
(94.2 %), mean token similarity 0.994, **2 critical errors** — a spurious prime in (1.42) (`p0033-03`) and a stray
`\atop` in (1.80) (`p0042-03`). No sign, matrix-entry or index-placement errors.

**Faithfulness verifier** — real catches on the local model: O(3)/SO(3) conflated; $g_\nu$ for $g_{\mu\nu}$;
“two inertial observers” where one accelerates; $m_g$ instead of $m_g/m_i$ as gravitational charge; “WEP implies
EEP” (the book gives a counterexample); wrong direction of $\cot^{-1}$; chronal-curve definition. Version 2 shows
neighbouring blocks as context, which removed the false “source cuts off” flags of v1.

**Speed** (cloud vs local, interactive tasks, uncached): Ask 9 s vs ~20–60 s; hint 8 s vs ~3 min; worked
solution 43 s vs ~15 min; answer check 5–10 s vs several min; quiz 69 s vs ~10 min; visualization 66 s vs ~15 min.

**Cost of bulk processing on the local GPU** (RX 6800): transcription ≈ 1.6 GPU-h, optimization ≈ 3.4 GPU-h,
faithfulness ≈ 2 GPU-h for both chapters.

**Tests**: backend `uv run pytest -q` → 87 passed (incl. routing, CLI sandbox flags, grammar);
frontend `pnpm test` → 15 passed; `pnpm e2e` → 14/14 against the live server.

## Bugs found and fixed in this round (worth knowing)

1. **llama.cpp's JSON-schema grammar silently corrupted LaTeX.** Its string rule forbids `\l`, `\p`, `\g`…, so a
   single-backslash `\lambda_0` was forced into `\frac{\tilde{\nu}…}`, `\phi` into `\theta`, `\gamma` into
   `\bar{\nu}` — valid JSON, wrong math. Found by the faithfulness checker. Fix: our own LaTeX-tolerant GBNF
   (`model_adapters/gbnf.py`); chapters 1–2 re-optimized and re-verified. Reverts dropped from 13 to 4, fallback
   windows from 2 to 0.
2. Optimizer context was nondeterministic (notation ordered by row id; chapters optimized in parallel read each
   other's notation) → cache misses. Now ordered by book position; optimize jobs form one chain across chapters.
3. JSON escape repair missed `\rightarrow`, `\tanh`, `\textstyle`, … (now covered; tab-indented code untouched).
4. The first generated boost diagram drew the x′ axis with the wrong sign; the independent visualization review
   (added for this) catches exactly that error.

## Changes 2026-11-21: quality control

`verification/quality.py`, deterministic, runs after transcription, optimize and verify (and on demand):
- Block level: page numbers ("- 4 -") → `artifact` blocks (coverage `omitted` with reason); figure regions that
  are really text (captions, theorem boxes, stray lines) → caption/paragraph, read again; a line caught twice
  (equation crop including the next line, paragraph repeated in the next block) → copy removed.
- Unit level: KaTeX-failing LaTeX repaired (`\eqno`, upgreek, `\slashed`, letter-spaced words in `\mathrm`,
  unbalanced braces), else optional AI field dropped / AI rewrite reverted / transcription queued to be read again;
  LaTeX written outside math wrapped (only if it renders); repeated sentences and loops removed; texts the model
  stopped mid-sentence cut to the last sentence (optional fields) or reverted (main text); a unit repeating its
  neighbour merged into it.
- Open issues (verbatim passages after a failed rewrite, broken transcriptions, unchecked batches, coverage) keep a
  section `partial`; everything else makes it `done`. Report: `GET /api/documents/{id}/quality`.
- Re-processing a partial/failed chapter now really re-runs its sections (finished jobs used to make it a no-op);
  only broken transcriptions are read again and successful windows come from the cache. "Quality check" panel on
  the book page: "Check again" (no model) and "Fix with the model" (re-runs only sections that need it).
- Transcription retries once with the KaTeX error when its LaTeX does not render. Empty `n.0` chapter openings
  are skipped instead of failing the chapter. Dashed page numbers are recognised as folios during extraction.
- Dry run on 3,800 units of the processed books: 77 repeats, 76 LaTeX repairs, 61 cut-off texts.

## Changes 2026-11-20 (second round)

1. **Upload 500 on the Pi server fixed.** `POST /api/documents` was `async` and wrote SQLite on the event loop;
   while a worker held a server-side transaction (its job poll, every second) the loop froze, the worker's commit
   could not be served, and the upload died after the 30 s lock timeout. Blocking work now runs in a thread
   (regression test `test_upload_while_a_worker_holds_a_transaction`). Needs `./axiom ship-server` to reach the Pi.
2. **Teaching text** (`optimize-v3`, `faithful-v3`): Standard level emphasises key terms and claims and explains
   step by step; Deep Dive adds derivation steps, meaning, limiting cases; up to 3 study notes per unit. All AI
   additions are checked (contradicted → dropped). Re-processing a book re-runs optimize + verify (cache miss).
3. **Visualizations persist** across re-optimization (anchored to blocks) and show without another click.
4. **Cross-references** "(2.13)", "Figure 1.5", "Section 2.4", "Chapter 3" are links with hover previews.
5. **Automatic diagrams** during processing (Settings → Cloud models; default Codex, reviewed by Claude).
6. **Lecture notes get real chapters.** Outlines without "CHAPTER n" titles (Witten, Laine–Vuorinen, Deffner–
   Campbell) now give one chapter per level-1 entry, numbered as printed; front matter becomes chapter 0. Sections
   start at their outline anchor (checked against the printed title), and extraction splits text by position, so
   unstyled headings no longer matter. Already-imported documents: "re-read outline" on the document page
   (or `axiom restructure <doc_id>`), allowed until something has been extracted.

## Known problems (honest list)

1. **Faithfulness on the local model is good, not perfect.** It still flags some correct additions as “not in the
   source” and can miss subtle distortions; a premium checker (`max` profile, or set “Faithfulness checks” to a
   cloud model) would be stronger but costs subscription usage (~50 calls for two chapters).
2. **Semantic classification is mediocre** (e.g. “or in matrix notation:” labelled as a definition); it drives
   box styling and Anki card selection.
3. **Subscription CLIs**: AXIOM cannot see your plan's remaining quota; it only enforces its own daily cap
   (`AXIOM_SUBSCRIPTION_DAILY_LIMIT`). If a CLI hits its limit, “Auto” falls back to the local model; an explicit
   choice shows the error. The CLIs cannot read images here, so transcription stays local (or a paid API).
4. Paid-API adapters are implemented and unit-tested with fakes only (no key was configured).
5. Hints and quiz grades are AI judgements; solutions carry “consistent / inconsistent” evidence, hints do not.
6. Books without a PDF outline fall back to a single section; notation is shown, never normalized across books.

## Next priorities

1. Run the faithfulness check with a cloud model on one chapter and compare against the local verdicts.
2. Better unit classification (definition vs remark vs plain prose) with a small labelled set.
3. Gold set for chapter 3+ (curvature) before processing further chapters.
