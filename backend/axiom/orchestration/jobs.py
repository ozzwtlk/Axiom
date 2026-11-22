"""Persistent job queue in SQLite.

Task contract (one row per job): kind, target_id, depends_on (job ids), profile, attempts/max_attempts,
status ∈ pending | running | done | failed | cancelled, progress JSON, error.

Job graph per chapter:  extract(chapter) → transcribe(section_i) → optimize(section_i) → verify(section_i)
Parallel runs end with one reconcile job: book-wide notation check, serial re-optimize of conflicts.
optimize(section_i) also depends on optimize(section_{i-1}) because it reuses its summary as context.
Transcription jobs of different sections are independent and may run concurrently.

On startup, jobs left 'running' by a crashed process are reset to 'pending'; all stages are idempotent
and checkpointed at block level, so completed work is never redone.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone

from axiom.storage import db

log = logging.getLogger("axiom.jobs")
_stop = threading.Event()
_PIPELINE_STAGES = ("transcribe", "optimize", "verify")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _add(c, doc_id: str, kind: str, target: str, deps: list[str], profile: str, model: str | None = None,
         parallel: bool = False) -> str:
    existing = c.execute("SELECT id, status FROM jobs WHERE kind=? AND target_id=? AND status IN ('pending','running','done')",
                         (kind, target)).fetchone()
    if existing:
        return existing["id"]
    jid = uuid.uuid4().hex[:12]
    c.execute("INSERT INTO jobs(id,doc_id,kind,target_id,status,depends_on,profile,model,parallel,created_at,updated_at)"
              " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              (jid, doc_id, kind, target, "pending", json.dumps(deps), profile, model, int(parallel), _now(), _now()))
    return jid


def enqueue_document(doc_id: str, chapter_numbers: list[int], profile: str = "economy",
                     model: str | None = None, parallel: bool = False) -> list[str]:
    """`model`: one model for all bulk stages of this run (e.g. "claude-code:haiku"); None = routing settings.
    `parallel` (premium model only, enforced by the caller): the optimize chain is per chapter instead of one chain
    across the book, so chapters are rewritten concurrently, and extra job threads pick these jobs up. Trade-off:
    a chapter's notation context may then include a still-moving earlier chapter (less stable cache keys)."""
    ids: list[str] = []
    # optimize jobs form ONE chain across chapters: a section's context (summaries, notation) comes from all earlier
    # sections of the book, so it must not depend on how far a parallel chapter has got (stable cache keys)
    prev_opt = None
    with db.connect() as c:
        _lock_document(c, doc_id)
        for n in sorted(chapter_numbers):
            if parallel:
                prev_opt = None
            ch = c.execute("SELECT * FROM chapters WHERE doc_id=? AND number=?", (doc_id, n)).fetchone()
            if not ch:
                continue
            ex = _extract_job(c, doc_id, ch, profile, model, parallel)
            ids.append(ex)
            for s in c.execute("SELECT * FROM sections WHERE chapter_id=? ORDER BY ord", (ch["id"],)).fetchall():
                if s["status"] in ("partial", "failed") and not c.execute(
                    "SELECT 1 FROM jobs WHERE target_id=? AND kind IN ('transcribe','optimize','verify') "
                    "AND status IN ('pending','running')", (s["id"],)
                ).fetchone():
                    _forget_section_jobs(c, s["id"])  # finished jobs would make this a no-op: run it again
                tr = _add(c, doc_id, "transcribe", s["id"], [ex], profile, model, parallel)
                op = _add(c, doc_id, "optimize", s["id"], [tr] + (["~" + prev_opt] if prev_opt else []), profile, model, parallel)
                vf = _add(c, doc_id, "verify", s["id"], [op], profile, model, parallel)
                ids += [tr, op, vf]
                prev_opt = op
                c.execute("UPDATE sections SET status='pending' WHERE id=? AND status NOT IN ('done','partial')", (s["id"],))
        if parallel and ids:
            # after everything of this run: check the notation book-wide and re-optimize contradicting sections.
            # Not "parallel": runs on a main optimize thread, serially, with the full book-wide context.
            soft = ["~" + i for i in ids]
            nums = ",".join(str(n) for n in sorted(chapter_numbers))
            ids.append(_add(c, doc_id, "reconcile", f"{doc_id}@{nums}", soft, profile, model, False))
    return ids


