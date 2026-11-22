"""Tests for the second batch of features: faithfulness verifier, digests, solutions, mastery checks,
sandboxed visualizations, Anki packages, premium escalation, crop refitting and exercise assembly.
All model calls go to fakes; no GPU needed."""

import json
import threading

import pymupdf
import pytest

from axiom.model_adapters.base import ChatResult
from axiom.storage import db


class Router:
    """Fake adapter: answers by matching a substring of the system prompt / task text."""
    name = "fake"
    model_class = "local_text"
    supports_vision = False

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def chat(self, req):
        self.calls.append(req)
        for key, fn in self.routes.items():
            if key in (req.system or "") or key in req.user_text[:400]:
                out = fn(req)
                return ChatResult(out if isinstance(out, str) else json.dumps(out), "fake", 1, 1, 0.0, "stop")
        raise AssertionError(f"unrouted request: {req.system!r}")


@pytest.fixture
def fake(monkeypatch):
    from axiom.orchestration import llm

    def install(routes, cls="local_text"):
        """Fake model: "local_text"/"local_vlm"/"local" → the local model; "premium" → a fake subscription agent
        (claude-code:sonnet); any other value is used as the model id."""
        mid = {"local_text": "local", "local_vlm": "local", "premium": "claude-code:sonnet"}.get(cls, cls)
        r = Router(routes)
        r.name = "fake-" + mid
        r.supports_vision = True
        monkeypatch.setitem(llm._adapters, mid, r)
        monkeypatch.setitem(llm._sems, "local" if mid == "local" else mid, threading.Semaphore(2))
        return r
    return install


def _section(blocks, units=None, summary=None, exercises=False):
    with db.connect() as c:
        c.execute("INSERT INTO documents VALUES ('d','T','f','h',10,'now','{}')")
        c.execute("INSERT INTO chapters(id,doc_id,number,title,page_start,page_end) VALUES ('d-c1','d',1,'C',1,2)")
        c.execute("INSERT INTO sections(id,doc_id,chapter_id,ord,number,title,page_start,page_end,is_exercises,summary_md,"
                  "stage,status) VALUES ('d-s1.1','d','d-c1',0,'1.1','S',1,2,?,?,'optimized','done')", (int(exercises), summary))
        for i, (t, md) in enumerate(blocks):
            c.execute("INSERT INTO blocks(id,doc_id,section_id,ord,page,x0,y0,x1,y1,block_type,raw_text,content_hash,"
                      "transcribed_md,transcription_method,confidence,eq_number) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (f"b{i}", "d", "d-s1.1", i, 1 + i // 3, 0, i * 10, 100, i * 10 + 9, t, md, "h", md, "vision_ocr", 0.9,
                       str(i) if t == "exercise" else None))
        for i, u in enumerate(units or []):
            uid = f"d-s1.1-u{i:03d}"
            c.execute("INSERT INTO units(id, section_id, ord, data) VALUES (?,?,?,?)", (uid, "d-s1.1", i, json.dumps(dict(u, id=uid))))
            for b in u["block_ids"]:
                c.execute("INSERT OR REPLACE INTO coverage VALUES (?,?,?,?,NULL)", (b, "d-s1.1", u["coverage"], json.dumps([uid])))


def _unit(text, blocks, kind="prose", fast=None, deep=None, coverage="rewritten"):
    return {"kind": kind, "role": "core", "min_level": 1, "title": None, "text_md": text, "fast_md": fast, "deep_md": deep,
            "block_ids": blocks, "coverage": coverage, "ai_generated": True, "flags": [], "pages": [1]}


