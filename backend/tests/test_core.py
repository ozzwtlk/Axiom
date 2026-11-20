"""Core acceptance tests: ingestion, layout, integrity rules, caching, jobs/resume, robustness."""

import json

import pymupdf
import pytest

from axiom.extraction.layout import Line, Span, group_rows, lines_from_pymupdf, segment_page
from axiom.orchestration.llm import ValidationFailed, parse_json
from axiom.storage import db


# ---------------------------------------------------------------- JSON / LaTeX escape integrity
def test_parse_json_repairs_latex_backslashes():
    raw = r'{"a": "$g_{\text{\mu\nu}} = \frac{1}{2}\nabla \beta \rho \\alpha$", "b": "x\ny"}'
    d = parse_json(raw)
    assert d["a"] == r"$g_{\text{\mu\nu}} = \frac{1}{2}\nabla \beta \rho \alpha$"
    assert d["b"] == "x\ny"  # real newlines survive


def test_parse_json_repairs_commands_outside_the_known_list():
    raw = r'{"a": "$x \rightarrow y,\ \tanh\phi,\ \textstyle\bigl(\frak g\bigr)$", "b": "a\nThe end\n\nwhere", "c": "if (x) {\n\tconst y = 1;\n}"}'
    d = parse_json(raw)
    assert d["c"] == "if (x) {\n\tconst y = 1;\n}"  # tab-indented generated code is untouched
    assert d["a"] == r"$x \rightarrow y,\ \tanh\phi,\ \textstyle\bigl(\frak g\bigr)$"
    assert d["b"] == "a\nThe end\n\nwhere"  # \n before a word stays a newline


def test_parse_json_rejects_control_chars():
    with pytest.raises(ValidationFailed):
        parse_json('{"a": "bad \\u000crac"}')


# ---------------------------------------------------------------- layout (synthetic)
def _line(x0, y0, x1, y1, text, size=9.6, font="Times-Roman"):
    return Line(x0, y0, x1, y1, [Span(text, size, font, x0, x1)])


def test_segment_tagged_equation_with_empty_ocr_band():
    """A display equation whose OCR text is missing must still be cut as a band, keeping its tag."""
    lines = [_line(180, 51, 505, 63, "12 Chapter 1 Something")]
    y = 80
    for i in range(6):
        lines.append(_line(180 if i else 192, y, 505, y + 12, f"body text line number {i} with words"))
        y += 12
    lines.append(_line(483, y + 10, 505, y + 22, "(1.3)", 9.0))
    y += 34
    for i in range(3):
        lines.append(_line(180, y, 505, y + 12, f"continuation text line {i} here"))
        y += 12
    blocks = segment_page(group_rows(lines), 5, 557, 670)
    kinds = [b.kind for b in blocks]
    assert kinds == ["paragraph", "equation", "paragraph"]
    eq = blocks[1]
    assert eq.eq_number == "1.3"
    assert eq.bbox[1] < eq.bbox[3]
    assert "Chapter" not in blocks[0].text  # running head removed


def test_dehyphenation_and_paragraph_split():
    lines = []
    y = 80
    texts = ["First paragraph starts here and goes on with dimen-", "sions of space and time continuing on.",
             "and it ends here."]
    for i, t in enumerate(texts):
        lines.append(_line(192 if i == 0 else 180, y, 505 if i < 2 else 300, y + 12, t)); y += 12
    lines.append(_line(192, y, 505, y + 12, "Second paragraph is indented and starts anew with text.")); y += 12
    lines.append(_line(180, y, 505, y + 12, "and continues on this line with more words in it.")); y += 12
    for i in range(3):
        lines.append(_line(180, y, 505, y + 12, f"more filler body text line {i} for geometry")); y += 12
    blocks = segment_page(group_rows(lines), 1, 557, 670)
    assert len([b for b in blocks if b.kind == "paragraph"]) == 2
    assert "dimensions of space" in blocks[0].text


# ---------------------------------------------------------------- layout (real scanned pages)
def test_carroll_page16_equations(carroll):
    d = pymupdf.open(carroll)
    p = d[15]
    blocks = segment_page(group_rows(lines_from_pymupdf(p.get_text("dict"))), 16, p.rect.width, p.rect.height)
    eqs = [b.eq_number for b in blocks if b.kind == "equation"]
    assert eqs == ["1.1", "1.2", "1.3", "1.4"]
    assert blocks[0].kind == "heading" and blocks[0].heading_number == "1.1"