def _forget_section_jobs(c, section_id: str) -> None:
    """Make the next run of a section real: drop its finished/failed stage jobs and mark the transcriptions the
    quality check found broken as unread. Everything that was fine comes back from the cache."""
    from axiom.verification.quality import blocks_to_reread
    c.execute("DELETE FROM jobs WHERE target_id=? AND kind IN ('transcribe','optimize','verify') AND status!='running'",
              (section_id,))
    bad = blocks_to_reread(section_id)
    if bad:
        c.execute(f"UPDATE blocks SET transcription_method='none', transcribed_md=NULL WHERE id IN ({','.join('?' * len(bad))})"
                  " AND section_id=?", (*bad, section_id))
    c.execute("DELETE FROM meta WHERE key=?", (f"empty:{section_id}",))


def requeue_section(section_id: str, profile: str = "economy", model: str | None = None) -> list[str]:
    """transcribe → optimize → verify again for one section (what the quality check could not fix itself)."""
    sec = db.q1("SELECT doc_id FROM sections WHERE id=?", (section_id,))
    with db.connect() as c:
        if c.execute("SELECT 1 FROM jobs WHERE target_id=? AND status='running' AND kind IN ('transcribe','optimize',"
                     "'verify')", (section_id,)).fetchone():
            return []
        _forget_section_jobs(c, section_id)
        tr = _add(c, sec["doc_id"], "transcribe", section_id, [], profile, model)
        op = _add(c, sec["doc_id"], "optimize", section_id, [tr], profile, model)
        return [tr, op, _add(c, sec["doc_id"], "verify", section_id, [op], profile, model)]


def enqueue_quality(doc_id: str, rerun: bool, profile: str = "economy", model: str | None = None) -> list[str]:
    """Check every processed section of a document (no model). With `rerun`, sections that still need a model
    (verbatim passages, broken transcriptions, unchecked batches) are queued again — only those parts cost calls."""
    ids = []
    with db.connect() as c:
        _lock_document(c, doc_id)
        for s in c.execute("SELECT id FROM sections WHERE doc_id=? AND status IN ('done','partial','failed') ORDER BY ord",
                           (doc_id,)).fetchall():
            c.execute("DELETE FROM jobs WHERE kind='quality' AND target_id LIKE ? AND status!='running'", (s["id"] + "|%",))
            ids.append(_add(c, doc_id, "quality", f"{s['id']}|{'rerun' if rerun else 'check'}", [], profile, model))
    return ids


def _run_quality(job: dict) -> dict:
    from axiom.verification import quality
    section_id, _, mode = job["target_id"].partition("|")
    sec = db.q1("SELECT status FROM sections WHERE id=?", (section_id,))
    if not sec:
        return {"skipped": "section no longer exists"}
    r = quality.check_section(section_id) if sec["status"] != "failed" else {"fixed": {}, "needs": ["optimize"]}
    if mode == "rerun" and r.get("needs"):
        r["requeued"] = requeue_section(section_id, job["profile"], job["model"])
    return r


def _extract_job(c, doc_id: str, ch, profile: str, model: str | None, parallel: bool) -> str:
    """The job that makes the chapter's blocks. A chapter that already has blocks but with sections that got none
    (their heading was not recognised once, so the text sits in the previous section) is extracted again: a
    "reextract" job replaces the stale queue entries of the chapter, so nothing runs against half-old blocks."""
    secs = [s["id"] for s in c.execute("SELECT id FROM sections WHERE chapter_id=?", (ch["id"],))]
    marks = ",".join("?" * len(secs)) or "NULL"
    have = {r["section_id"] for r in c.execute(f"SELECT DISTINCT section_id FROM blocks WHERE section_id IN ({marks})", secs)}
    existing = c.execute("SELECT id FROM jobs WHERE target_id=? AND kind IN ('extract','reextract') "
                         "AND status IN ('pending','running')", (ch["id"],)).fetchone()
    if existing:
        return existing["id"]
    if not have or len(have) == len(secs) or c.execute(
        f"SELECT 1 FROM jobs WHERE status='running' AND target_id IN ({marks}) "
        "AND kind IN ('transcribe','optimize','verify')", secs
    ).fetchone():
        return _add(c, doc_id, "extract", ch["id"], [], profile, model, parallel)
    c.execute(f"DELETE FROM jobs WHERE status IN ('pending','failed','cancelled') AND target_id IN ({marks})"
              " AND kind IN ('transcribe','optimize','verify')", secs)
    c.execute("DELETE FROM jobs WHERE kind='reextract' AND target_id=?", (ch["id"],))
    log.info("chapter %s has sections without blocks: re-extracting", ch["number"])
    return _add(c, doc_id, "reextract", ch["id"], [], profile, model, parallel)


