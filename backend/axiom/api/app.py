"""FastAPI application: HTTP API (see docs/ARCHITECTURE.md) + optional static frontend + background worker."""

from __future__ import annotations

import json
import uuid
import os
import logging
import re
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from axiom.api.internal import install as install_internal, require_compute, role
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from axiom.api.schemas import (AskRequest, HintRequest, AskResponse, ChapterSummary, DocumentDetail, DocumentSummary, Exercise,
                               ExerciseSummary, Flag, HintResponse, ProgressReport, SearchHit, SectionSummary,
                               SectionView, SourceBlock, StageCount, Unit, UnitSource)
from axiom.config import REPO_ROOT, settings
from axiom.storage import db

log = logging.getLogger("axiom.api")


class ProcessRequest(BaseModel):
    chapters: list[int]
    profile: str = "economy"
    parallel: bool = False  # run chapters/sections concurrently; only with a premium model
    model: str | None = None  # one model for transcription, rewriting and checks of this run; None = Settings


class AnkiPushRequest(BaseModel):
    deck: str = "AXIOM"
    chapters: list[int] | None = None


class CheckRequest(BaseModel):
    answer_md: str
    model: str | None = None


class ModelChoice(BaseModel):
    model: str | None = None


class RoutingUpdate(BaseModel):
    groups: dict[str, str] = {}
    premium_default: str | None = None
    cloud_enabled: bool | None = None


class QuizRequest(BaseModel):
    scope: str  # section | chapter
    scope_id: str
    n: int = 6
    variant: int = 0
    model: str | None = None


class GradeRequest(BaseModel):
    answers: dict[str, int | str]
    model: str | None = None


def _model_errors():
    from axiom.model_adapters.base import AdapterUnavailable
    from axiom.orchestration.llm import BudgetExceeded
    return (AdapterUnavailable, BudgetExceeded)


_MODEL_ERRORS = _model_errors()


def _chapter_status(statuses: list[str]) -> str:
    if statuses and all(s == "done" for s in statuses):
        return "done"
    if any(s == "running" for s in statuses):
        return "running"
    if any(s in ("done", "partial") for s in statuses):
        return "partial"
    if any(s == "failed" for s in statuses):
        return "failed"
    return "pending"


def _doc_summary(d) -> DocumentSummary:
    chs = db.q("SELECT id FROM chapters WHERE doc_id=?", (d["id"],))
    done = 0
    for ch in chs:
        st = [r["status"] for r in db.q("SELECT status FROM sections WHERE chapter_id=?", (ch["id"],))]
        done += int(_chapter_status(st) in ("done",) or (st and all(s in ("done", "partial") for s in st)))
    return DocumentSummary(id=d["id"], title=d["title"], page_count=d["page_count"], created_at=d["created_at"],
                           chapters_total=len(chs), chapters_done=done)


def _unit(u: dict) -> Unit:
    figure_url = f"/api/blocks/{u['figure_block']}/image" if u.get("figure_block") else None
    return Unit(
        id=u["id"], kind=u["kind"], role=u.get("role", "supporting_explanation"), min_level=u.get("min_level", 2),
        text_md=u.get("text_md") or "", fast_md=u.get("fast_md"), deep_md=u.get("deep_md"), latex=u.get("latex"),
        eq_number=u.get("eq_number"), annotation_md=u.get("annotation_md"), title=u.get("title"),
        ai_generated=bool(u.get("ai_generated")),
        source=UnitSource(block_ids=u["block_ids"], pages=u.get("pages", []), coverage=u.get("coverage", "preserved"),
                          note=u.get("note")),
        flags=[Flag(**f) for f in u.get("flags", [])], figure_url=figure_url,
        verification=(u.get("verification") or {}).get("status"),
    )


