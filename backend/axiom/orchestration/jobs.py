"""Persistent job queue in SQLite.

Task contract (one row per job): kind, target_id, depends_on (job ids), profile, attempts/max_attempts,
status ∈ pending | running | done | failed | cancelled, progress JSON, error.

Job graph per chapter:  extract(chapter) → transcribe(section_i) → optimize(section_i) → verify(section_i)
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


def _add(c, doc_id: str, kind: str, target: str, deps: list[str], profile: str) -> str:
    existing = c.execute("SELECT id, status FROM jobs WHERE kind=? AND target_id=? AND status IN ('pending','running','done')",
                         (kind, target)).fetchone()
    if existing:
        return existing["id"]
    jid = uuid.uuid4().hex[:12]
    c.execute("INSERT INTO jobs(id,doc_id,kind,target_id,status,depends_on,profile,created_at,updated_at)"
              " VALUES (?,?,?,?,?,?,?,?,?)", (jid, doc_id, kind, target, "pending", json.dumps(deps), profile, _now(), _now()))
    return jid


def enqueue_document(doc_id: str, chapter_numbers: list[int], profile: str = "economy") -> list[str]:
    ids: list[str] = []
    # optimize jobs form ONE chain across chapters: a section's context (summaries, notation) comes from all earlier
    # sections of the book, so it must not depend on how far a parallel chapter has got (stable cache keys)
    prev_opt = None
    with db.connect() as c:
        for n in sorted(chapter_numbers):
            ch = c.execute("SELECT * FROM chapters WHERE doc_id=? AND number=?", (doc_id, n)).fetchone()
            if not ch:
                continue
            ex = _add(c, doc_id, "extract", ch["id"], [], profile)
            ids.append(ex)
            for s in c.execute("SELECT * FROM sections WHERE chapter_id=? ORDER BY ord", (ch["id"],)).fetchall():
                tr = _add(c, doc_id, "transcribe", s["id"], [ex], profile)
                op = _add(c, doc_id, "optimize", s["id"], [tr] + ([prev_opt] if prev_opt else []), profile)
                vf = _add(c, doc_id, "verify", s["id"], [op], profile)
                ids += [tr, op, vf]
                prev_opt = op
                c.execute("UPDATE sections SET status='pending' WHERE id=? AND status NOT IN ('done','partial')", (s["id"],))
    return ids


def cancel(job_id: str) -> None:
    _cancel_requested.add(job_id)
    with db.connect() as c:
        c.execute("UPDATE jobs SET status='cancelled', updated_at=? WHERE id=? AND status='pending'", (_now(), job_id))


def cancel_document(doc_id: str) -> int:
    rows = db.q("SELECT id FROM jobs WHERE doc_id=? AND status IN ('pending','running')", (doc_id,))
    for r in rows:
        cancel(r["id"])
    return len(rows)


def recover() -> int:
    with db.connect() as c:
        # an interruption is not a failure: give the attempt back
        n = c.execute("UPDATE jobs SET status='pending', attempts=MAX(0, attempts-1), updated_at=? WHERE status='running'",
                      (_now(),)).rowcount
        c.execute("UPDATE sections SET status='pending' WHERE status='running'")
    return n


_claim_lock = threading.Lock()


def _claim(kinds: tuple[str, ...]) -> dict | None:
    with _claim_lock, db.connect() as c:
        done = {r["id"] for r in c.execute("SELECT id FROM jobs WHERE status='done'")}
        failed = {r["id"] for r in c.execute("SELECT id FROM jobs WHERE status IN ('failed','cancelled')")}
        for j in c.execute(f"SELECT * FROM jobs WHERE status='pending' AND kind IN ({','.join('?' * len(kinds))})"
                           " ORDER BY created_at, rowid", kinds).fetchall():
            deps = json.loads(j["depends_on"])
            if any(d in failed for d in deps):
                c.execute("UPDATE jobs SET status='failed', error='upstream dependency failed', updated_at=? WHERE id=?",
                          (_now(), j["id"]))
                c.execute("UPDATE sections SET status='failed' WHERE id=? AND status NOT IN ('done','partial')",
                          (j["target_id"],))
                continue
            if all(d in done for d in deps):
                c.execute("UPDATE jobs SET status='running', attempts=attempts+1, updated_at=? WHERE id=?", (_now(), j["id"]))
                return dict(j)
    return None


def _run(job: dict) -> None:
    from axiom.extraction.extract import extract_chapter
    from axiom.orchestration.pipeline import optimize_and_verify
    from axiom.transformations.transcribe import transcribe_section

    def should_stop():
        return _stop.is_set() or job["id"] in _cancel_requested

    kind, target, profile = job["kind"], job["target_id"], job["profile"]
    if kind == "extract":
        result = {"blocks": extract_chapter(job["doc_id"], target)}
    elif kind == "transcribe":
        with db.connect() as c:
            c.execute("UPDATE sections SET status='running' WHERE id=? AND status NOT IN ('done','partial')", (target,))
        result = transcribe_section(target, profile, should_stop)
    elif kind == "optimize":
        result = optimize_and_verify(target, profile, should_stop)
    elif kind == "verify":
        from axiom.orchestration.pipeline import verify_and_finalize
        result = verify_and_finalize(target, profile, should_stop)
    else:
        raise ValueError(f"unknown job kind {kind}")
    with db.connect() as c:
        c.execute("UPDATE jobs SET status='done', progress=?, error=NULL, updated_at=? WHERE id=?",
                  (json.dumps(result), _now(), job["id"]))


def _worker(kinds: tuple[str, ...], idle_exit: bool) -> None:
    while not _stop.is_set():
        job = _claim(kinds)
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
    for kinds, n in ((("extract", "transcribe"), n_transcribe), (("optimize", "verify", "extract"), n_optimize)):
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