def cancel(job_id: str) -> None:
    with db.connect(immediate=True) as c:
        job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job and job["status"] == "running":
            c.execute("UPDATE jobs SET requested_action='abort',restart_model=NULL,updated_at=? WHERE id=?", (_now(), job_id))
        elif job and job["status"] == "pending":
            _cancel_tree(c, job, "Removed from queue")


def cancel_document(doc_id: str) -> int:
    with db.connect(immediate=True) as c:
        rows = c.execute("SELECT * FROM jobs WHERE doc_id=? AND status IN ('pending','running')", (doc_id,)).fetchall()
        c.execute("UPDATE jobs SET requested_action='abort',restart_model=NULL,updated_at=? "
                  "WHERE doc_id=? AND status='running'", (_now(), doc_id))
        for row in rows:
            if row["status"] == "pending":
                _cancel_row(c, row, "Removed from queue")
        return len(rows)


def reset(doc_id: str | None = None, include_done: bool = False) -> dict:
    where = "" if not doc_id else " AND doc_id=?"
    args = (doc_id,) if doc_id else ()
    statuses = "('pending','running','failed','cancelled'" + (",'done'" if include_done else "") + ")"
    with db.connect() as c:
        # imports waiting in the inbox are not part of a document's run
        n = c.execute(f"DELETE FROM jobs WHERE status IN {statuses} AND kind!='import'{where}", args).rowcount
        s = c.execute("UPDATE sections SET status='pending' WHERE status IN ('running','failed')"
                      + (" AND doc_id=?" if doc_id else ""), args).rowcount
    return {"jobs_removed": n, "sections_reset": s}


# name of this machine when it runs as a worker for a server (None: all-in-one, this process owns the queue)
WORKER_NAME: str | None = None


def enqueue_import(rel: str) -> str:
    """A PDF uploaded to the server's inbox, imported by the next computing machine."""
    with db.connect() as c:
        return _add(c, "inbox", "import", rel, [], "economy")


def recover(worker: str | None = None) -> int:
    owner = worker or WORKER_NAME
    with db.connect(immediate=True) as c:
        own = " AND worker=?" if owner else ""
        rows = c.execute("SELECT * FROM jobs WHERE status='running'" + own, (owner,) if owner else ()).fetchall()
        for row in rows:
            if not _apply_request(c, row):
                c.execute("UPDATE jobs SET status='pending', attempts=MAX(0, attempts-1), worker=NULL, run_token=NULL, "
                          "updated_at=? WHERE id=?", (_now(), row["id"]))
                _release_section(c, row)
        if not owner:
            c.execute("UPDATE sections SET status='pending' WHERE status='running'")
    return len(rows)


_GROUP_OF_KIND = {"transcribe": "transcribe", "optimize": "optimize", "verify": "faithfulness",
                  "reconcile": "optimize", "diagram": "visualize", "anki": "anki", "cover": "cover"}


def _lock_document(c, doc_id):
    if not c.execute("UPDATE documents SET id=id WHERE id=?", (doc_id,)).rowcount:
        raise KeyError(doc_id)


def enqueue_cover(doc_id: str) -> str:
    with db.connect() as c:
        _lock_document(c, doc_id)
        c.execute("DELETE FROM jobs WHERE doc_id=? AND kind='cover' AND status='done'", (doc_id,))
        return _add(c, doc_id, "cover", doc_id, [], "economy", "codex:default")