# ---------------------------------------------------------------- faithfulness
def test_faithfulness_reverts_contradictions_and_flags_dropped_conditions(tmp_db, fake):
    from axiom.verification.coverage import audit_section
    from axiom.verification.faithfulness import verify_section
    _section([("paragraph", "Orthogonal matrices form O(3); those with unit determinant form SO(3)."),
              ("paragraph", "For small velocities, the Galilean addition law holds."),
              ("paragraph", "Rotations preserve lengths.")],
             [_unit("SO(3) is the set of all orthogonal matrices.", ["b0"]),
              _unit("The Galilean addition law holds.", ["b1"], deep="Deep remark."),
              _unit("Rotations preserve lengths.", ["b2"], fast="Rotations keep lengths.")])

    def verdicts(req):
        assert "SOURCE" in req.user_text and req.thinking  # independent check runs with reasoning
        return {"problems": [
            {"unit": "U1", "field": "text", "verdict": "contradicted", "claim": "SO(3) is the set of all orthogonal",
             "explanation": "O(3) vs SO(3) conflated", "source_evidence": "form O(3)"},
            {"unit": "U2", "field": "text", "verdict": "dropped_condition", "claim": "holds",
             "explanation": "only for small velocities", "source_evidence": "For small velocities"}]}
    fake({"exacting physics editor": verdicts})
    stats = verify_section("d-s1.1")
    assert stats == {"units_checked": 3, "verified": 1, "flagged": 1, "reverted": 1, "failed_batches": 0,
                     "model_class": "local_text", "model": "local_text"}
    u = [json.loads(r["data"]) for r in db.q("SELECT data FROM units ORDER BY ord")]
    assert u[0]["text_md"].startswith("Orthogonal matrices form O(3)") and not u[0]["ai_generated"]
    assert u[0]["verification"]["status"] == "reverted" and u[0]["flags"][0]["kind"] == "reverted"
    assert u[1]["verification"]["status"] == "flagged" and "small velocities" in u[1]["flags"][0]["message"]
    assert u[1]["text_md"] == "The Galilean addition law holds."  # flagged, not rewritten by the checker
    assert u[2]["verification"]["status"] == "verified"
    assert db.q1("SELECT status FROM coverage WHERE block_id='b0'")["status"] == "preserved"
    assert audit_section("d-s1.1")["ok"]


def test_faithfulness_drops_only_the_bad_fast_version(tmp_db, fake):
    from axiom.verification.faithfulness import verify_section
    _section([("paragraph", "The interval is invariant.")], [_unit("The interval is invariant.", ["b0"], fast="Time is invariant.")])
    fake({"exacting physics editor": lambda r: {"problems": [
        {"unit": "U1", "field": "fast", "verdict": "contradicted", "claim": "Time is invariant", "explanation": "x",
         "source_evidence": "interval"}]}})
    verify_section("d-s1.1")
    u = json.loads(db.q1("SELECT data FROM units")["data"])
    assert u["fast_md"] is None and u["text_md"] == "The interval is invariant." and u["verification"]["status"] == "flagged"


def test_faithfulness_shows_neighbouring_blocks_as_context(tmp_db):
    """A unit citing only the first half of a split equation must see the continuation (Carroll eq. 1.69)."""
    from axiom.verification.faithfulness import _prompt
    _section([("paragraph", "Earlier text."),
              ("paragraph", "Multilinearity means $$T(a\\omega + b\\eta, cV + dW) = acT(\\omega, V)$$"),
              ("equation", "$$+ adT(\\omega, W) + bcT(\\eta, V) + bdT(\\eta, W).$$"),
              ("paragraph", "Later text."), ("paragraph", "Far away.")],
             [_unit("T is linear in each slot: four terms.", ["b1"])])
    blocks = {b["id"]: dict(b) for b in db.q("SELECT * FROM blocks")}
    u = dict(json.loads(db.q1("SELECT data FROM units")["data"]))
    user, _ = _prompt([u], blocks)
    src, ctx = user.split("CONTEXT (neighbouring text, not cited):")
    assert "acT" in src and "bdT" not in src.split("SOURCE:")[1]
    assert "bdT" in ctx and "Earlier text." in ctx and "Later text." in ctx and "Far away." not in ctx


def test_digest_consolidates_long_summaries_only(tmp_db, fake):
    from axiom.transformations.digest import digest_section
    _section([("paragraph", "x")], summary="This passage introduces A. " * 40)
    r = fake({"summaries of consecutive passages": lambda req: "Short digest of $A$."})
    assert digest_section("d-s1.1") == "Short digest of $A$."
    with db.connect() as c:
        c.execute("UPDATE sections SET summary_md='Brief.', digest_md=NULL")
    n = len(r.calls)
    assert digest_section("d-s1.1") == "Brief." and len(r.calls) == n  # short summary: no model call


