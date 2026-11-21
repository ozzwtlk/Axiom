"""Per-section pipeline stages glued together. Each stage is idempotent and checkpointed in the DB."""

from __future__ import annotations

import json
import logging

from axiom.exercises.engine import extract_exercises
from axiom.storage import db
from axiom.transformations.optimize import optimize_section
from axiom.verification.coverage import audit_section

log = logging.getLogger("axiom.pipeline")


class EmptySection(RuntimeError):
    pass


def optimize_and_verify(section_id: str, profile: str = "economy", should_stop=lambda: False) -> dict:
    from axiom.verification import quality
    if not db.q1("SELECT COUNT(*) n FROM blocks WHERE section_id=?", (section_id,))["n"]:
        sec = db.q1("SELECT number, chapter_id FROM sections WHERE id=?", (section_id,))
        if sec and sec["number"].endswith(".0") and db.q1(
                "SELECT 1 FROM sections WHERE chapter_id=? AND id!=? LIMIT 1", (sec["chapter_id"], section_id)):
            # a chapter's opening text that the outline promised but the page did not have: nothing to read
            with db.connect() as c:
                c.execute("UPDATE sections SET stage='verified', status='done' WHERE id=?", (section_id,))
                c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (f"empty:{section_id}", "1"))
            return {"skipped": "chapter opening without text"}
        # never report an empty section as readable: fail loudly so the UI shows it
        raise EmptySection(f"no source text was extracted for {section_id}; re-run extraction for its chapter")
    stats = optimize_section(section_id, profile, should_stop)
    stats["exercises"] = extract_exercises(section_id)
    q = quality.fix_units(section_id)
    stats["quality"] = {"fixed": q["fixed"], "needs": q["needs"]}
    report = audit_section(section_id)
    status = "done" if report["ok"] and not q["needs"] else "partial"
    with db.connect() as c:  # readable now; the faithfulness check (verify job) later moves it to 'verified'
        c.execute("UPDATE sections SET stage='optimized', status=? WHERE id=?", (status, section_id))
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                  (f"audit:{section_id}", json.dumps({"optimize": stats, "coverage": report})))
    if not report["ok"]:
        log.warning("coverage audit failed for %s: %s", section_id, report)
    from axiom.orchestration import jobs
    try:
        stats["diagram_jobs"] = len(jobs.enqueue_diagrams(section_id, profile))
    except Exception as e:  # optional extra; never fail the section over it
        log.warning("could not queue diagrams for %s: %s", section_id, e)
    return {"optimize": stats, "coverage": {k: report[k] for k in ("blocks", "units", "by_status", "ok", "flags")}}


def verify_and_finalize(section_id: str, profile: str = "economy", should_stop=lambda: False) -> dict:
    """Independent faithfulness check, reader digest, then a final coverage audit (the checker may revert units)."""
    from axiom.transformations.digest import digest_section
    from axiom.verification.faithfulness import verify_section
    sec = db.q1("SELECT status, is_exercises FROM sections WHERE id=?", (section_id,))
    if not sec or db.q1("SELECT 1 FROM meta WHERE key=?", (f"empty:{section_id}",)):
        return {"skipped": "nothing to check"}
    stats = verify_section(section_id, profile, should_stop) if not sec["is_exercises"] else {"units_checked": 0}
    digest_section(section_id, profile)
    from axiom.verification.xrefs import xrefs
    doc_id = db.q1("SELECT doc_id FROM sections WHERE id=?", (section_id,))["doc_id"]
    stats["unresolved_refs"] = [f"{x['kind']} {x['ref']}" for x in xrefs(doc_id)["unresolved"]
                                if x["section_id"] == section_id]
    if stats["unresolved_refs"]:
        log.info("%s: references without a target: %s", section_id, ", ".join(stats["unresolved_refs"]))
    from axiom.verification import quality
    q = quality.fix_units(section_id, stats)  # the checker may have reverted units: check them again
    stats["quality"] = {"fixed": q["fixed"], "needs": q["needs"]}
    report = audit_section(section_id)
    status = "done" if report["ok"] and not q["needs"] else "partial"
    with db.connect() as c:
        c.execute("UPDATE sections SET stage='verified', status=? WHERE id=?", (status, section_id))
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                  (f"faithfulness:{section_id}", json.dumps(stats)))
    return {"faithfulness": stats, "coverage_ok": report["ok"]}