def enqueue_anki(doc_id: str, regenerate: bool = False, model: str | None = None) -> list[str]:
    model = _validate_model("anki", model)
    ids = []
    with db.connect() as c:
        _lock_document(c, doc_id)
        rows = c.execute("SELECT s.id, sc.section_id AS generated FROM sections s LEFT JOIN study_cards sc "
                         "ON sc.section_id=s.id WHERE s.doc_id=? AND s.stage='verified' AND s.is_exercises=0 "
                         "ORDER BY s.ord", (doc_id,)).fetchall()
        for row in rows:
            sid = row["id"]
            if row["generated"] and not regenerate:
                continue
            c.execute("DELETE FROM jobs WHERE target_id=? AND kind='anki' AND status='done'", (sid,))
            ids.append(_add(c, doc_id, "anki", sid, [], "economy", model))
    return ids


def queue_snapshot() -> list[dict]:
    return [dict(r, depends_on=json.loads(r["depends_on"]), model_group=_GROUP_OF_KIND.get(r["kind"]))
            for r in db.q("SELECT j.id,j.doc_id,j.kind,j.target_id,j.status,j.depends_on,j.model,j.worker,"
                          "j.priority,j.parallel,j.error,j.requested_action,j.restart_model,d.title "
                          "FROM jobs j LEFT JOIN documents d ON d.id=j.doc_id "
                          "ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END, "
                          "CASE WHEN j.status='pending' THEN j.priority END, "
                          "CASE WHEN j.status NOT IN ('running','pending') THEN j.updated_at END DESC,j.created_at,j.rowid")]


def _validate_model(kind: str, model: str | None) -> str | None:
    from axiom.orchestration import models
    if kind not in _GROUP_OF_KIND:
        raise ValueError("This job does not use a model")
    model = None if model in (None, "auto", "") else model
    if kind == "cover" and (not model or not model.startswith("codex:")):
        raise ValueError("Cover jobs require Codex")
    if model and not any(m["id"] == model and (kind != "transcribe" or m["vision"]) for m in models.catalog()):
        raise ValueError("Unknown or incompatible model")
    return model


def _release_section(c, job, cancelled: bool = False) -> None:
    if job["kind"] not in _PIPELINE_STAGES:
        return
    target = job["target_id"]
    c.execute("UPDATE sections SET status='pending' WHERE id=? AND status='running' AND NOT EXISTS "
              "(SELECT 1 FROM jobs WHERE target_id=? AND status='running' AND kind IN ('transcribe','optimize','verify'))",
              (target, target))
    if cancelled and job["kind"] == "verify":
        c.execute("UPDATE sections SET status='partial' WHERE id=? AND stage='optimized' AND status='done'", (target,))


def _cancel_row(c, job, reason: str) -> None:
    c.execute("UPDATE jobs SET status='cancelled',error=?,updated_at=?,worker=NULL,run_token=NULL,"
              "requested_action=NULL,restart_model=NULL WHERE id=?", (reason, _now(), job["id"]))
    _release_section(c, job, cancelled=True)


def _cancel_tree(c, job, reason: str) -> None:
    # Keep tombstones, so ordering-only dependencies can proceed without inheriting failure.
    cancelled = {job["id"]}
    pending = c.execute("SELECT * FROM jobs WHERE doc_id=? AND status='pending'", (job["doc_id"],)).fetchall()
    while True:
        more = {r["id"] for r in pending if cancelled.intersection(json.loads(r["depends_on"]))}
        if more <= cancelled:
            break
        cancelled.update(more)
    _cancel_row(c, job, reason)
    for row in pending:
        if row["id"] in cancelled and row["id"] != job["id"]:
            _cancel_row(c, row, "Required job was cancelled")


def _apply_request(c, row) -> bool:
    if row["requested_action"] == "abort":
        _cancel_tree(c, row, "Aborted by user")
    elif row["requested_action"] == "restart":
        c.execute("UPDATE jobs SET status='pending', model=restart_model, attempts=0, worker=NULL, run_token=NULL, "
                  "error=NULL, progress='{}', requested_action=NULL, restart_model=NULL, updated_at=? WHERE id=?",
                  (_now(), row["id"]))
        _release_section(c, row)
    else:
        return False
    return True


def request_running(job_id: str, action: str, model: str | None = None) -> None:
    if action not in ("abort", "restart"):
        raise ValueError("Unknown job action")
    with db.connect(immediate=True) as c:
        row = c.execute("SELECT * FROM jobs WHERE id=? AND status='running'", (job_id,)).fetchone()
        if not row or row["requested_action"]:
            raise ValueError("Job is no longer running or already has a pending action; refresh the queue.")
        if action == "restart":
            model = _validate_model(row["kind"], model)
            if model == row["model"]:
                raise ValueError("Select a different model to restart this job")
        c.execute("UPDATE jobs SET requested_action=?, restart_model=?, updated_at=? WHERE id=?",
                  (action, model, _now(), job_id))