def test_carroll_exercises_page(carroll):
    d = pymupdf.open(carroll)
    p = d[60]
    blocks = segment_page(group_rows(lines_from_pymupdf(p.get_text("dict"))), 61, p.rect.width, p.rect.height,
                          in_exercises=True)
    nums = [b.exercise_number for b in blocks if b.kind == "exercise"]
    assert nums == ["2", "3", "4", "5", "6", "7"]


def test_import_outline_and_immutability(tmp_db, carroll):
    from axiom.ingestion.pdf import import_pdf, sha256_file
    doc_id = import_pdf(carroll)
    assert import_pdf(carroll) == doc_id  # idempotent
    stored = tmp_db.doc_dir(doc_id) / "original.pdf"
    assert sha256_file(stored) == sha256_file(carroll)
    ch = db.q("SELECT number, title FROM chapters WHERE doc_id=? ORDER BY number", (doc_id,))
    assert [c["number"] for c in ch][:2] == [1, 2]
    secs = db.q("SELECT number FROM sections WHERE chapter_id=? ORDER BY ord", (f"{doc_id}-c1",))
    assert [s["number"] for s in secs] == [f"1.{i}" for i in range(1, 12)]
    ex = db.q1("SELECT is_exercises FROM sections WHERE id=?", (f"{doc_id}-s1.11",))
    assert ex["is_exercises"] == 1


def test_extract_chapter_assigns_every_section(tmp_db, carroll):
    from axiom.extraction.extract import extract_chapter
    from axiom.ingestion.pdf import import_pdf
    doc_id = import_pdf(carroll)
    extract_chapter(doc_id, f"{doc_id}-c1")
    rows = db.q("SELECT s.number, COUNT(b.id) n FROM sections s LEFT JOIN blocks b ON b.section_id=s.id"
                " WHERE s.chapter_id=? GROUP BY s.id", (f"{doc_id}-c1",))
    assert all(r["n"] > 0 for r in rows), [dict(r) for r in rows]
    tags = {r["eq_number"] for r in db.q("SELECT eq_number FROM blocks WHERE doc_id=? AND block_type='equation'", (doc_id,))}
    assert {"1.1", "1.29", "1.30", "1.31"} <= tags


def test_stale_stage_flag_does_not_skip_extraction_and_empty_sections_fail(tmp_db, carroll):
    """Regression: chapters 3-9 had stage='transcribed' but no blocks → extraction was skipped and every later
    stage 'succeeded' on empty sections, which the reader then showed as ready (with only a quiz button)."""
    from axiom.extraction.extract import extract_chapter
    from axiom.ingestion.pdf import import_pdf
    from axiom.orchestration.pipeline import EmptySection, optimize_and_verify
    doc_id = import_pdf(carroll)
    ch = f"{doc_id}-c3"
    with db.connect() as c:
        c.execute("UPDATE sections SET stage='transcribed' WHERE chapter_id=?", (ch,))
    with pytest.raises(EmptySection):
        optimize_and_verify(f"{doc_id}-s3.1")
    assert extract_chapter(doc_id, ch) > 100
    assert {r["stage"] for r in db.q("SELECT stage FROM sections WHERE chapter_id=?", (ch,))} == {"extracted"}
    assert extract_chapter(doc_id, ch) == 0  # second call: blocks exist → skipped