# ---------------------------------------------------------------- exercises
def test_exercise_page_break_is_joined(tmp_db):
    from axiom.exercises.engine import extract_exercises
    _section([("exercise", "1. Show that the point of this"), ("paragraph", "Hint: start here."),
              ("paragraph", "Use the metric."), ("paragraph", "problem is to think deeply.")], exercises=True)
    # b3 is the first block on page 2 and continues a sentence broken by the page turn
    with db.connect() as c:
        c.execute("UPDATE blocks SET transcribed_md='Show that the point of this' WHERE id='b0'")
        c.execute("UPDATE blocks SET page=1 WHERE id IN ('b0','b1','b2')")
        c.execute("UPDATE blocks SET transcribed_md='Use the metric, and note that the point of this' WHERE id='b2'")
    extract_exercises("d-s1.1")
    st = db.q1("SELECT statement_md FROM exercises")["statement_md"]
    assert "the point of this problem is to think deeply." in st


def test_solutions_cross_check_and_answer_check(tmp_db, fake):
    from axiom.exercises import solutions
    _section([("exercise", "1. Compute x.")], exercises=True)
    from axiom.exercises.engine import extract_exercises
    extract_exercises("d-s1.1")
    ex_id = db.q1("SELECT id FROM exercises")["id"]
    sol = lambda ans: (lambda r: {"parts": [{"label": "main", "solution_md": "Work.", "final_answer_md": ans,
                                             "answer_type": "expression"}]})
    fake({"careful theoretical physicist": sol("$x = 2$"), "independent grader": sol("$x = 3$"),
          "compare two independent": lambda r: {"parts": [{"label": "main", "status": "inconsistent", "explanation": "2 vs 3"}]},
          "fair, precise physics grader": lambda r: {"verdict": "cannot_judge", "feedback_md": "References disagree.",
                                                     "first_error_md": ""}})
    s = solutions.get_solution(ex_id)
    assert s["status"] == "inconsistent" and s["alternative"]["parts"][0]["final_answer_md"] == "$x = 3$"
    assert solutions.get_solution(ex_id, generate=False) == s  # stored
    res = solutions.check_answer(ex_id, "I get x = 2")
    assert res["verdict"] == "cannot_judge" and res["reference_status"] == "inconsistent"
    assert db.q1("SELECT COUNT(*) n FROM answer_checks")["n"] == 1
    with pytest.raises(ValueError):
        solutions.check_answer(ex_id, "   ")


def test_quiz_blind_check_drops_wrong_keys_and_grading(tmp_db, fake):
    from axiom.exercises import mastery
    _section([("paragraph", "p")], [_unit("The metric is symmetric.", ["b0"], kind="definition", coverage="merged"),
                                     _unit("Proper time is invariant.", ["b0"], kind="result", coverage="merged")])
    qs = {"questions": [
        {"type": "mcq", "prompt_md": "Is the metric symmetric?", "options": ["yes", "no", "sometimes", "never"], "correct": 0,
         "answer_md": "yes", "explanation_md": "def", "source_units": ["U1"]},
        {"type": "mcq", "prompt_md": "Bad key", "options": ["a", "b", "c", "d"], "correct": 2, "answer_md": "c",
         "explanation_md": "", "source_units": ["U2"]},
        {"type": "short", "prompt_md": "What is invariant?", "options": [], "correct": 0, "answer_md": "proper time",
         "explanation_md": "", "source_units": ["U2"]}]}
    blind = lambda r: {"answer": "A"}  # agrees with question 1 (key A), disagrees with question 2 (key C)
    fake({"rigorous physics exam": lambda r: qs, "answer exam questions": blind,
          "grade short physics": lambda r: {"score": 0.5, "feedback_md": "Partly."}})
    quiz = mastery.generate_quiz("section", "d-s1.1", n=3)
    assert quiz["dropped_by_blind_check"] == 1 and [q["type"] for q in quiz["questions"]] == ["mcq", "short"]
    pub = mastery.public_quiz(quiz)
    assert "correct" not in pub["questions"][0] and "answer_md" not in pub["questions"][0]
    key = quiz["questions"][0]["correct"]
    assert quiz["questions"][0]["options"][key] == "yes"  # shuffle keeps the key pointing at the right option
    res = mastery.grade(quiz["id"], {"q1": key, "q2": "proper time, I think"})
    assert res["score"] == pytest.approx(0.75)
    assert mastery.mastery("d")["d-s1.1"] == pytest.approx(0.75)