def update_pending(job_id: str, *, model: str | None = None, move_first: bool = False, remove: bool = False) -> None:
    with db.connect(immediate=True) as c:
        job = c.execute("SELECT * FROM jobs WHERE id=? AND status='pending'", (job_id,)).fetchone()
        if not job:
            raise ValueError("Only waiting jobs can be changed; refresh the queue.")
        if remove:
            _cancel_tree(c, job, "Removed from queue")
        elif move_first:
            c.execute("UPDATE jobs SET priority=(SELECT MIN(priority)-1 FROM jobs WHERE status='pending'),"
                      "updated_at=? WHERE id=?", (_now(), job_id))
        else:
            model = _validate_model(job["kind"], model)
            c.execute("UPDATE jobs SET model=?,updated_at=? WHERE id=?", (model, _now(), job_id))

MAX_DIAGRAMS_PER_SECTION = 2


def enqueue_diagrams(section_id: str, profile: str = "balanced") -> list[str]:
    """After a section is optimized: the units the optimizer marked as worth a picture get a diagram job each,
    on the model chosen in Settings (default Codex). They run on their own thread, alongside the rest of the run,
    and never hold up or fail the section. The target is the unit's source blocks (stable across re-optimizing)."""
    from axiom.orchestration import models
    mid = models.load_routing()["auto_diagrams"]
    if mid == "off" or not models.available(mid):
        return []
    sec = db.q1("SELECT doc_id FROM sections WHERE id=?", (section_id,))
    have = {r["unit_id"] for r in db.q("SELECT unit_id FROM visualizations WHERE unit_id LIKE ?", (section_id + "-u%",))}
    ids: list[str] = []
    with db.connect() as c:
        for r in c.execute("SELECT id, data FROM units WHERE section_id=? ORDER BY ord", (section_id,)).fetchall():
            if len(ids) >= MAX_DIAGRAMS_PER_SECTION:
                break
            u = json.loads(r["data"])
            if u.get("diagram_idea") and r["id"] not in have:
                ids.append(_add(c, sec["doc_id"], "diagram", f"{section_id}|{','.join(u['block_ids'])}", [], profile, mid))
    return ids


def _run_diagram(job: dict) -> dict:
    from axiom.model_adapters.base import AdapterUnavailable
    from axiom.orchestration.llm import BudgetExceeded, ValidationFailed
    from axiom.visualization.generate import visualize
    section_id, _, blocks = job["target_id"].partition("|")
    want = set(blocks.split(","))
    unit = None
    for r in db.q("SELECT id, data FROM units WHERE section_id=? ORDER BY ord", (section_id,)):
        u = json.loads(r["data"])
        if set(u.get("block_ids", [])) == want:
            unit = dict(u, id=r["id"])
            break
    if not unit or not unit.get("diagram_idea"):
        return {"skipped": "the passage changed since the diagram was suggested"}
    try:
        v = visualize(unit["id"], job["profile"], model=job["model"], idea=unit["diagram_idea"])
    except (ValidationFailed, AdapterUnavailable, BudgetExceeded) as e:  # an optional extra: never fail the run
        log.info("automatic diagram for %s skipped: %s", unit["id"], e)
        return {"skipped": str(e)[:300]}
    return {"unit_id": unit["id"], "title": v.get("title"), "model": v.get("model")}


def can_run(job, selected: str = "auto") -> bool:
    """May this machine take the job? Only the chosen computing machine (or anyone under "auto"), and only if it
    has the model the job needs — so a Qwen run waits for the PC instead of failing on a laptop."""
    if WORKER_NAME and selected not in ("auto", WORKER_NAME):
        return False
    group = _GROUP_OF_KIND.get(job["kind"])
    if not group:
        return True  # import / extract need no model
    from axiom.orchestration import models
    need = job["model"] if "model" in job.keys() and job["model"] else models.resolve(group, profile=job["profile"]).model
    return models.available("local" if need in ("local_text", "local_vlm") else need)


_claim_lock = threading.Lock()


