"""Quality control (verification/quality.py): deterministic fixes, open issues, re-running what needs a model."""

from __future__ import annotations

import json

import pymupdf
import pytest

from axiom.storage import db
from axiom.verification import latex, quality

needs_katex = pytest.mark.skipif(not latex.available(), reason="KaTeX helper not installed")


def _doc(blocks, units=None, status="done", stage="verified", number="1.1", pdf=None, tmp=None):
    """blocks: (type, text[, page, (x0,y0,x1,y1)]). Units are stored with ids d-s1.1-u000..."""
    with db.connect() as c:
        c.execute("INSERT OR IGNORE INTO documents VALUES ('d','T','f','h',10,'now','{}')")
        c.execute("INSERT OR IGNORE INTO chapters(id,doc_id,number,title,page_start,page_end) VALUES ('d-c1','d',1,'C',1,2)")
        sid = f"d-s{number}"
        c.execute("INSERT INTO sections(id,doc_id,chapter_id,ord,number,title,page_start,page_end,is_exercises,stage,status)"
                  " VALUES (?,?,?,?,?,?,1,2,0,?,?)", (sid, "d", "d-c1", 0 if number.endswith(".0") else 1, number, "S",
                                                     stage, status))
        for i, b in enumerate(blocks):
            t, md = b[0], b[1]
            page = b[2] if len(b) > 2 else 1
            x0, y0, x1, y1 = b[3] if len(b) > 3 else (0, i * 20, 100, i * 20 + 15)
            c.execute("INSERT INTO blocks(id,doc_id,section_id,ord,page,x0,y0,x1,y1,block_type,raw_text,content_hash,"
                      "transcribed_md,transcription_method,confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (f"b{i}", "d", sid, i, page, x0, y0, x1, y1, t, md, "h", md if t != "figure" else "",
                       "vision_ocr", 0.9))
        for i, u in enumerate(units or []):
            uid = f"{sid}-u{i:03d}"
            c.execute("INSERT INTO units(id, section_id, ord, data) VALUES (?,?,?,?)", (uid, sid, i, json.dumps(dict(u, id=uid))))
            for bid in u["block_ids"]:
                c.execute("INSERT OR REPLACE INTO coverage VALUES (?,?,?,?,NULL)", (bid, sid, u["coverage"], json.dumps([uid])))
    return sid


def _u(text, blocks, kind="prose", ai=True, **kw):
    return dict({"kind": kind, "role": "core", "min_level": 1, "title": None, "text_md": text, "fast_md": None,
                 "deep_md": None, "annotation_md": None, "block_ids": blocks, "coverage": "rewritten" if ai else "preserved",
                 "ai_generated": ai, "flags": [], "notes": []}, **kw)


def _units(sid):
    return [json.loads(r["data"]) for r in db.q("SELECT data FROM units WHERE section_id=? ORDER BY ord", (sid,))]


def test_page_numbers_and_lines_caught_twice_are_removed_from_the_blocks(tmp_db):
    lead = "In section 6.2 we will compute the action and compare the result to the entropy"
    sid = _doc([("paragraph", "We now turn to the thermodynamics of black holes in detail here."),
                ("equation", "$$- \\ 4 \\ -$$"),
                ("equation", f"$$S = -(1-\\beta\\partial_\\beta) I$$ {lead}"),
                ("paragraph", f"{lead} formula. In section 6.3 we do it another way."),
                ("paragraph", "The same sentence appears twice because two crops overlap here"),
                ("paragraph", "The same sentence appears twice because two crops overlap here, and then it goes on.")])
    r = quality.clean_blocks(sid)
    assert r["fixed"] == {"page_number": 1, "repeated_text": 2}
    b = {x["id"]: x for x in db.q("SELECT * FROM blocks WHERE section_id=?", (sid,))}
    assert b["b1"]["block_type"] == "artifact" and "Page number" in b["b1"]["flags"]
    assert b["b2"]["transcribed_md"] == "$$S = -(1-\\beta\\partial_\\beta) I$$"  # the next line's copy is gone
    assert b["b4"]["block_type"] == "artifact" and b["b5"]["block_type"] == "paragraph"