# ---------------------------------------------------------------- visualizations
@pytest.mark.parametrize("bad", [
    "<script src='https://cdn.example/x.js'></script>", "<script>fetch('/api/documents')</script>",
    "<script>window.parent.document.body.innerHTML=''</script>", "<script>localStorage.x=1</script>",
    "<script>eval('1')</script>", "<iframe></iframe><script></script>", "<div style=\"background:url(http://x)\">"])
def test_visualization_screen_rejects_escape_attempts(bad):
    from axiom.visualization.generate import screen
    assert screen(bad)


def test_visualization_served_in_csp_sandbox(tmp_db, fake):
    from fastapi.testclient import TestClient

    from axiom.api.app import create_app
    from axiom.visualization.generate import screen
    good = "<canvas id=c width=600 height=400></canvas><input type=range id=v><script>const g=document.getElementById('c').getContext('2d');g.fillRect(0,0,10,10)</script>"
    assert not screen(good)
    _section([("paragraph", "p")], [_unit("Hyperbolae of constant interval.", ["b0"])])
    reviews = iter([{"errors": ["x' axis tilts down: canvas y is inverted"], "notes": []},
                    {"errors": [], "notes": ["label says speed for a signed velocity"]}])
    fixed = iter([good, good.replace("10,10", "20,20")])  # the retry returns corrected code
    calls = fake({"self-contained physics visualizations": lambda r: {"title": "Interval", "description_md": "Drag.",
                                                                      "html": next(fixed)},
                  "meticulous physics reviewer": lambda r: next(reviews)})
    client = TestClient(create_app(start_worker=False))
    resp = client.post("/api/units/d-s1.1-u000/visualization")
    assert resp.json().get("ai_generated") is True and resp.json()["reviewed"] is True, resp.text
    assert resp.json()["review_notes"] == ["label says speed for a signed velocity"]  # minor: shown, not blocking
    gens = [c for c in calls.calls if "visualizations" in c.system]
    assert len(gens) == 2 and "x' axis tilts down" in gens[1].user_text  # review fed back to the generator
    r = client.get("/api/units/d-s1.1-u000/visualization.html")
    csp = r.headers["content-security-policy"]
    assert csp.startswith("sandbox allow-scripts;") and "allow-same-origin" not in csp and "connect-src 'none'" in csp
    assert "<canvas" in r.text and len(calls.calls) == 4


# ---------------------------------------------------------------- anki package
def test_apkg_has_stable_guids(tmp_path):
    import sqlite3
    import zipfile

    from axiom.anki.export import to_apkg
    cards = [{"guid": "axiom-abc", "front": "Define: $\\eta$", "back": "$$x$$", "source": "Book §1", "tags": ["axiom::ch1"]}]
    p = tmp_path / "d.apkg"
    assert to_apkg(cards, "Deck", p) == 1
    zipfile.ZipFile(p).extract("collection.anki2", tmp_path)
    row = sqlite3.connect(tmp_path / "collection.anki2").execute("SELECT guid, flds FROM notes").fetchone()
    assert row[0] == "axiom-abc" and "\\(\\eta\\)" in row[1] and row[1].endswith("axiom-abc")


# ---------------------------------------------------------------- premium routing (no real key: fake adapter)
def test_escalation_to_premium_and_budget(tmp_db, fake, monkeypatch):
    from axiom.model_adapters.base import ChatRequest
    from axiom.orchestration import llm
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
    local = fake({"": lambda r: "not json"})
    prem = fake({"": lambda r: {"ok": True}}, cls="api:anthropic")
    value, meta = llm.call_json("t", "local_text", ChatRequest("s", "u"), "v1", lambda d: d, doc_id="d", profile="balanced")
    assert value == {"ok": True} and meta["escalated"] and len(local.calls) == 2 and len(prem.calls) == 1
    with pytest.raises(llm.ValidationFailed):  # economy never escalates
        llm.call_json("t2", "local_text", ChatRequest("s", "u2"), "v1", lambda d: d, doc_id="d", profile="economy")
    with db.connect() as c:  # exhaust the balanced budget (40 premium calls)
        for _ in range(40):
            c.execute("INSERT INTO llm_calls(doc_id,task,model,model_class,cached,ok,seconds,ts) VALUES ('d','t','m','premium',0,1,0,'x')")
    with pytest.raises(llm.BudgetExceeded):
        llm.call("t3", "api:anthropic", ChatRequest("s", "u3"), "v1", doc_id="d", profile="balanced")