def _claim(kinds: tuple[str, ...], parallel_only: bool = False) -> dict | None:
    with _claim_lock, db.connect(immediate=True) as c:
        done = {r["id"] for r in c.execute("SELECT id FROM jobs WHERE status='done'")}
        failed = {r["id"] for r in c.execute("SELECT id FROM jobs WHERE status IN ('failed','cancelled')")}
        active = {r["id"] for r in c.execute("SELECT id FROM jobs WHERE status IN ('pending','running')")}
        # "~<id>" = ordering-only dependency: wait until that job has finished, but do not inherit its failure
        # (the optimize chain: a failed earlier section must not fail every later section and chapter)
        sel_row = c.execute("SELECT value FROM meta WHERE key='compute.selected'").fetchone()
        selected = (sel_row["value"] if sel_row else None) or "auto"
        for j in c.execute(f"SELECT * FROM jobs WHERE status='pending' AND kind IN ({','.join('?' * len(kinds))})"
                           " ORDER BY priority, created_at, rowid", kinds).fetchall():
            if parallel_only and not j["parallel"]:
                continue
            all_deps = json.loads(j["depends_on"])
            deps = [d for d in all_deps if not d.startswith("~")]
            soft = [d[1:] for d in all_deps if d.startswith("~")]
            if any(d in active for d in soft):
                continue
            if any(d in failed or (d not in done and d not in active) for d in deps):
                c.execute("UPDATE jobs SET status='failed', error='upstream dependency failed', updated_at=? WHERE id=?",
                          (_now(), j["id"]))
                c.execute("UPDATE sections SET status='failed' WHERE id=? AND status NOT IN ('done','partial')",
                          (j["target_id"],))
                continue
            if all(d in done for d in deps) and can_run(j, selected):
                token = uuid.uuid4().hex
                c.execute("UPDATE jobs SET status='running', attempts=attempts+1, worker=?, run_token=?, updated_at=? WHERE id=?",
                          (WORKER_NAME, token, _now(), j["id"]))
                return dict(j, run_token=token)
    return None


def _run(job: dict) -> None:
    from axiom.orchestration import llm, models

    def should_stop():
        if _stop.is_set():
            return True
        row = db.q1("SELECT run_token,requested_action FROM jobs WHERE id=?", (job["id"],))
        return not row or row["run_token"] != job.get("run_token") or bool(row["requested_action"])

    def checkpoint():
        if should_stop():
            raise InterruptedError("Job interrupted")

    token = models.processing_model.set(job.get("model"))
    ptoken = models.processing_parallel.set(bool(job.get("parallel")))
    stop_token = llm.job_checkpoint.set(checkpoint)
    try:
        checkpoint()
        _run_kind(job, job["kind"], job["target_id"], job["profile"], should_stop)
    finally:
        llm.job_checkpoint.reset(stop_token)
        models.processing_parallel.reset(ptoken)
        models.processing_model.reset(token)


def _run_kind(job: dict, kind: str, target: str, profile: str, should_stop) -> None:
    from axiom.extraction.extract import extract_chapter
    from axiom.orchestration.pipeline import optimize_and_verify
    from axiom.transformations.transcribe import transcribe_section
    if kind == "import":
        result = _import_from_inbox(target)
    elif kind in ("extract", "reextract"):
        result = {"blocks": extract_chapter(job["doc_id"], target, force=(kind == "reextract"))}
    elif kind == "transcribe":
        with db.connect() as c:
            c.execute("UPDATE sections SET status='running' WHERE id=? AND status NOT IN ('done','partial')", (target,))
        result = transcribe_section(target, profile, should_stop)
    elif kind == "optimize":
        result = optimize_and_verify(target, profile, should_stop)
    elif kind == "reconcile":
        from axiom.verification.notation import reconcile
        doc, _, nums = target.partition("@")
        result = reconcile(doc, [int(x) for x in nums.split(",") if x], profile, should_stop)
    elif kind == "verify":
        from axiom.orchestration.pipeline import verify_and_finalize
        result = verify_and_finalize(target, profile, should_stop)
    elif kind == "diagram":
        result = _run_diagram(job)
    elif kind == "quality":
        result = _run_quality(job)
    elif kind == "cover":
        from axiom.ingestion.cover import generate
        result = generate(job["doc_id"], job["model"], job["id"])
    elif kind == "anki":
        from axiom.anki.study import generate
        result = generate(target, job["model"])
    else:
        raise ValueError(f"unknown job kind {kind}")
    _finish(job, result=result)


