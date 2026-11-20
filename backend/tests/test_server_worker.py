"""Server (stores, serves, forwards) ⇄ worker (computes) split, against a real `axiom serve --role server` process."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time

import httpx
import pytest

TOKEN = "test-token-0123456789abcdef"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    data = tmp_path_factory.mktemp("server-data")
    port = _free_port()
    env = dict(os.environ, AXIOM_DATA_DIR=str(data), AXIOM_TOKEN=TOKEN, AXIOM_ROLE="server")
    p = subprocess.Popen([sys.executable, "-m", "axiom.cli", "serve", "--role", "server", "--port", str(port)],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(url + "/api/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        p.kill()
        pytest.fail("server did not start")
    yield {"url": url, "data": data}
    p.terminate()
    p.wait(10)


@pytest.fixture
def remote(server):
    """This test process acts as a worker: its db/files calls go to the server."""
    from axiom.orchestration import jobs
    from axiom.storage import db, files
    db.init_remote(server["url"], TOKEN)
    files.init_remote(server["url"], TOKEN)
    jobs.WORKER_NAME = "me"
    yield server
    db._remote = None
    files._remote = None
    jobs.WORKER_NAME = None


def test_internal_api_needs_the_token_and_stays_in_the_data_dir(server):
    r = httpx.post(server["url"] + "/internal/db/run", json={"sql": "SELECT 1", "params": []})
    assert r.status_code == 401
    h = {"Authorization": f"Bearer {TOKEN}"}
    for evil in ("..%2F..%2Fetc%2Fpasswd", "docs%2F..%2Faxiom.db", "docs/%2E%2E/axiom.db", "%2Fetc%2Fpasswd"):
        r = httpx.get(server["url"] + "/internal/files/" + evil, headers=h)
        assert r.status_code in (400, 403, 404) and b"root:" not in r.content, (evil, r.status_code)
    assert httpx.get(server["url"] + "/internal/files/axiom.db", headers=h).status_code == 403  # not shared


def test_remote_db_behaves_like_local_sqlite(remote):
    from axiom.storage import db
    with db.connect() as c:
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", ("t.k", "v1"))
        assert c.execute("SELECT value FROM meta WHERE key=?", ("t.k",)).fetchone()["value"] == "v1"  # read own write
        with db.connect() as inner:  # nested block joins the transaction
            inner.execute("UPDATE meta SET value='v2' WHERE key='t.k'")
    row = db.q1("SELECT key, value FROM meta WHERE key='t.k'")
    assert row["value"] == "v2" and row[0] == "t.k" and dict(row) == {"key": "t.k", "value": "v2"}
    with pytest.raises(RuntimeError):
        with db.connect() as c:
            c.execute("UPDATE meta SET value='lost' WHERE key='t.k'")
            raise RuntimeError("boom")
    assert db.q1("SELECT value FROM meta WHERE key='t.k'")["value"] == "v2"  # rolled back on the server
    assert db.q("SELECT 1 AS one")[0]["one"] == 1


def test_compute_is_forwarded_to_a_connected_worker(remote):
    import websockets
    url = remote["url"]
    assert httpx.post(url + "/api/ask", json={"section_id": "x", "selection": "y"}).status_code == 503  # nobody there
    ready, done = threading.Event(), threading.Event()

    async def fake_worker():
        async with websockets.connect(url.replace("http", "ws") + "/internal/worker",
                                      additional_headers={"Authorization": f"Bearer {TOKEN}"}) as ws:
            await ws.send(json.dumps({"type": "hello", "name": "laptop", "info": {},
                                      "models": {"models": [{"id": "claude-code:sonnet", "label": "Sonnet",
                                                             "kind": "subscription", "available": True}]}}))
            ready.set()
            msg = json.loads(await ws.recv())
            assert msg["path"] == "/api/ask" and json.loads(__import__("base64").b64decode(msg["body"]))["selection"] == "y"
            body = json.dumps({"answer_md": "from the laptop", "model": "claude-code:sonnet", "cached": False,
                               "context_used": []}).encode()
            await ws.send(json.dumps({"type": "response", "id": msg["id"], "status": 200,
                                      "headers": {"content-type": "application/json"},
                                      "body": __import__("base64").b64encode(body).decode()}))
            await asyncio.get_running_loop().run_in_executor(None, done.wait, 10)

    t = threading.Thread(target=lambda: asyncio.run(fake_worker()), daemon=True)
    t.start()
    assert ready.wait(10)
    time.sleep(0.3)
    w = httpx.get(url + "/api/workers").json()
    assert w["effective"] == "laptop" and w["workers"][0]["online"]
    r = httpx.post(url + "/api/ask", json={"section_id": "x", "selection": "y"}, timeout=20)
    assert r.status_code == 200 and r.json()["answer_md"] == "from the laptop" and r.headers["x-axiom-machine"] == "laptop"
    done.set()
    t.join(10)
    time.sleep(0.5)
    w = httpx.get(url + "/api/workers").json()
    assert w["effective"] is None and not w["workers"][0]["online"]  # disconnected → offline, nothing hangs


def test_upload_goes_to_the_inbox_until_a_machine_imports_it(server):
    import pymupdf
    d = pymupdf.open()
    d.new_page().insert_text((72, 72), "hello")
    r = httpx.post(server["url"] + "/api/documents", files={"file": ("notes.pdf", d.tobytes(), "application/pdf")})
    assert r.status_code == 202 and r.json()["queued"]
    imports = httpx.get(server["url"] + "/api/imports").json()
    assert imports[-1]["filename"] == "notes.pdf" and imports[-1]["status"] == "pending"
    assert any(p.name.endswith("__notes.pdf") for p in (server["data"] / "inbox").iterdir())


def test_jobs_only_run_on_the_chosen_machine_with_the_needed_model(remote, monkeypatch):
    from axiom.orchestration import jobs, models
    from axiom.storage import db
    with db.connect() as c:
        c.execute("DELETE FROM jobs")
        c.execute("INSERT INTO jobs(id,doc_id,kind,target_id,status,depends_on,profile,model,created_at,updated_at)"
                  " VALUES ('j1','d','transcribe','s','pending','[]','economy','claude-code:haiku','a','a')")
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('compute.selected', 'pc')")
    monkeypatch.setattr(models, "available", lambda mid: mid == "claude-code:haiku")
    assert jobs._claim(("transcribe",)) is None  # another machine is chosen
    with db.connect() as c:
        c.execute("UPDATE meta SET value='auto' WHERE key='compute.selected'")
    monkeypatch.setattr(models, "available", lambda mid: mid == "local")
    assert jobs._claim(("transcribe",)) is None  # this machine lacks Claude Haiku: the job waits
    monkeypatch.setattr(models, "available", lambda mid: mid == "claude-code:haiku")
    got = jobs._claim(("transcribe",))
    assert got["id"] == "j1" and db.q1("SELECT worker FROM jobs WHERE id='j1'")["worker"] == "me"