# ---------------------------------------------------------------- crop refit
def test_fit_to_ink_grows_cut_box_but_not_into_neighbour():
    from axiom.extraction.extract import fit_to_ink
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=400)
    page.draw_rect(pymupdf.Rect(50, 100, 300, 160), color=(0, 0, 0), fill=(0, 0, 0))  # tall "matrix"
    page.draw_rect(pymupdf.Rect(50, 165, 300, 175), color=(0, 0, 0), fill=(0, 0, 0))  # next text line, 5pt below
    x0, y0, x1, y1 = fit_to_ink(page, (40, 105, 310, 140), upper=80, lower=163)
    assert y0 <= 100 and 160 <= y1 <= 163  # whole matrix, stops before the neighbour
    assert fit_to_ink(page, (40, 90, 310, 162), upper=80, lower=163) == (40, 90, 310, 162)  # clean edges: unchanged


# ---------------------------------------------------------------- deterministic optimizer context
def test_optimize_chain_spans_chapters_and_notation_context_ignores_row_ids(tmp_db):
    from axiom.orchestration import jobs
    from axiom.transformations.optimize import _context
    with db.connect() as c:
        c.execute("INSERT INTO documents VALUES ('d','T','f','h',10,'now','{}')")
        for ch in (1, 2):
            c.execute("INSERT INTO chapters(id,doc_id,number,title,page_start,page_end) VALUES (?,?,?,?,1,2)",
                      (f"d-c{ch}", "d", ch, f"C{ch}"))
            for i in range(2):
                c.execute("INSERT INTO sections(id,doc_id,chapter_id,ord,number,title,page_start,page_end)"
                          " VALUES (?,?,?,?,?,?,1,2)", (f"d-s{ch}.{i+1}", "d", f"d-c{ch}", i, f"{ch}.{i+1}", "S"))
    jobs.enqueue_document("d", [2, 1])
    first_ch2 = db.q1("SELECT depends_on FROM jobs WHERE kind='optimize' AND target_id='d-s2.1'")["depends_on"]
    last_ch1 = db.q1("SELECT id FROM jobs WHERE kind='optimize' AND target_id='d-s1.2'")["id"]
    assert "~" + last_ch1 in json.loads(first_ch2)  # chapter 2 waits for chapter 1 (ordering only)

    def put(order):
        with db.connect() as c:
            c.execute("DELETE FROM notation")
            for sid, sym, meaning in order:
                c.execute("INSERT INTO notation(doc_id, section_id, symbol, meaning) VALUES ('d',?,?,?)", (sid, sym, meaning))
    rows = [("d-s1.1", "\\eta", "metric (old)"), ("d-s1.2", "\\eta", "Minkowski metric"), ("d-s1.1", "c", "speed of light")]
    sec = db.q1("SELECT * FROM sections WHERE id='d-s2.2'")
    put(rows)
    a = _context(sec, "d", None)
    put([rows[1], rows[0], rows[2]])  # s1.2 re-optimized first -> different row ids, same book
    assert _context(sec, "d", None) == a and "Minkowski metric" in a and "metric (old)" not in a


# ---------------------------------------------------------------- teaching extras: notes, diagrams, xrefs
def test_faithfulness_checks_equation_notes_and_drops_only_them(tmp_db, fake):
    """Equation units carry AI explanations now (annotation, deep dive, study notes); a false one is dropped, the
    verbatim equation is never touched."""
    from axiom.verification.faithfulness import verify_section
    eq = {"kind": "equation", "role": "key_equation", "min_level": 1, "title": None, "text_md": "",
          "latex": "E = mc^2", "eq_number": "1.1", "annotation_md": "Energy of a body at rest.",
          "deep_md": "Expanding $\\gamma m$ for small $v$ gives $$E \\approx m + \\tfrac12 m v^2.$$",
          "notes": [{"kind": "pitfall", "md": "Here $c$ is not set to 1."}],
          "block_ids": ["b0"], "coverage": "preserved", "ai_generated": False, "flags": [], "pages": [1]}
    _section([("equation", "$$E = mc^2$$ (1.1)")], [eq])
    seen = {}

    def verdicts(req):
        seen["user"] = req.user_text
        return {"problems": [{"unit": "U1", "field": "notes", "verdict": "contradicted", "claim": "c is not set to 1",
                              "explanation": "the book uses c = 1", "source_evidence": "c = 1"},
                             {"unit": "U1", "field": "text", "verdict": "contradicted", "claim": "E = mc^2",
                              "explanation": "bogus", "source_evidence": ""}]}
    fake({"exacting physics editor": verdicts})
    stats = verify_section("d-s1.1")
    assert stats["units_checked"] == 1 and stats["flagged"] == 1
    assert "equation (verbatim from the source): $$E = mc^2$$" in seen["user"] and "notes (pitfall)" in seen["user"]
    u = json.loads(db.q1("SELECT data FROM units")["data"])
    assert u["notes"] == [] and u["latex"] == "E = mc^2" and u["deep_md"] and u["annotation_md"]
    assert u["verification"]["status"] == "flagged"


