"""Server ⇄ worker plumbing (role "server", e.g. the Raspberry Pi in Docker).

The server stores everything and serves the reader, but computes nothing: no model calls, no PDF parsing, no
rendering. Compute happens on *workers* (`axiom worker` on a PC or laptop) that connect OUT to the server:

  /internal/worker      WebSocket. The worker says hello with its name and the models it can use; the server sends
                        it HTTP requests to execute (an AI question, a hint, a page render, ...) and gets the
                        responses back. Works through the laptop's firewall / NAT because the worker dials out.
  /internal/db/*        Remote SQL: workers read and write the server's SQLite directly (server-side transactions,
                        BEGIN IMMEDIATE), so all data lives on the server and nothing needs syncing.
  /internal/files/*     Download the original PDFs, upload imported PDFs.
Everything under /internal requires `Authorization: Bearer $AXIOM_TOKEN`.

Endpoints that need compute call `require_compute()` first. On the server that raises NeedsCompute, and the
middleware forwards the request to the chosen worker (Settings → computing machine; "auto" prefers a machine
with a local model). Cacheable results (page images, scan crops) are stored on the server, so they keep working
when every PC is off.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

from axiom.config import settings
from axiom.storage import db

log = logging.getLogger("axiom.server")

SELECTED_KEY = "compute.selected"
CACHEABLE = ("/image",)  # GET paths whose computed bodies are stored on the server
PROXY_TIMEOUT = float(os.environ.get("AXIOM_PROXY_TIMEOUT", "3600"))


def role() -> str:
    return os.environ.get("AXIOM_ROLE", "all")


class NeedsCompute(Exception):
    pass


def require_compute() -> None:
    """Call at the top of every endpoint that runs a model, parses a PDF or renders an image."""
    if role() == "server":
        raise NeedsCompute()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- auth
def _token() -> str:
    return os.environ.get("AXIOM_TOKEN", "")


def check_token(authorization: str = Header(default="")) -> None:
    tok = _token()
    if not tok or len(tok) < 16:
        raise HTTPException(503, "server has no AXIOM_TOKEN (≥ 16 chars) configured; workers are disabled")
    if not hmac.compare_digest(authorization.encode(), f"Bearer {tok}".encode()):
        raise HTTPException(401, "bad worker token")


# ---------------------------------------------------------------- remote SQL with server-held transactions
class _Tx:
    def __init__(self):
        self.conn = sqlite3.connect(settings.db_path, timeout=60, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = None
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("BEGIN IMMEDIATE")
        self.lock = threading.Lock()
        self.used = time.monotonic()


_txs: dict[str, _Tx] = {}
_txs_lock = threading.Lock()
TX_IDLE_S = 60


def _reap(idle: float = TX_IDLE_S) -> None:
    """Roll back worker transactions nobody has touched for `idle` s. A transaction held by a worker that died
    keeps SQLite's write lock, which blocks *every* write on the server until it is rolled back."""
    now = time.monotonic()
    with _txs_lock:
        dead = [k for k, t in _txs.items() if now - t.used > idle]
        for k in dead:
            t = _txs.pop(k)
            try:
                t.conn.rollback()
                t.conn.close()
            except sqlite3.Error:
                pass
    if dead:
        log.warning("rolled back %d abandoned worker transaction(s)", len(dead))


def _result(cur: sqlite3.Cursor) -> dict:
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = [[db._enc(v) for v in r] for r in cur.fetchall()] if cols else []
    return {"cols": cols, "rows": rows, "rowcount": cur.rowcount, "lastrowid": cur.lastrowid}


# ---------------------------------------------------------------- worker registry
class WorkerConn:
    def __init__(self, name: str, ws: WebSocket, info: dict, models: dict):
        self.name, self.ws, self.info, self.models = name, ws, info, models
        self.connected_at = time.time()
        self.pending: dict[str, asyncio.Future] = {}
        self.send_lock = asyncio.Lock()

    def has_local(self) -> bool:
        return any(m.get("kind") == "local" and m.get("available") for m in self.models.get("models", []))

    def has_model(self, mid: str) -> bool:
        return any(m.get("id") == mid and m.get("available") for m in self.models.get("models", []))


class Registry:
    def __init__(self):
        self.workers: dict[str, WorkerConn] = {}

    def selected(self) -> str:
        row = db.q1("SELECT value FROM meta WHERE key=?", (SELECTED_KEY,))
        return row["value"] if row and row["value"] else "auto"

    def effective(self) -> WorkerConn | None:
        sel = self.selected()
        if sel != "auto":
            return self.workers.get(sel)
        online = sorted(self.workers.values(), key=lambda w: (not w.has_local(), -w.connected_at))
        return online[0] if online else None

    async def call(self, w: WorkerConn, method: str, path: str, query: str, headers: dict, body: bytes) -> Response:
        rid = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        w.pending[rid] = fut
        try:
            async with w.send_lock:
                await w.ws.send_text(json.dumps({"type": "request", "id": rid, "method": method, "path": path,
                                                 "query": query, "headers": headers,
                                                 "body": base64.b64encode(body).decode()}))
            msg = await asyncio.wait_for(fut, PROXY_TIMEOUT)
        except asyncio.TimeoutError:
            raise HTTPException(504, f"{w.name} did not answer within {PROXY_TIMEOUT:.0f}s") from None
        finally:
            w.pending.pop(rid, None)
        hdrs = {k: v for k, v in (msg.get("headers") or {}).items()
                if k.lower() in ("content-type", "content-disposition", "cache-control", "x-axiom-model")}
        hdrs["x-axiom-machine"] = w.name
        return Response(base64.b64decode(msg.get("body") or ""), status_code=msg.get("status", 502), headers=hdrs)