def _finish(job, *, result=None, error: str | None = None, interrupted: bool = False) -> None:
    with db.connect(immediate=True) as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
        if not row or row["run_token"] != job.get("run_token") or row["status"] not in ("running", "pending"):
            return  # this attempt lost ownership (e.g. its worker disconnected)
        if _apply_request(c, row):
            return
        if interrupted:
            c.execute("UPDATE jobs SET status='pending', attempts=MAX(0,attempts-1), worker=NULL,run_token=NULL,updated_at=? "
                      "WHERE id=?", (_now(), job["id"]))
            _release_section(c, row)
        elif error is not None:
            status = "failed" if row["attempts"] >= row["max_attempts"] else "pending"
            c.execute("UPDATE jobs SET status=?,error=?,worker=NULL,run_token=NULL,updated_at=? WHERE id=?",
                      (status, error, _now(), job["id"]))
            if status == "failed" and job["kind"] in _PIPELINE_STAGES:
                c.execute("UPDATE sections SET status='failed' WHERE id=?", (job["target_id"],))
        else:
            c.execute("UPDATE jobs SET status='done',progress=?,error=NULL,run_token=NULL,updated_at=? WHERE id=?",
                      (json.dumps(result), _now(), job["id"]))


def _worker(kinds: tuple[str, ...], idle_exit: bool, parallel_only: bool = False) -> None:
    while not _stop.is_set():
        job = _claim(kinds, parallel_only)
        if not job:
            if idle_exit and not db.q("SELECT 1 FROM jobs WHERE status IN ('pending','running') LIMIT 1"):
                return
            time.sleep(1.0)
            continue
        log.info("job %s %s %s (attempt %s)", job["id"], job["kind"], job["target_id"], job["attempts"] + 1)
        try:
            _run(job)
        except InterruptedError:
            _finish(job, interrupted=True)
        except Exception as e:
            log.error("job %s failed: %s", job["id"], e)
            _finish(job, error=f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}")


def start_workers(n_transcribe: int = 1, n_optimize: int | None = None, idle_exit: bool = False) -> list[threading.Thread]:
    """Transcription calls are internally parallel (local slots), so one transcribe worker suffices;
    an optimize worker runs alongside so that ready sections are optimized while later ones transcribe."""
    import os
    if n_optimize is None:  # chapter chains are independent, so >1 lets two chapters optimize concurrently
        n_optimize = int(os.environ.get("AXIOM_OPTIMIZE_WORKERS", "1"))
    recover()
    _stop.clear()
    threads = []
    from axiom.config import settings
    extra = max(0, settings.parallel_workers)  # only ever run jobs of "parallel" runs (premium model)
    for kinds in (("import", "extract", "reextract", "transcribe"), ("optimize", "verify", "extract", "reextract")):  # no reconcile: serial
        for _ in range(extra):
            t = threading.Thread(target=_worker, args=(kinds, idle_exit, True), daemon=True)
            t.start()
            threads.append(t)
    # diagrams (a subscription model, not the GPU) run on their own thread, in parallel with the pipeline
    for kinds, n in ((("import", "extract", "reextract", "transcribe", "quality"), n_transcribe),
                     (("optimize", "verify", "extract", "reextract", "reconcile", "quality"), n_optimize),
                     (("diagram", "cover"), 1), (("anki",), 1)):
        for _ in range(n):
            t = threading.Thread(target=_worker, args=(kinds, idle_exit), daemon=True)
            t.start()
            threads.append(t)
    return threads


def stop_workers() -> None:
    _stop.set()


def run_until_idle() -> None:
    for t in start_workers(idle_exit=True):
        t.join()


def _import_from_inbox(rel: str) -> dict:
    from axiom.ingestion.pdf import import_pdf
    from axiom.storage import files
    from pathlib import Path
    local = files.fetch(rel)
    name = rel.split("__", 1)[-1]
    staged = local.with_name(name)  # import under the original file name (it becomes the default title)
    local.replace(staged)
    try:
        doc_id = import_pdf(staged, Path(name).stem)
    finally:
        staged.unlink(missing_ok=True)
    files.remove(rel)
    return {"doc_id": doc_id}