def test_visualizations_follow_their_passage_when_a_section_is_reoptimized(tmp_db):
    from axiom.transformations.optimize import _store
    _section([("paragraph", "a"), ("paragraph", "b"), ("paragraph", "c")],
             [_unit("A", ["b0"]), _unit("B", ["b1"]), _unit("C", ["b2"])])
    with db.connect() as c:
        for uid, blocks in (("d-s1.1-u001", ["b1"]), ("d-s1.1-u002", ["b2"])):
            c.execute("INSERT INTO visualizations VALUES (?,?,?,?)",
                      (uid, "d", json.dumps({"title": uid, "html": "<svg></svg>", "anchor_blocks": blocks}), "t"))
    # re-optimized: a new unit in front shifts every id; the passage of b2 is gone (merged into a heading here)
    _store("d-s1.1", "d", [_unit("New lead", ["b0"]), _unit("A", ["b0"]), _unit("B", ["b1"]),
                           dict(_unit("", ["b2"]), kind="heading")], [], [], [])
    viz = {r["unit_id"]: json.loads(r["data"])["title"] for r in db.q("SELECT unit_id, data FROM visualizations")}
    assert viz.get("d-s1.1-u002") == "d-s1.1-u001"  # B moved from u001 to u002 together with its picture
    detached = [k for k in viz if k.startswith("detached:d-s1.1:")]
    assert len(detached) == 1 and viz[detached[0]] == "d-s1.1-u002"  # kept, not lost, and not on a wrong unit
    assert "d-s1.1-u003" not in viz
    # it comes back when its passage does
    _store("d-s1.1", "d", [_unit("A", ["b0"]), _unit("B", ["b1"]), _unit("C", ["b2"])], [], [], [])
    viz = {r["unit_id"]: json.loads(r["data"])["title"] for r in db.q("SELECT unit_id, data FROM visualizations")}
    assert viz == {"d-s1.1-u001": "d-s1.1-u001", "d-s1.1-u002": "d-s1.1-u002"}


def test_xrefs_link_equations_figures_sections_and_report_dangling(tmp_db):
    from axiom.verification.xrefs import xrefs
    _section([("figure", ""), ("caption", "FIGURE 1.5 A light cone."), ("paragraph", "p")],
             [_unit("By (1.29) and Figure 1.5 (see Section 1.1); also (9.99).", ["b2"])])
    with db.connect() as c:
        c.execute("INSERT INTO blocks(id,doc_id,section_id,ord,page,x0,y0,x1,y1,block_type,raw_text,content_hash,"
                  "transcribed_md,transcription_method,confidence,eq_number) VALUES ('beq','d','d-s1.1',9,2,0,0,1,1,"
                  "'equation','','h','$$a=b$$ (1.29)','vision_ocr',0.9,'1.29')")
    x = xrefs("d")
    assert x["equations"]["1.29"] == {"block_id": "beq", "section_id": "d-s1.1", "page": 2, "latex": "a=b"}
    assert x["figures"]["1.5"]["image_url"] == "/api/blocks/b0/image"
    assert x["sections"]["1.1"]["section_id"] == "d-s1.1"
    assert x["unresolved"] == [{"kind": "equation", "ref": "9.99", "section_id": "d-s1.1"}]