# ---------------------------------------------------------------- robustness
def test_malformed_pdf_rejected_gracefully(tmp_db, tmp_path):
    from fastapi.testclient import TestClient

    from axiom.api.app import create_app
    client = TestClient(create_app(start_worker=False))
    r = client.post("/api/documents", files={"file": ("broken.pdf", b"%PDF-1.4 garbage \x00\x01", "application/pdf")})
    assert r.status_code == 422
    r = client.post("/api/documents", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert r.status_code == 400


# ---------------------------------------------------------------- optimizer integrity
def _row(id, typ, text, ord_, cont=0, eq=None, conf=0.9):
    return {"id": id, "block_type": typ, "transcribed_md": text, "raw_text": text, "continues_prev": cont,
            "eq_number": eq, "ord": ord_, "page": 1, "confidence": conf}


def test_plan_windows_never_separates_equation_from_context():
    from axiom.transformations.optimize import plan_windows
    blocks = []
    for i in range(30):
        blocks.append(_row(f"p{i}", "paragraph", "x" * 400, len(blocks)))
        if i % 3 == 0:
            blocks.append(_row(f"e{i}", "equation", "$$a=b$$", len(blocks), eq=f"1.{i}"))
    ws = plan_windows(blocks, max_chars=1500)
    assert sum(len(w.blocks) for w in ws) == len(blocks)
    for w in ws:
        assert w.blocks[0]["block_type"] == "paragraph"
        assert w.blocks[-1]["block_type"] != "equation"  # equation is followed by its context
        # cut only between paragraph and paragraph
    for a, b in zip(ws, ws[1:]):
        assert a.blocks[-1]["block_type"] == "paragraph" and b.blocks[0]["block_type"] == "paragraph"


def test_split_equations_inserts_verbatim_and_dedupes():
    from axiom.transformations.optimize import _split_equations
    by_id = {"bp": _row("bp", "paragraph", "lead", 0), "be": _row("be", "equation", "$$F = ma$$", 1),
             "be2": _row("be2", "equation", "$$E = mc^2$$", 2)}
    rev = {"B1": "bp", "B2": "be", "B3": "be2"}
    units = [
        {"source_ids": ["B1", "B2"], "kind": "prose", "min_level": 1, "text_md": "Newton: $$F=ma$$", "fast_md": None},
        {"source_ids": ["B3"], "kind": "equation", "min_level": 2, "text_md": ""},
        {"source_ids": ["B3"], "kind": "remark", "min_level": 2, "text_md": "Energy."},
    ]
    out = _split_equations(units, rev, by_id)
    kinds = [(u["kind"], tuple(u["source_ids"])) for u in out]
    assert kinds == [("prose", ("B1",)), ("equation", ("B2",)), ("equation", ("B3",)), ("remark", ("B3",))]
    assert "$$" not in out[0]["text_md"]  # the model's copy of the equation was removed (verbatim one inserted)


def test_optimize_section_end_to_end_with_fake_model(tmp_db, monkeypatch):
    """Integrity rules hold even when the model misbehaves: drops a block, rewrites an equation."""
    from axiom.model_adapters.base import ChatResult
    from axiom.orchestration import llm
    from axiom.transformations.optimize import optimize_section
    from axiom.verification.coverage import audit_section

    with db.connect() as c:
        c.execute("INSERT INTO documents VALUES ('d','T','f','h',10,'now','{}')")
        c.execute("INSERT INTO chapters(id,doc_id,number,title,page_start,page_end) VALUES ('d-c1','d',1,'C',1,2)")
        c.execute("INSERT INTO sections(id,doc_id,chapter_id,ord,number,title,page_start,page_end) VALUES"
                  " ('d-s1.1','d','d-c1',0,'1.1','S',1,2)")
        for i, (t, md, eq) in enumerate([("heading", "INTRO", None), ("paragraph", "Newton says force is mass times acceleration.", None),
                                          ("equation", "$$\\mathbf{F} = m\\mathbf{a}$$", "1.2"),
                                          ("paragraph", "A dropped remark about history.", None)]):
            c.execute("INSERT INTO blocks(id,doc_id,section_id,ord,page,x0,y0,x1,y1,block_type,raw_text,content_hash,"
                      "transcribed_md,transcription_method,confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (f"b{i}", "d", "d-s1.1", i, 1, 0, i * 10, 100, i * 10 + 9, t, md, "h", md, "vision_ocr", 0.9))

    calls = []

    class Fake:
        name = "fake"; model_class = "local_text"; supports_vision = False
        def chat(self, req):
            calls.append(req)
            out = {"units": [
                {"source_ids": ["B1", "B2"], "kind": "definition", "role": "essential_definition", "min_level": 1,
                 "title": "Newton II", "text_md": "Force equals mass times acceleration: $$F = m a + 1$$",
                 "fast_md": None, "deep_md": None, "annotation_md": None, "coverage": "merged", "omission_reason": None}],
                "notation": [{"symbol": "\\mathbf{F}", "meaning": "force", "convention": None}],
                "concepts": [], "summary": "Newton's second law."}
            return ChatResult(json.dumps(out), "fake", 10, 10, 0.01, "stop")

    monkeypatch.setitem(llm._adapters, "local", Fake())
    import threading
    monkeypatch.setitem(llm._sems, "local", threading.Semaphore(1))
    optimize_section("d-s1.1")
    units = [json.loads(r["data"]) for r in db.q("SELECT data FROM units WHERE section_id='d-s1.1' ORDER BY ord")]
    eq = [u for u in units if u["kind"] == "equation"]
    assert len(eq) == 1 and eq[0]["latex"] == "\\mathbf{F} = m\\mathbf{a}"  # verbatim source, not the model's
    prose = [u for u in units if u["kind"] == "definition"][0]
    assert any("AI" in f["message"] for f in prose["flags"])  # model's non-verbatim display math is flagged
    restored = [u for u in units if "b3" in u["block_ids"]]
    assert restored and restored[0]["coverage"] == "expandable"  # dropped block restored, not lost
    rep = audit_section("d-s1.1")
    assert rep["ok"] and not rep["missing"]
    # caching: second run makes no new model call
    n = len(calls)
    optimize_section("d-s1.1")
    assert len(calls) == n


# ---------------------------------------------------------------- jobs: dependencies + resume
def test_jobs_dependencies_and_recovery(tmp_db, monkeypatch):
    from axiom.orchestration import jobs
    with db.connect() as c:
        c.execute("INSERT INTO documents VALUES ('d','T','f','h',10,'now','{}')")
        c.execute("INSERT INTO chapters(id,doc_id,number,title,page_start,page_end) VALUES ('d-c1','d',1,'C',1,2)")
        for i in range(3):
            c.execute("INSERT INTO sections(id,doc_id,chapter_id,ord,number,title,page_start,page_end) VALUES (?,?,?,?,?,?,?,?)",
                      (f"d-s1.{i+1}", "d", "d-c1", i, f"1.{i+1}", "S", 1, 2))
    order = []
    monkeypatch.setattr(jobs, "_run", lambda job: (order.append((job["kind"], job["target_id"])),
                                                   db.connect().__enter__().execute("UPDATE jobs SET status='done' WHERE id=?", (job["id"],)),
                                                   db.conn().commit()))
    ids = jobs.enqueue_document("d", [1])
    assert len(ids) == 10  # extract + 3 × (transcribe, optimize, verify)
    assert jobs.enqueue_document("d", [1]) == ids  # idempotent
    # simulate a crash: one job stuck in 'running'
    with db.connect() as c:
        c.execute("UPDATE jobs SET status='running' WHERE id=?", (ids[1],))
    assert jobs.recover() == 1
    jobs.run_until_idle()
    pos = {k: i for i, k in enumerate(order)}
    assert pos[("extract", "d-c1")] < pos[("transcribe", "d-s1.1")]
    assert pos[("transcribe", "d-s1.2")] < pos[("optimize", "d-s1.2")]
    assert pos[("optimize", "d-s1.1")] < pos[("optimize", "d-s1.2")] < pos[("optimize", "d-s1.3")]
    assert pos[("optimize", "d-s1.3")] < pos[("verify", "d-s1.3")]
    assert not db.q("SELECT 1 FROM jobs WHERE status!='done'")


# ---------------------------------------------------------------- anki
def test_anki_math_conversion_and_escaping():
    from axiom.anki.export import to_anki_math, to_tsv
    s = to_anki_math("Metric $\\eta_{\\mu\\nu}$ with a<b:\n$$x^2 = 1$$ **bold**")
    assert s == "Metric \\(\\eta_{\\mu\\nu}\\) with a&lt;b:<br>\\[x^2 = 1\\] <b>bold</b>"
    tsv = to_tsv([{"guid": "g1", "front": "F\twith tab", "back": "B", "source": "src", "tags": ["t1"]}], "Deck::Sub")
    rows = tsv.strip().split("\n")
    assert "#guid column:1" in rows and rows[-1].split("\t")[0] == "g1" and len(rows[-1].split("\t")) == 4


def test_equation_tag_ocr_letter_l_is_read_as_digit():
    from axiom.extraction.layout import EQ_TAG_RE, _fix_digits
    m = EQ_TAG_RE.match("(l.41)")
    assert m and f"{_fix_digits(m.group(1))}.{_fix_digits(m.group(2))}" == "1.41"
    assert EQ_TAG_RE.match("( 1.29)") and not EQ_TAG_RE.match("(a.b)")


def test_mislabelled_equation_unit_is_repaired():
    from axiom.transformations.optimize import _split_equations
    by_id = {"bp": _row("bp", "paragraph", "lead", 0), "be": _row("be", "equation", "$$F = ma$$", 1)}
    rev = {"B1": "bp", "B2": "be"}
    out = _split_equations([{"source_ids": ["B1", "B2"], "kind": "equation", "min_level": 2, "text_md": "",
                             "fast_md": None}], rev, by_id)
    # prose block with no text → left uncovered (restored verbatim later); equation emitted verbatim
    assert [(u["kind"], u["source_ids"]) for u in out] == [("equation", ["B2"])]


def test_scan_detection_and_vision_policy(carroll):
    from axiom.transformations.transcribe import needs_vision
    from axiom.ingestion.pdf import is_scanned
    assert is_scanned(pymupdf.open(carroll)) is True
    para = {"block_type": "paragraph", "math_score": 0.0}
    assert needs_vision(para, scanned=True) and not needs_vision(para, scanned=False)
    assert needs_vision({"block_type": "equation", "math_score": 0.0})


def test_empty_mixed_unit_passes_validation_and_is_repaired():
    """Observed on Carroll (10 of 11 fallback windows): the model emits an empty unit spanning
    [paragraph, equation]. It must be repaired (equation kept verbatim, paragraph restored), not rejected."""
    from axiom.transformations.optimize import Window, _split_equations, _validator
    blocks = [_row("bp", "paragraph", "lead", 0), _row("be", "equation", "$$F = ma$$", 1)]
    ids = {"bp": "B1", "be": "B2"}
    rev, by_id = {v: k for k, v in ids.items()}, {b["id"]: b for b in blocks}
    unit = {"source_ids": ["B1", "B2"], "kind": "prose", "min_level": 2, "text_md": "", "fast_md": None,
            "coverage": "merged"}
    _validator(Window(blocks=blocks), ids)({"units": [unit]})  # must not raise
    out = _split_equations([unit], rev, by_id)
    assert [(u["kind"], u["source_ids"]) for u in out] == [("equation", ["B2"])]
    import pytest
    from axiom.orchestration.llm import ValidationFailed
    with pytest.raises(ValidationFailed):  # empty pure-prose unit is still a failure
        _validator(Window(blocks=blocks[:1]), {"bp": "B1"})({"units": [dict(unit, source_ids=["B1"])]})


def test_post_endpoints_take_json_bodies(tmp_db):
    """Regression: a request model declared inside create_app() (with postponed annotations) was silently
    treated as a *query* parameter, so the hint button returned 422."""
    from fastapi.testclient import TestClient

    from axiom.api.app import create_app
    spec = TestClient(create_app(start_worker=False)).get("/openapi.json").json()
    for path in ("/api/exercises/{exercise_id}/hint", "/api/ask", "/api/documents/{doc_id}/process"):
        op = spec["paths"][path]["post"]
        assert "requestBody" in op, path
        assert not [p for p in op.get("parameters", []) if p["in"] == "query"], path


def test_equations_copied_into_prose_are_interleaved_in_source_order():
    """Observed in §1.1: one prose unit contained (1.1) and (1.2); the repair used to strip both and append them
    after the paragraph, leaving gaps and producing an Anki card that paired the wrong equation."""
    from axiom.transformations.optimize import _split_equations
    by_id = {"p1": _row("p1", "paragraph", "The force is", 0), "e1": _row("e1", "equation", "$$F = GMm/r^2$$", 1),
             "p2": _row("p2", "paragraph", "and Newton gives", 2), "e2": _row("e2", "equation", "$$F = ma$$", 3)}
    rev = {"B1": "p1", "B2": "e1", "B3": "p2", "B4": "e2"}
    unit = {"source_ids": ["B1", "B2", "B3", "B4"], "kind": "prose", "role": "core", "min_level": 1, "title": None,
            "text_md": "The force is:\n$$F = GMm/r^2$$\nwhich with Newton's law gives:\n$$F=ma$$",
            "fast_md": "Gravity is inverse-square; combine with $F=ma$.", "deep_md": None, "coverage": "merged"}
    out = _split_equations([unit], rev, by_id)
    assert [(u["kind"], u["source_ids"]) for u in out] == [
        ("prose", ["B1"]), ("equation", ["B2"]), ("prose", ["B3"]), ("equation", ["B4"])]
    assert out[0]["text_md"] == "The force is:" and "$$" not in out[2]["text_md"]
    assert out[0]["fast_md"] and out[2]["fast_md"] is None and out[2]["min_level"] == 2  # no duplicate in Fast Track
    assert out[1]["_pos"] < out[2]["_pos"] < out[3]["_pos"]