def progress_report(doc_id: str) -> ProgressReport:
    d = db.q1("SELECT * FROM documents WHERE id=?", (doc_id,))
    if not d:
        raise HTTPException(404, "document not found")
    secs = db.q("SELECT status, stage FROM sections WHERE doc_id=?", (doc_id,))
    blocks = db.q1("SELECT COUNT(*) n, SUM(transcription_method!='none') t FROM blocks WHERE doc_id=?", (doc_id,))
    queued = {r["target_id"] for r in db.q("SELECT target_id FROM jobs WHERE doc_id=? AND kind='optimize'", (doc_id,))}
    opt_total = len(queued) or len(secs)
    opt_done = db.q1("SELECT COUNT(*) n FROM sections WHERE doc_id=? AND stage IN ('optimized','verified')", (doc_id,))["n"]
    ver_done = db.q1("SELECT COUNT(*) n FROM sections WHERE doc_id=? AND stage='verified'", (doc_id,))["n"]
    calls = db.q1("SELECT COUNT(*) n, SUM(cached) c, SUM(seconds) s, SUM(model_class='premium' AND cached=0) p,"
                  " SUM(CASE WHEN model_class='premium' AND cached=0 THEN COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0) END) pt"
                  " FROM llm_calls WHERE doc_id=?", (doc_id,))
    jobs = [dict(id=r["id"], kind=r["kind"], target_id=r["target_id"], status=r["status"], attempts=r["attempts"],
                 error=(r["error"] or "")[:300] or None, updated_at=r["updated_at"])
            for r in db.q("SELECT * FROM jobs WHERE doc_id=? ORDER BY created_at, rowid", (doc_id,))]
    return ProgressReport(
        document_id=doc_id, pages_total=d["page_count"], sections_total=len(secs),
        sections_done=sum(s["status"] in ("done", "partial") for s in secs),
        sections_failed=sum(s["status"] == "failed" for s in secs),
        stages=[StageCount(stage="transcribe (blocks)", done=int(blocks["t"] or 0), total=int(blocks["n"] or 0)),
                StageCount(stage="optimize + coverage audit (sections)", done=opt_done, total=opt_total),
                StageCount(stage="faithfulness check (sections)", done=ver_done, total=opt_total)],
        llm_calls=int(calls["n"] or 0), llm_cache_hits=int(calls["c"] or 0), local_seconds=round(float(calls["s"] or 0), 1),
        premium_calls=int(calls["p"] or 0), premium_tokens=int(calls["pt"] or 0), jobs=jobs,
    )