def test_auto_diagrams_run_on_the_chosen_model_and_never_fail_the_run(tmp_db, fake, monkeypatch):
    from axiom.orchestration import jobs, models
    good = "<svg width=10 height=10><circle r=3 /></svg><script>1</script>"
    _section([("paragraph", "Light cones tilt near the horizon."), ("paragraph", "Plain.")],
             [dict(_unit("Light cones tilt.", ["b0"]), diagram_idea="Light cones at several r; slider for r."),
              _unit("Plain.", ["b1"])])
    monkeypatch.setattr(models, "available", lambda mid: mid in ("codex:default", "claude-code:sonnet", "local"))
    routes = {"self-contained physics visualizations": lambda r: {"title": "Cones", "description_md": "Drag r.",
                                                                  "html": good},
              "meticulous physics reviewer": lambda r: {"errors": [], "notes": []}}
    gen = fake(routes, "codex:default")
    rev = fake(routes, "claude-code:sonnet")
    assert models.load_routing()["auto_diagrams"] == "codex:default"
    ids = jobs.enqueue_diagrams("d-s1.1")
    assert len(ids) == 1 and jobs.enqueue_diagrams("d-s1.1") == ids  # one per suggested unit, idempotent
    job = dict(db.q1("SELECT * FROM jobs WHERE id=?", (ids[0],)))
    assert job["kind"] == "diagram" and job["model"] == "codex:default" and jobs.can_run(job)
    out = jobs._run_diagram(job)
    assert out["unit_id"] == "d-s1.1-u000"
    assert "Light cones at several r" in gen.calls[0].user_text  # the optimizer's idea reaches the generator
    assert rev.calls and "meticulous physics reviewer" in rev.calls[0].system  # reviewed by another vendor
    v = json.loads(db.q1("SELECT data FROM visualizations WHERE unit_id='d-s1.1-u000'")["data"])
    assert v["automatic"] and v["anchor_blocks"] == ["b0"]
    # a rejected diagram is skipped, not a failure
    monkeypatch.setitem(routes, "meticulous physics reviewer", lambda r: {"errors": ["wrong sign"], "notes": []})
    db.q("DELETE FROM visualizations")
    db.q("DELETE FROM llm_cache")  # same prompt: would otherwise reuse the passing review
    db.conn().commit()
    assert "skipped" in jobs._run_diagram(dict(job, profile="economy"))
    models.save_routing(auto_diagrams="off")
    db.q("DELETE FROM jobs")
    db.conn().commit()
    assert jobs.enqueue_diagrams("d-s1.1") == []


def test_jobs_api_priority_models_and_dependencies(tmp_db, monkeypatch):
    from fastapi.testclient import TestClient
    from axiom.api.app import create_app
    from axiom.orchestration import jobs, models
    _section([])
    monkeypatch.setattr(models, "available", lambda _: True)
    extract, tr, opt, verify, _ = jobs.enqueue_document("d", [1], parallel=True, model="claude-code:sonnet")
    with TestClient(create_app(start_worker=False)) as client:
        assert client.patch(f"/api/jobs/{opt}", json={"action": "move_first"}).status_code == 204
        first = client.get("/api/jobs").json()["jobs"][0]
        assert first["id"] == opt and first["depends_on"] == [tr] and first["parallel"]
        edit = {"action": "model", "model": "codex:default"}
        assert client.patch(f"/api/jobs/{tr}", json=edit).status_code == 409
        assert client.patch(f"/api/jobs/{opt}", json=edit).status_code == 204
        assert jobs._claim(("extract", "optimize"))["id"] == extract
        assert client.delete(f"/api/jobs/{extract}").status_code == 409
        assert client.delete(f"/api/jobs/{tr}").status_code == 204
        states = {j["id"]: j for j in client.get("/api/jobs").json()["jobs"]}
        assert {states[x]["status"] for x in (tr, opt, verify)} == {"cancelled"}
        assert states[opt]["model"] == "codex:default"