registry = Registry()


def _save_worker(name: str, info: dict, models: dict, online: bool) -> None:
    with db.connect() as c:
        c.execute("INSERT INTO workers(name, info, models, last_seen, online) VALUES (?,?,?,?,?) ON CONFLICT(name) DO "
                  "UPDATE SET info=excluded.info, models=excluded.models, last_seen=excluded.last_seen, "
                  "online=excluded.online", (name, json.dumps(info), json.dumps(models), _now(), int(online)))


def _release_jobs(name: str) -> int:
    """A worker went away: its running jobs go back to the queue (they resume from their checkpoints)."""
    from axiom.orchestration import jobs
    return jobs.recover(worker=name)


# ---------------------------------------------------------------- HTTP cache for computed GETs (images)
def _cache_file(path: str, query: str) -> Path | None:
    parts = path.split("/")
    if len(parts) == 7 and parts[2] == "documents" and parts[4] == "pages":
        row = db.q1("SELECT id AS doc_id FROM documents WHERE id=?", (parts[3],))
    elif len(parts) == 5 and parts[2] == "blocks":
        row = db.q1("SELECT doc_id FROM blocks WHERE id=?", (parts[3],))
    else:
        return None
    if not row:
        return None
    h = hashlib.sha256(f"{path}?{query}".encode()).hexdigest()
    return settings.doc_dir(row["doc_id"]) / "http-cache" / (h + ".bin")


def _cache_image(path: Path, body: bytes) -> None:
    # The book can be removed while a worker renders. Never recreate its directory.
    try:
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(body)
    except FileNotFoundError:
        pass


def _safe_rel(rel: str) -> Path:
    base = settings.data_dir.resolve()
    p = (base / rel).resolve()
    if base not in p.parents or not rel or rel.startswith("/") or ".." in Path(rel).parts:
        raise HTTPException(400, "bad path")
    if p.parts[len(base.parts)] not in ("docs", "inbox", "exports"):
        raise HTTPException(403, "path not shared with workers")
    return p


# ---------------------------------------------------------------- wiring
def _reaper() -> None:
    while True:
        time.sleep(10)
        try:
            _reap()
        except Exception:  # never let the reaper die
            log.exception("transaction reaper failed")


