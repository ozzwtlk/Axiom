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
_cancel_requested: set[str] = set()


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
        for n in sorted(chapter_numbers):
            if parallel:
                prev_opt = None
            ch = c.execute("SELECT * FROM chapters WHERE doc_id=? AND number=?", (doc_id, n)).fetchone()
            if not ch:
                continue
            ex = _extract_job(c, doc_id, ch, profile, model, parallel)
            ids.append(ex)
            for s in c.execute("SELECT * FROM sections WHERE chapter_id=? ORDER BY ord", (ch["id"],)).fetchall():
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


def _extract_job(c, doc_id: str, ch, profile: str, model: str | None, parallel: bool) -> str:
    """The job that makes the chapter's blocks. A chapter that already has blocks but with sections that got none
    (their heading was not recognised once, so the text sits in the previous section) is extracted again: a
    "reextract" job replaces the stale queue entries of the chapter, so nothing runs against half-old blocks."""
    secs = [s["id"] for s in c.execute("SELECT id FROM sections WHERE chapter_id=?", (ch["id"],))]
    marks = ",".join("?" * len(secs)) or "NULL"
    have = {r["section_id"] for r in c.execute(f"SELECT DISTINCT section_id FROM blocks WHERE section_id IN ({marks})", secs)}
    if not have or len(have) == len(secs):
        return _add(c, doc_id, "extract", ch["id"], [], profile, model, parallel)
    c.execute(f"DELETE FROM jobs WHERE status IN ('pending','running','failed','cancelled') AND target_id IN ({marks})"
              " AND kind IN ('transcribe','optimize','verify')", secs)
    c.execute("DELETE FROM jobs WHERE kind='reextract' AND target_id=?", (ch["id"],))
    log.info("chapter %s has sections without blocks: re-extracting", ch["number"])
    return _add(c, doc_id, "reextract", ch["id"], [], profile, model, parallel)


def cancel(job_id: str) -> None:
    _cancel_requested.add(job_id)
    with db.connect() as c:
        c.execute("UPDATE jobs SET status='cancelled', updated_at=? WHERE id=? AND status='pending'", (_now(), job_id))


def cancel_document(doc_id: str) -> int:
    rows = db.q("SELECT id FROM jobs WHERE doc_id=? AND status IN ('pending','running')", (doc_id,))
    for r in rows:
        cancel(r["id"])
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
    _cancel_requested.clear()
    return {"jobs_removed": n, "sections_reset": s}


# name of this machine when it runs as a worker for a server (None: all-in-one, this process owns the queue)
WORKER_NAME: str | None = None


def enqueue_import(rel: str) -> str:
    """A PDF uploaded to the server's inbox, imported by the next computing machine."""
    with db.connect() as c:
        return _add(c, "inbox", "import", rel, [], "economy")


def recover() -> int:
    with db.connect() as c:
        # an interruption is not a failure: give the attempt back. A worker only reclaims its own jobs.
        own = " AND worker=?" if WORKER_NAME else ""
        args = (_now(), WORKER_NAME) if WORKER_NAME else (_now(),)
        n = c.execute("UPDATE jobs SET status='pending', attempts=MAX(0, attempts-1), updated_at=? WHERE status='running'"
                      + own, args).rowcount
        if not WORKER_NAME:
            c.execute("UPDATE sections SET status='pending' WHERE status='running'")
    return n


_GROUP_OF_KIND = {"transcribe": "transcribe", "optimize": "optimize", "verify": "faithfulness",
                  "reconcile": "optimize"}


def can_run(job, selected: str = "auto") -> bool:
    """May this machine take the job? Only the chosen computing machine (or anyone under "auto"), and only if it
    has the model the job needs — so a Qwen run waits for the PC instead of failing on a laptop."""
    if WORKER_NAME and selected not in ("auto", WORKER_NAME):
        return False
    group = _GROUP_OF_KIND.get(job["kind"])
    if not group:
        return True  # import / extract need no model
    from axiom.orchestration import models
    need = job["model"] if "model" in job.keys() and job["model"] else models.resolve(group).model
    return models.available("local" if need in ("local_text", "local_vlm") else need)


_claim_lock = threading.Lock()


def _claim(kinds: tuple[str, ...], parallel_only: bool = False) -> dict | None:
    with _claim_lock, db.connect() as c:
        done = {r["id"] for r in c.execute("SELECT id FROM jobs WHERE status='done'")}
        failed = {r["id"] for r in c.execute("SELECT id FROM jobs WHERE status IN ('failed','cancelled')")}
        # "~<id>" = ordering-only dependency: wait until that job has finished, but do not inherit its failure
        # (the optimize chain: a failed earlier section must not fail every later section and chapter)
        sel_row = c.execute("SELECT value FROM meta WHERE key='compute.selected'").fetchone()
        selected = (sel_row["value"] if sel_row else None) or "auto"
        for j in c.execute(f"SELECT * FROM jobs WHERE status='pending' AND kind IN ({','.join('?' * len(kinds))})"
                           " ORDER BY created_at, rowid", kinds).fetchall():
            if parallel_only and not j["parallel"]:
                continue
            all_deps = json.loads(j["depends_on"])
            deps = [d for d in all_deps if not d.startswith("~")]
            soft = [d[1:] for d in all_deps if d.startswith("~")]
            if not all(d in done or d in failed for d in soft):
                continue
            if any(d in failed for d in deps):
                c.execute("UPDATE jobs SET status='failed', error='upstream dependency failed', updated_at=? WHERE id=?",
                          (_now(), j["id"]))
                c.execute("UPDATE sections SET status='failed' WHERE id=? AND status NOT IN ('done','partial')",
                          (j["target_id"],))
                continue
            if all(d in done for d in deps) and can_run(j, selected):
                c.execute("UPDATE jobs SET status='running', attempts=attempts+1, worker=?, updated_at=? WHERE id=?",
                          (WORKER_NAME, _now(), j["id"]))
                return dict(j)
    return None


def _run(job: dict) -> None:
    from axiom.extraction.extract import extract_chapter
    from axiom.orchestration.pipeline import optimize_and_verify
    from axiom.transformations.transcribe import transcribe_section

    def should_stop():
        return _stop.is_set() or job["id"] in _cancel_requested

    kind, target, profile = job["kind"], job["target_id"], job["profile"]
    from axiom.orchestration import models
    token = models.processing_model.set(job.get("model"))  # the model chosen for this processing run
    ptoken = models.processing_parallel.set(bool(job.get("parallel")))
    try:
        _run_kind(job, kind, target, profile, should_stop)
    finally:
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
    else:
        raise ValueError(f"unknown job kind {kind}")
    with db.connect() as c:
        c.execute("UPDATE jobs SET status='done', progress=?, error=NULL, updated_at=? WHERE id=?",
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
            st = "cancelled" if job["id"] in _cancel_requested else "pending"
            with db.connect() as c:
                c.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (st, _now(), job["id"]))
        except Exception as e:
            log.error("job %s failed: %s", job["id"], e)
            with db.connect() as c:
                row = c.execute("SELECT attempts, max_attempts FROM jobs WHERE id=?", (job["id"],)).fetchone()
                st = "failed" if row["attempts"] >= row["max_attempts"] else "pending"
                c.execute("UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?",
                          (st, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}", _now(), job["id"]))
                if st == "failed" and job["kind"] != "extract":
                    c.execute("UPDATE sections SET status='failed' WHERE id=?", (job["target_id"],))


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
    for kinds, n in ((("import", "extract", "reextract", "transcribe"), n_transcribe), (("optimize", "verify", "extract", "reextract", "reconcile"), n_optimize)):
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