def test_remove_document_and_generated_cover(tmp_db, fake):
    from fastapi.testclient import TestClient
    from axiom.api.app import create_app
    from axiom.config import settings
    from axiom.orchestration import jobs
    _section([("paragraph", "text")], [_unit("text", ["b0"])])
    original = settings.doc_dir("d") / "original.pdf"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"owned copy")
    external = settings.data_dir / "upload-source.pdf"
    external.write_bytes(b"original upload")
    fake({"Design a sophisticated": lambda _: dict(palette=["#0d1130", "#ecca88", "#92cce9"],
          marks=[dict(x=.5, y=.5, radius=r, color=1, filled=False) for r in (.1, .2, .3)])}, cls="codex:default")
    with TestClient(create_app(start_worker=False)) as client:
        assert not client.get("/api/documents/d").json()["completed"]  # readable but not verified
        with db.connect() as c:
            c.execute("UPDATE sections SET stage='verified'")
        assert client.get("/api/documents/d").json()["completed"]
        jid = client.post("/api/documents/d/cover").json()["job_id"]
        assert client.delete("/api/documents/d").status_code == 409
        jobs._run(dict(db.q1("SELECT * FROM jobs WHERE id=?", (jid,))))
        assert client.get("/api/documents/d").json()["cover_revision"]
        assert client.get("/api/documents/d/cover.png").content.startswith(b"\x89PNG")
        assert client.delete("/api/documents/d").status_code == 204
        assert client.get("/api/documents/d/cover.png").status_code == 404
    assert not original.exists() and external.exists()
    for table in ("blocks", "units", "coverage", "jobs", "covers"):
        assert not db.q(f"SELECT 1 FROM {table}")


def test_study_deck_keeps_reviewed_figure_and_packages_media(tmp_db, fake, tmp_path):
    import zipfile
    import pymupdf
    from axiom.anki import export
    from axiom.config import settings
    from axiom.orchestration import jobs
    unit = _unit("The light cone divides the tangent plane into causal regions.", ["b0"])
    unit["verification"] = {"status": "verified"}
    _section([("paragraph", "light cone"), ("figure", ""), ("caption", "Inside the cone, events are timelike.")], [unit])
    with db.connect() as c:
        c.execute("UPDATE sections SET stage='verified'")
    img = settings.doc_dir("d") / "original.pdf"
    img.parent.mkdir(parents=True)
    with pymupdf.open() as pdf:
        pdf.new_page().draw_line((0, 11), (100, 17))
        pdf.save(img)
    source = ["u1"]
    fake({"Generate zero to three": lambda _: {"cards": [
        dict(kind="figure", question="What makes the events inside the depicted light cone timelike?",
             answer="They can be reached from the origin below light speed.", sources=source, figure="f1"),
        dict(kind="concept", question="What is the arbitrary component of the tensor?",
             answer="It is an arbitrary component.", sources=source),
    ]}, "Audit these candidate": lambda _: {"checks": "First grounded; second rote.", "keep": [0]}})
    jid, = jobs.enqueue_anki("d")
    jobs._run(dict(db.q1("SELECT * FROM jobs WHERE id=?", (jid,))))
    assert not jobs.enqueue_anki("d")
    cards = export.build_cards("d")
    assert len(cards) == 1 and cards[0]["figure"] == "b1"
    assert "timelike" not in export.to_tsv(cards, "Study")
    dst = tmp_path / "study.apkg"
    assert export.to_apkg(cards, "Study", dst) == 1
    with zipfile.ZipFile(dst) as pkg:
        media = json.loads(pkg.read("media"))
        assert list(media.values())[0].endswith(".png")
        assert pkg.read(next(iter(media))).startswith(b"\x89PNG")
    assert jobs.enqueue_anki("d", regenerate=True) != [jid]


def test_study_deck_spreads_caps_across_sections_and_chapters(tmp_db, monkeypatch):
    from axiom.anki import study
    _section([])
    monkeypatch.setattr(study, "PER_BOOK", 3)
    monkeypatch.setattr(study, "PER_CHAPTER", 2)
    with db.connect() as c:
        c.execute("INSERT INTO chapters(id,doc_id,number,title,page_start,page_end) VALUES ('d-c2','d',2,'C',2,3)")
        for i, ch in ((1, 1), (2, 1), (3, 2)):
            sid = f"s{i}"
            c.execute("INSERT INTO sections(id,doc_id,chapter_id,ord,number,title,page_start,page_end,stage) "
                      "VALUES (?,'d',?,?,?,'S',1,2,'verified')", (sid, f"d-c{ch}", i, sid))
            cards = [dict(guid=f"{sid}-{n}", front=f"Question {sid} {n}") for n in range(3)]
            c.execute("INSERT INTO study_cards VALUES (?,?)", (sid, json.dumps(cards)))
    assert [c["guid"] for c in study.build_cards("d")] == ["s1-0", "s2-0", "s3-0"]
    assert [c["guid"] for c in study.build_cards("d", [2])] == ["s3-0", "s3-1"]
