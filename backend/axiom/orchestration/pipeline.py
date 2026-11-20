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
    if not db.q1("SELECT COUNT(*) n FROM blocks WHERE section_id=?", (section_id,))["n"]:
        # never report an empty section as readable: fail loudly so the UI shows it
        raise EmptySection(f"no source text was extracted for {section_id}; re-run extraction for its chapter")
    stats = optimize_section(section_id, profile, should_stop)
    stats["exercises"] = extract_exercises(section_id)
    report = audit_section(section_id)
    status = "done" if report["ok"] and not stats["fallback_windows"] else "partial"
    with db.connect() as c:  # readable now; the faithfulness check (verify job) later moves it to 'verified'
        c.execute("UPDATE sections SET stage='optimized', status=? WHERE id=?", (status, section_id))
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                  (f"audit:{section_id}", json.dumps({"optimize": stats, "coverage": report})))
    if not report["ok"]:
        log.warning("coverage audit failed for %s: %s", section_id, report)
    return {"optimize": stats, "coverage": {k: report[k] for k in ("blocks", "units", "by_status", "ok", "flags")}}


def verify_and_finalize(section_id: str, profile: str = "economy", should_stop=lambda: False) -> dict:
    """Independent faithfulness check, reader digest, then a final coverage audit (the checker may revert units)."""
    from axiom.transformations.digest import digest_section
    from axiom.verification.faithfulness import verify_section
    sec = db.q1("SELECT status, is_exercises FROM sections WHERE id=?", (section_id,))
    stats = verify_section(section_id, profile, should_stop) if not sec["is_exercises"] else {"units_checked": 0}
    digest_section(section_id, profile)
    report = audit_section(section_id)
    status = sec["status"] if report["ok"] and not stats.get("failed_batches") else "partial"
    with db.connect() as c:
        c.execute("UPDATE sections SET stage='verified', status=? WHERE id=?", (status, section_id))
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                  (f"faithfulness:{section_id}", json.dumps(stats)))
    return {"faithfulness": stats, "coverage_ok": report["ok"]}
