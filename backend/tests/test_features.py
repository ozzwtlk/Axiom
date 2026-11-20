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