@needs_katex
def test_unit_fixes_repair_latex_drop_cut_off_text_and_account_for_every_block(tmp_db):
    sid = _doc([("paragraph", "Intro text about the black hole."), ("equation", "$$- 5 -$$"),
                ("equation", "$$x' = x + \\upomega_a t_a x \\eqno (1.3)$$"),
                ("paragraph", "Second passage of the source text, it is complete."),
                ("paragraph", "Third passage.")],
               [_u("Intro **text**.", ["b0"], deep_md="A complete first sentence that is long enough. The model then stopped in the "),
                _u("", ["b1"], kind="equation", ai=False, latex="- 5 -"),
                _u("", ["b2"], kind="equation", ai=False, latex="x' = x + \\upomega_a t_a x \\eqno (1.3)"),
                _u("Rewritten second passage", ["b3"], fast_md="Uses g_{\\mu\\nu} here.",
                   annotation_md="Broken $\\frac{1}{$ math."),
                _u("Third passage.", ["b4"], ai=False, note="Optimization failed validation; showing faithful transcription")])
    quality.clean_blocks(sid)
    rep = quality.fix_units(sid)
    us = _units(sid)
    assert [u["id"] for u in us] == ["d-s1.1-u000", "d-s1.1-u002", "d-s1.1-u003", "d-s1.1-u004"]  # page number gone
    assert us[0]["deep_md"] == "A complete first sentence that is long enough."
    assert us[1]["latex"] == "x' = x + \\omega_a t_a x"
    blk = db.q1("SELECT transcribed_md FROM blocks WHERE id='b2'")["transcribed_md"]
    assert "\\omega_a" in blk and "eqno" not in blk  # the transcription itself is repaired too
    # cut off mid-sentence → the source; LaTeX outside math wrapped; unrenderable optional note dropped
    assert us[2]["text_md"] == "Second passage of the source text, it is complete." and not us[2]["ai_generated"]
    cov = {r["block_id"]: r for r in db.q("SELECT * FROM coverage WHERE section_id=?", (sid,))}
    assert cov["b1"]["status"] == "omitted" and "Page number" in cov["b1"]["note"]
    from axiom.verification.coverage import audit_section
    assert audit_section(sid)["ok"]
    assert rep["needs"] == ["optimize"] and rep["issues"][0]["kind"] == "not_optimized"
    assert quality.status_for(rep) == "partial"


@needs_katex
def test_stray_latex_is_wrapped_only_when_it_renders():
    md = "Massive particles: ε = −g_{μν}U^μU^ν = +1, with h_k^(1), see https://x.org/a_b."
    ok = {t for t, e in zip(quality.stray_candidates(md), latex.check([(t, False) for t in quality.stray_candidates(md)]))
          if not e}
    out = quality.wrap_stray(md, ok)
    assert "$−g_{μν}U^μU^ν$" in out and "$h_k^(1)$," in out and "https://x.org/a_b" in out
    assert quality.normalize_md("$$\\mathrm { d e e p \\ r e a s o n \\ f o r \\ t h i s ? }$$") == "deep reason for this?"
    rep = "The same long sentence is written twice by the model."
    assert quality._dedupe(f"Keep $a$ constant while the radius shrinks. Keep $b$ constant while the radius shrinks. "
                           f"{rep} {rep}") == \
        f"Keep $a$ constant while the radius shrinks. Keep $b$ constant while the radius shrinks. {rep}"
    assert quality._collapse_loops("x \\qquad \\qquad \\qquad \\qquad \\qquad y") != "x"


@needs_katex
def test_a_unit_repeating_its_neighbour_is_merged(tmp_db):
    text = "For the sake of illustration let us work out the terms of the above sum explicitly for three cases"
    sid = _doc([("paragraph", text), ("paragraph", text + " and more.")],
               [_u(text, ["b0"], ai=False), _u(text + " and more, with the rest of the passage.", ["b1"])])
    rep = quality.fix_units(sid)
    us = _units(sid)
    assert len(us) == 1 and us[0]["block_ids"] == ["b0", "b1"] and rep["fixed"]["repeated_text"] == 1