def create_app(start_worker: bool = True) -> FastAPI:
    if role() == "server":
        start_worker = False  # the server never computes; workers connect to it
    if not db.is_remote():
        db.init(settings.db_path)
    if role() == "server":
        from axiom.orchestration import jobs
        jobs.recover()  # every running job belonged to a worker that is not connected yet

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            from axiom.orchestration import jobs
            jobs.start_workers()
        yield
        if start_worker:
            from axiom.orchestration import jobs
            jobs.stop_workers()

    app = FastAPI(title="AXIOM", lifespan=lifespan)
    install_internal(app)  # worker endpoints + compute forwarding (active only in the "server" role)

    @app.get("/api/models")
    def list_models():
        """Models of the machine that computes (this one, or the connected worker when running as a server)."""
        require_compute()
        from axiom.orchestration import models
        import socket
        return dict(models.report(), machine=os.environ.get("AXIOM_WORKER_NAME") or socket.gethostname())

    @app.get("/api/settings/routing")
    def get_routing():
        from axiom.orchestration import models
        return models.load_routing()

    @app.put("/api/settings/routing")
    def put_routing(body: RoutingUpdate):
        from axiom.orchestration import models
        try:
            return models.save_routing(body.groups, body.premium_default, body.cloud_enabled)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/health")
    def health():
        if role() == "server":  # the server computes nothing: report storage and connected machines
            from axiom.api.internal import registry
            eff = registry.effective()
            return {"role": "server", "machines_online": sorted(registry.workers), "computing_machine": eff.name if eff else None}
        from axiom.orchestration.llm import get_adapter, premium_available
        local = get_adapter("local_text")
        return {"role": role(), "local_llm": getattr(local, "health", lambda: None)(), "premium_configured": premium_available()}

    @app.get("/api/imports")
    def list_imports():
        """PDFs uploaded to the server that a computing machine has not imported yet."""
        rows = db.q("SELECT id, target_id, status, error, updated_at FROM jobs WHERE kind='import' AND status!='done' "
                    "ORDER BY created_at")
        return [{"id": r["id"], "filename": r["target_id"].split("__", 1)[-1], "status": r["status"],
                 "error": (r["error"] or "").splitlines()[0][:200] if r["error"] else None} for r in rows]

    @app.get("/api/documents", response_model=list[DocumentSummary])
    def list_documents():
        return [_doc_summary(d) for d in db.q("SELECT * FROM documents ORDER BY created_at DESC")]

    @app.post("/api/documents", response_model=DocumentSummary)
    async def upload(file: UploadFile = File(...)):
        from axiom.ingestion.pdf import import_pdf
        if not (file.filename or "").lower().endswith(".pdf"):
            raise HTTPException(400, "only PDF files are supported")
        if role() == "server":  # store now; a computing machine imports it as soon as one is connected
            from axiom.orchestration import jobs
            safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", Path(file.filename).name)[:120]
            rel = f"inbox/{uuid.uuid4().hex[:12]}__{safe}"
            dest = settings.data_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(await file.read())
            jobs.enqueue_import(rel)
            return JSONResponse({"queued": True, "filename": safe}, status_code=202)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / Path(file.filename).name
            p.write_bytes(await file.read())
            try:
                doc_id = import_pdf(p)
            except Exception as e:  # malformed PDF etc.
                raise HTTPException(422, f"could not import PDF: {e}") from e
        return _doc_summary(db.q1("SELECT * FROM documents WHERE id=?", (doc_id,)))

    @app.get("/api/documents/{doc_id}", response_model=DocumentDetail)
    def get_document(doc_id: str):
        d = db.q1("SELECT * FROM documents WHERE id=?", (doc_id,))
        if not d:
            raise HTTPException(404, "document not found")
        chapters = []
        for ch in db.q("SELECT * FROM chapters WHERE doc_id=? ORDER BY number", (doc_id,)):
            secs = db.q("SELECT * FROM sections WHERE chapter_id=? ORDER BY ord", (ch["id"],))
            chapters.append(ChapterSummary(
                id=ch["id"], number=ch["number"], title=ch["title"], page_start=ch["page_start"], page_end=ch["page_end"],
                status=_chapter_status([s["status"] for s in secs]),
                sections=[SectionSummary(id=s["id"], number=s["number"], title=s["title"], page_start=s["page_start"],
                                         page_end=s["page_end"], status=s["status"], is_exercises=bool(s["is_exercises"]))
                          for s in secs]))
        return DocumentDetail(**_doc_summary(d).model_dump(), chapters=chapters)

    @app.post("/api/documents/{doc_id}/process")
    def process(doc_id: str, body: ProcessRequest):
        from axiom.orchestration import jobs
        from axiom.orchestration import models
        if body.profile not in ("economy", "balanced", "max"):
            raise HTTPException(400, "unknown profile")
        model = None if body.model in (None, "", "auto") else body.model
        if model and role() != "server" and model not in {m["id"] for m in models.catalog() if m["vision"]}:
            raise HTTPException(400, f"{model} cannot process scans (needs a model that reads images)")
        if body.parallel and (not model or models.kind(model) == "local"):
            raise HTTPException(400, "parallel processing needs a premium model (Claude Code, Codex or an API); "
                                     "the local model has a single GPU")
        return {"job_ids": jobs.enqueue_document(doc_id, body.chapters, body.profile, model, body.parallel)}

    @app.post("/api/jobs/reset")
    def reset_jobs(doc_id: str | None = None, include_done: bool = False):
        """Clean slate for the queue (e.g. after a code update): drops pending / running / failed / cancelled jobs
        and puts their sections back to 'pending'. Finished work (blocks, units, caches) is kept, so queueing the
        chapters again skips what is done. `include_done` also forgets finished jobs. Stop the workers first."""
        from axiom.orchestration import jobs
        if role() == "server":
            from axiom.api.internal import _reap
            _reap(15)  # a dead worker's open transaction would block the writes below
        return jobs.reset(doc_id, include_done)

    @app.post("/api/documents/{doc_id}/cancel")
    def cancel(doc_id: str):
        from axiom.orchestration import jobs
        return {"cancelled": jobs.cancel_document(doc_id)}

    @app.get("/api/documents/{doc_id}/progress", response_model=ProgressReport)
    def progress(doc_id: str):
        return progress_report(doc_id)

    @app.get("/api/sections/{section_id}", response_model=SectionView)
    def get_section(section_id: str):
        s = db.q1("SELECT s.*, c.number AS chapter_number FROM sections s JOIN chapters c ON c.id=s.chapter_id WHERE s.id=?",
                  (section_id,))
        if not s:
            raise HTTPException(404, "section not found")
        units = [_unit(json.loads(r["data"])) for r in db.q("SELECT data FROM units WHERE section_id=? ORDER BY ord", (section_id,))]
        # equation numbers come from the source block (single source of truth; fixes propagate without re-optimizing)
        eqnums = {r["id"]: r["eq_number"] for r in db.q(
            "SELECT id, eq_number FROM blocks WHERE section_id=? AND block_type='equation'", (section_id,))}
        for u in units:
            if u.kind == "equation" and len(u.source.block_ids) == 1 and u.source.block_ids[0] in eqnums:
                u.eq_number = eqnums[u.source.block_ids[0]]
        # the section's own heading is rendered by the reader as the section title
        units = [u for u in units if not (u.kind == "heading" and u.text_md.strip().lower() == s["title"].strip().lower())]
        exercises = [ExerciseSummary(id=e["id"], number=f"{e['chapter_number']}.{e['number']}", statement_md=e["statement_md"],
                                     page=e["page"])
                     for e in db.q("SELECT * FROM exercises WHERE section_id=? ORDER BY CAST(number AS INTEGER)", (section_id,))]
        order = db.q("SELECT id FROM sections WHERE doc_id=? ORDER BY CAST(substr(chapter_id, instr(chapter_id,'-c')+2) AS INTEGER), ord",
                     (s["doc_id"],))
        ids = [r["id"] for r in order]
        i = ids.index(section_id)
        return SectionView(id=s["id"], document_id=s["doc_id"], chapter_number=s["chapter_number"], number=s["number"],
                           title=s["title"], status=s["status"], summary_md=s["digest_md"] or s["summary_md"],
                           verified=s["stage"] == "verified" and bool(units), units=units, exercises=exercises,
                           prev_section_id=ids[i - 1] if i > 0 else None,
                           next_section_id=ids[i + 1] if i + 1 < len(ids) else None)

    @app.get("/api/blocks/{block_id}", response_model=SourceBlock)
    def get_block(block_id: str):
        b = db.q1("SELECT * FROM blocks WHERE id=?", (block_id,))
        if not b:
            raise HTTPException(404, "block not found")
        return SourceBlock(id=b["id"], document_id=b["doc_id"], section_id=b["section_id"], page=b["page"],
                           bbox=(b["x0"], b["y0"], b["x1"], b["y1"]), block_type=b["block_type"], raw_text=b["raw_text"],
                           transcribed_md=b["transcribed_md"], transcription_method=b["transcription_method"],
                           confidence=b["confidence"], eq_number=b["eq_number"] if b["block_type"] == "equation" else None,
                           image_url=f"/api/blocks/{b['id']}/image")

    @app.get("/api/blocks/{block_id}/image")
    def block_image(block_id: str):
        from axiom.extraction.extract import block_crop_path, crop_png
        b = db.q1("SELECT * FROM blocks WHERE id=?", (block_id,))
        if not b:
            raise HTTPException(404, "block not found")
        p = block_crop_path(b["doc_id"], block_id)
        if not p.exists():
            require_compute()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(crop_png(b["doc_id"], b["page"], (b["x0"], b["y0"], b["x1"], b["y1"])))
        return FileResponse(p, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/documents/{doc_id}/pages/{page}/image")
    def page_image(doc_id: str, page: int):
        from axiom.ingestion.pdf import open_pdf
        d = db.q1("SELECT page_count FROM documents WHERE id=?", (doc_id,))
        if not d or not (1 <= page <= d["page_count"]):
            raise HTTPException(404, "page not found")
        p = settings.doc_dir(doc_id) / "pages" / f"{page:04d}.png"
        if not p.exists():
            require_compute()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(open_pdf(doc_id)[page - 1].get_pixmap(dpi=130).tobytes("png"))
        return FileResponse(p, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

    @app.post("/api/ask", response_model=AskResponse)
    def ask(req: AskRequest):
        require_compute()
        from axiom.model_adapters.base import AdapterUnavailable
        from axiom.orchestration.llm import BudgetExceeded
        from axiom.retrieval.context import ask as do_ask
        try:
            answer, model, cached, used = do_ask(req)
        except KeyError:
            raise HTTPException(404, "section not found")
        except (BudgetExceeded, AdapterUnavailable) as e:
            raise HTTPException(409, str(e))
        return AskResponse(answer_md=answer, model=model, cached=cached, context_used=used)

    @app.get("/api/exercises/{exercise_id}", response_model=Exercise)
    def get_exercise(exercise_id: str):
        from axiom.exercises.engine import related_sections
        e = db.q1("SELECT * FROM exercises WHERE id=?", (exercise_id,))
        if not e:
            raise HTTPException(404, "exercise not found")
        return Exercise(id=e["id"], document_id=e["doc_id"], chapter_number=e["chapter_number"],
                        number=f"{e['chapter_number']}.{e['number']}", statement_md=e["statement_md"], page=e["page"],
                        block_ids=json.loads(e["block_ids"]), related_section_ids=related_sections(e))

    @app.post("/api/exercises/{exercise_id}/hint", response_model=HintResponse)
    def get_hint(exercise_id: str, body: HintRequest):
        require_compute()
        from axiom.exercises.engine import hint
        try:
            text, model, cached = hint(exercise_id, body.level, model=body.model)
        except KeyError:
            raise HTTPException(404, "exercise not found")
        except _MODEL_ERRORS as e:
            raise HTTPException(409, str(e))
        return HintResponse(level=max(1, min(5, body.level)), hint_md=text, model=model, cached=cached)

    @app.get("/api/search", response_model=list[SearchHit])
    def search(doc_id: str, q: str):
        terms = re.findall(r"\w+", q)
        if not terms:
            return []
        fts = " ".join(f'"{t}"' for t in terms[:8])
        rows = db.q("SELECT block_id, section_id, snippet(search_fts, 3, '', '', '…', 24) AS snip FROM search_fts"
                    " WHERE search_fts MATCH ? AND doc_id=? ORDER BY rank LIMIT 60", (fts, doc_id))
        hits, seen = [], set()
        for r in rows:
            s = db.q1("SELECT number, title FROM sections WHERE id=?", (r["section_id"],))
            # prefer unit hits (what the reader shows); map block hits to a page number
            if "-u" in r["block_id"]:
                u = db.q1("SELECT data FROM units WHERE id=?", (r["block_id"],))
                page = (json.loads(u["data"]).get("pages") or [0])[0] if u else 0
            else:
                b = db.q1("SELECT page FROM blocks WHERE id=?", (r["block_id"],))
                page = b["page"] if b else 0
            key = (r["section_id"], page)
            if key in seen:
                continue
            seen.add(key)
            hits.append(SearchHit(section_id=r["section_id"], section_title=f"{s['number']} {s['title']}",
                                  block_id=r["block_id"], snippet=r["snip"], page=page))
            if len(hits) >= 20:
                break
        return hits

    @app.get("/api/documents/{doc_id}/anki")
    def anki_export(doc_id: str, deck: str = "AXIOM", chapters: str | None = None, format: str = "tsv"):
        require_compute()
        from axiom.anki.export import build_cards, export_path, to_apkg, to_tsv
        chs = [int(x) for x in chapters.split(",")] if chapters else None
        cards = build_cards(doc_id, chs)
        if format == "json":  # preview
            return cards
        path = export_path(doc_id, deck, format)  # also kept on disk under data/exports/
        if format == "apkg":
            to_apkg(cards, deck, path)
            return FileResponse(path, media_type="application/octet-stream", filename=path.name)
        path.write_text(to_tsv(cards, deck), encoding="utf-8")
        return FileResponse(path, media_type="text/tab-separated-values", filename=path.name)

    @app.post("/api/documents/{doc_id}/anki/push")
    def anki_push(doc_id: str, body: AnkiPushRequest):
        require_compute()
        from axiom.anki.export import build_cards, push_ankiconnect
        try:
            return push_ankiconnect(build_cards(doc_id, body.chapters), body.deck)
        except ConnectionError as e:
            raise HTTPException(503, str(e))
        except RuntimeError as e:
            raise HTTPException(502, str(e))

    # ------------------------------------------------------------ solutions, answer checks
    @app.get("/api/exercises/{exercise_id}/solution")
    def get_solution(exercise_id: str):
        from axiom.exercises.solutions import get_solution as gs
        sol = gs(exercise_id, generate=False)
        if not sol:
            raise HTTPException(404, "no solution generated yet")
        return sol

    @app.post("/api/exercises/{exercise_id}/solution")
    def make_solution(exercise_id: str, body: ModelChoice | None = None):
        require_compute()
        from axiom.exercises.solutions import get_solution as gs
        try:
            return gs(exercise_id, model=body.model if body else None)
        except KeyError:
            raise HTTPException(404, "exercise not found")
        except _MODEL_ERRORS as e:
            raise HTTPException(409, str(e))

    @app.post("/api/exercises/{exercise_id}/check")
    def check(exercise_id: str, body: CheckRequest):
        require_compute()
        from axiom.exercises.solutions import check_answer
        try:
            return check_answer(exercise_id, body.answer_md, model=body.model)
        except KeyError:
            raise HTTPException(404, "exercise not found")
        except _MODEL_ERRORS as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/documents/{doc_id}/exercise-map")
    def exercise_map(doc_id: str):
        """Exercise-first mode: every exercise with the sections it draws on (cheap, deterministic)."""
        from axiom.exercises.engine import related_sections
        out = []
        for e in db.q("SELECT * FROM exercises WHERE doc_id=? ORDER BY chapter_number, CAST(number AS INTEGER)", (doc_id,)):
            out.append({"id": e["id"], "number": f"{e['chapter_number']}.{e['number']}", "chapter_number": e["chapter_number"],
                        "statement_md": e["statement_md"], "related_section_ids": related_sections(e)})
        return out

    # ------------------------------------------------------------ mastery checks
    @app.post("/api/quizzes")
    def make_quiz(body: QuizRequest):
        require_compute()
        from axiom.exercises.mastery import generate_quiz, public_quiz
        try:
            return public_quiz(generate_quiz(body.scope, body.scope_id, body.n, variant=body.variant, model=body.model))
        except KeyError:
            raise HTTPException(404, "scope not found")
        except _MODEL_ERRORS as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/quizzes/{quiz_id}/grade")
    def grade_quiz(quiz_id: str, body: GradeRequest):
        require_compute()
        from axiom.exercises.mastery import grade
        try:
            return grade(quiz_id, body.answers, model=body.model)
        except KeyError:
            raise HTTPException(404, "quiz not found")
        except _MODEL_ERRORS as e:
            raise HTTPException(409, str(e))

    @app.get("/api/documents/{doc_id}/mastery")
    def get_mastery(doc_id: str):
        from axiom.exercises.mastery import mastery
        return mastery(doc_id)

    # ------------------------------------------------------------ sandboxed visualizations
    @app.get("/api/units/{unit_id}/visualization")
    def get_viz(unit_id: str):
        row = db.q1("SELECT data FROM visualizations WHERE unit_id=?", (unit_id,))
        if not row:
            raise HTTPException(404, "no visualization yet")
        d = json.loads(row["data"])
        return {k: d.get(k) for k in ("unit_id", "title", "description_md", "model", "ai_generated", "reviewed",
                                      "review_notes")}

    @app.post("/api/units/{unit_id}/visualization")
    def make_viz(unit_id: str, regenerate: bool = False, model: str | None = None):
        require_compute()
        from axiom.orchestration.llm import ValidationFailed
        from axiom.visualization.generate import visualize
        try:
            d = visualize(unit_id, regenerate=regenerate, model=model)
        except KeyError:
            raise HTTPException(404, "unit not found")
        except _MODEL_ERRORS as e:
            raise HTTPException(409, str(e))
        except ValidationFailed as e:
            log.warning("visualization failed for %s: %s", unit_id, e)
            raise HTTPException(422, f"could not generate a safe, correct visualization: {str(e)[:400]}")
        return {k: d.get(k) for k in ("unit_id", "title", "description_md", "model", "ai_generated", "reviewed",
                                      "review_notes")}

    @app.get("/api/units/{unit_id}/visualization.html", include_in_schema=False)
    def viz_html(unit_id: str):
        from axiom.visualization.generate import CSP, document
        row = db.q1("SELECT data FROM visualizations WHERE unit_id=?", (unit_id,))
        if not row:
            raise HTTPException(404, "no visualization yet")
        return Response(document(json.loads(row["data"])), media_type="text/html",
                        headers={"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})

    dist = REPO_ROOT / "frontend" / "dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                return Response(status_code=404)
            f = dist / path
            return FileResponse(f if (path and f.is_file()) else dist / "index.html")

    return app