def install(app: FastAPI) -> None:
    if role() == "server" and not getattr(install, "_reaper_started", False):
        install._reaper_started = True
        threading.Thread(target=_reaper, daemon=True, name="tx-reaper").start()
    r = APIRouter(prefix="/internal", dependencies=[Depends(check_token)])

    @r.post("/db/run")
    def db_run(body: dict):
        sql, params, tx = body["sql"], [db._dec(p) for p in body.get("params") or []], body.get("tx")
        if tx:
            with _txs_lock:
                t = _txs.get(tx)
            if not t:
                raise HTTPException(409, "transaction expired")
            with t.lock:
                t.used = time.monotonic()
                return _result(t.conn.execute(sql, params))
        c = db.conn()
        try:
            out = _result(c.execute(sql, params))
            c.commit()
            return out
        except sqlite3.Error as e:
            c.rollback()
            raise HTTPException(400, f"{type(e).__name__}: {e}") from None

    @r.post("/db/begin")
    def db_begin():
        _reap()
        t = _Tx()
        tid = uuid.uuid4().hex
        with _txs_lock:
            _txs[tid] = t
        return {"tx": tid}

    @r.post("/db/end")
    def db_end(body: dict):
        with _txs_lock:
            t = _txs.pop(body["tx"], None)
        if not t:
            raise HTTPException(409, "transaction expired")
        with t.lock:
            (t.conn.commit if body.get("commit") else t.conn.rollback)()
            t.conn.close()
        return {"ok": True}

    @r.get("/files/{rel:path}")
    def get_file(rel: str):
        p = _safe_rel(rel)
        if not p.is_file():
            raise HTTPException(404, "no such file")
        return FileResponse(p)

    @r.put("/files/{rel:path}")
    async def put_file(rel: str, request: Request):
        p = _safe_rel(rel)
        body = await request.body()

        def _write() -> int:  # disk I/O off the event loop (large PDFs on an SD card)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".part")
            tmp.write_bytes(body)
            if p.exists():
                p.chmod(0o644)
            tmp.replace(p)
            return p.stat().st_size
        return {"ok": True, "bytes": await asyncio.to_thread(_write)}

    @r.delete("/files/{rel:path}")
    def delete_file(rel: str):
        p = _safe_rel(rel)
        if p.is_file() and p.parts[len(settings.data_dir.resolve().parts)] == "inbox":
            p.unlink()
        return {"ok": True}

    app.include_router(r)

    @app.websocket("/internal/worker")
    async def worker_socket(ws: WebSocket):
        tok = _token()
        auth = ws.headers.get("authorization", "")
        if not tok or len(tok) < 16 or not hmac.compare_digest(auth.encode(), f"Bearer {tok}".encode()):
            await ws.close(code=4401)
            return
        await ws.accept()
        hello = json.loads(await ws.receive_text())
        name = str(hello.get("name") or "worker")[:64]
        w = WorkerConn(name, ws, hello.get("info") or {}, hello.get("models") or {})
        old = registry.workers.get(name)
        if old:  # same machine reconnecting: drop the stale socket
            for f in old.pending.values():
                f.cancel()
        registry.workers[name] = w
        await asyncio.to_thread(_save_worker, name, w.info, w.models, True)
        log.info("worker %s connected (%s)", name, ", ".join(m["id"] for m in w.models.get("models", [])
                                                             if m.get("available")))
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                if msg.get("type") == "response":
                    f = w.pending.get(msg.get("id"))
                    if f and not f.done():
                        f.set_result(msg)
                elif msg.get("type") == "status":
                    w.models = msg.get("models") or w.models
                    await asyncio.to_thread(_save_worker, name, w.info, w.models, True)
        except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError):
            pass
        finally:
            if registry.workers.get(name) is w:
                del registry.workers[name]
                await asyncio.to_thread(_save_worker, name, w.info, w.models, False)
                n = await asyncio.to_thread(_release_jobs, name)
                log.info("worker %s disconnected (%d running job(s) requeued)", name, n)
            for f in w.pending.values():
                if not f.done():
                    f.set_exception(HTTPException(503, f"{name} disconnected"))

    @app.middleware("http")
    async def forward_compute(request: Request, call_next):
        if role() != "server" or not request.url.path.startswith("/api/"):
            return await call_next(request)
        body = await request.body()  # cached by Starlette, so the local handler can still read it
        cacheable = request.method == "GET" and request.url.path.endswith(CACHEABLE)
        cf = await asyncio.to_thread(_cache_file, request.url.path, request.url.query) if cacheable else None
        if cf:
            try:
                image = await asyncio.to_thread(cf.read_bytes)
            except FileNotFoundError:
                pass
            else:
                return Response(image, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})
        resp = await call_next(request)
        if resp.headers.get("x-axiom-needs-compute") != "1":
            return resp
        w = registry.effective()
        if not w:
            sel = registry.selected()
            msg = (f"The computing machine “{sel}” is not connected." if sel != "auto" else
                   "No computing machine is connected.") + \
                " Start AXIOM on your PC or laptop (./axiom worker), or pick another machine."
            return JSONResponse({"detail": msg, "needs_compute": True}, status_code=503)
        hdrs = {k: v for k, v in request.headers.items() if k.lower() in ("content-type", "accept")}
        out = await registry.call(w, request.method, request.url.path, request.url.query, hdrs, body)
        if cf and out.status_code == 200:
            await asyncio.to_thread(_cache_image, cf, out.body)
        return out

    @app.exception_handler(NeedsCompute)
    async def _needs(request: Request, exc: NeedsCompute):
        return Response(status_code=599, headers={"x-axiom-needs-compute": "1"})

    @app.get("/api/workers")
    def list_workers():
        if role() != "server":
            return {"mode": role(), "selected": "local", "effective": "local", "workers": []}
        live = registry.workers
        eff = registry.effective()
        rows = db.q("SELECT name, info, models, last_seen, online FROM workers ORDER BY last_seen DESC")
        out = []
        for r in rows:
            m = json.loads(r["models"] or "{}")
            out.append({"name": r["name"], "online": r["name"] in live, "last_seen": r["last_seen"],
                        "info": json.loads(r["info"] or "{}"),
                        "models": [{"id": x["id"], "label": x["label"], "kind": x["kind"],
                                    "available": x.get("available", False)} for x in m.get("models", [])]})
        return {"mode": "server", "selected": registry.selected(), "effective": eff.name if eff else None,
                "workers": out}

    @app.put("/api/workers/selected")
    def select_worker(body: dict):
        name = str(body.get("name") or "auto")
        known = {r["name"] for r in db.q("SELECT name FROM workers")}
        if name != "auto" and name not in known:
            raise HTTPException(400, f"unknown machine {name!r}")
        with db.connect() as c:
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (SELECTED_KEY, name))
        return list_workers()

    @app.delete("/api/workers/{name}")
    def forget_worker(name: str):
        if name in registry.workers:
            raise HTTPException(409, "machine is connected; stop it first")
        with db.connect() as c:
            c.execute("DELETE FROM workers WHERE name=?", (name,))
        return list_workers()