def test_text_cut_out_as_an_image_is_read_as_text(tmp_db, tmp_path):
    from axiom.extraction import extract
    pdf = pymupdf.open()
    page = pdf.new_page(width=500, height=700)
    page.insert_text((60, 100), "Figure 6.9, such that afterward you follow a geodesic trajectory back", fontsize=10)
    page.insert_text((60, 112), "outside the stationary limit surface into the external universe. Since", fontsize=10)
    page.draw_circle((250, 400), 80)
    page.insert_text((240, 400), "r", fontsize=10)
    path = tmp_path / "x.pdf"
    pdf.save(path)
    sid = _doc([("figure", "", 1, (55, 88, 420, 116)), ("figure", "", 1, (60, 300, 440, 500))])
    rep = quality.clean_blocks(sid, pymupdf.open(path))
    b = {x["id"]: x for x in db.q("SELECT * FROM blocks WHERE section_id=?", (sid,))}
    assert rep["retranscribe"] == ["b0"] and b["b0"]["block_type"] == "paragraph"
    assert "stationary limit surface" in b["b0"]["raw_text"] and b["b0"]["transcription_method"] == "none"
    assert b["b1"]["block_type"] == "figure"  # a real picture with one label stays a picture
    assert extract  # (open_pdf is used when no PDF is passed)


def test_reprocessing_a_partial_section_really_runs_again(tmp_db):
    from axiom.orchestration import jobs
    sid = _doc([("paragraph", "text"), ("equation", "$$\\frac{1}{$$")], [_u("text", ["b0"])], status="partial")
    with db.connect() as c:
        for k in ("extract", "transcribe", "optimize", "verify"):
            c.execute("INSERT INTO jobs(id,doc_id,kind,target_id,status,depends_on,profile,created_at,updated_at)"
                      " VALUES (?,?,?,?,?,?,?,?,?)", (k, "d", k, "d-c1" if k == "extract" else sid, "done", "[]",
                                                      "economy", "t", "t"))
        c.execute("INSERT INTO meta VALUES (?,?)", (f"quality:{sid}", json.dumps(
            {"issues": [{"kind": "bad_transcription", "needs": "transcribe", "blocks": ["b1"]}], "needs": ["transcribe"]})))
    jobs.enqueue_document("d", [1])
    pending = {r["kind"] for r in db.q("SELECT kind FROM jobs WHERE status='pending' AND target_id=?", (sid,))}
    assert pending == {"transcribe", "optimize", "verify"}
    assert db.q1("SELECT transcription_method m FROM blocks WHERE id='b1'")["m"] == "none"  # read again
    assert db.q1("SELECT transcription_method m FROM blocks WHERE id='b0'")["m"] == "vision_ocr"  # kept (cached)
    assert db.q1("SELECT status FROM sections WHERE id=?", (sid,))["status"] == "partial"  # stays readable


def test_empty_chapter_opening_is_skipped_not_failed(tmp_db):
    from axiom.orchestration.pipeline import optimize_and_verify, verify_and_finalize
    _doc([("paragraph", "x")], number="1.1", status="pending", stage="none")
    sid = _doc([], number="1.0", status="pending", stage="none")
    assert optimize_and_verify(sid)["skipped"]
    assert verify_and_finalize(sid)["skipped"]
    assert db.q1("SELECT status FROM sections WHERE id=?", (sid,))["status"] == "done"


@needs_katex
def test_quality_job_checks_and_requeues_only_what_needs_a_model(tmp_db):
    from axiom.orchestration import jobs
    sid = _doc([("paragraph", "Source passage, complete."), ("paragraph", "- 12 -")],
               [_u("Source passage, complete.", ["b0"], ai=False, note="Optimization failed validation; x"),
                _u("12", ["b1"], ai=False)])
    jobs.enqueue_quality("d", rerun=True)
    jobs.run_until_idle()
    rep = quality.document_report("d")
    assert rep["fixed"]["page_number"] == 1 and rep["open"] == {"not_optimized": 1}
    kinds = {r["kind"] for r in db.q("SELECT kind FROM jobs WHERE target_id=? AND status IN ('pending','failed','done')",
                                     (sid,))}
    assert {"transcribe", "optimize"} <= kinds  # queued again (they fail here: no model in tests)
