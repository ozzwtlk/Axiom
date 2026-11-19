"""Coverage audit: every source block of a section must map to at least one unit with a known status."""

from __future__ import annotations

import json

from axiom.storage import db


def audit_section(section_id: str) -> dict:
    blocks = db.q("SELECT id, block_type FROM blocks WHERE section_id=?", (section_id,))
    cov = {r["block_id"]: r for r in db.q("SELECT * FROM coverage WHERE section_id=?", (section_id,))}
    units = [json.loads(r["data"]) for r in db.q("SELECT data FROM units WHERE section_id=? ORDER BY ord", (section_id,))]
    missing = [b["id"] for b in blocks if b["id"] not in cov]
    by_status: dict[str, int] = {}
    for r in cov.values():
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    omitted_without_reason = [r["block_id"] for r in cov.values() if r["status"] == "omitted" and not r["note"]]
    eq_blocks = {b["id"] for b in blocks if b["block_type"] == "equation"}
    eq_units_verbatim = all(
        (u.get("latex") is not None or u.get("text_md")) for u in units if u["kind"] == "equation")
    eq_in_units = {b for u in units if u["kind"] == "equation" for b in u["block_ids"]}
    flags = sum(len(u.get("flags", [])) for u in units)
    report = {
        "blocks": len(blocks), "units": len(units), "missing": missing, "by_status": by_status,
        "omitted_without_reason": omitted_without_reason,
        "equations": len(eq_blocks), "equations_rendered": len(eq_blocks & eq_in_units),
        "equations_all_have_content": eq_units_verbatim, "flags": flags,
        "ok": not missing and not omitted_without_reason and eq_blocks <= eq_in_units,
    }
    return report
