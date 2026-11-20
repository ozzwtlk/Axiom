"""`axiom worker`: turn this PC or laptop into a computing machine for an AXIOM server (e.g. a Raspberry Pi).

The worker
  * uses the server's database and files (db/files remote mode) — nothing is stored here except a cache;
  * runs processing jobs from the server's queue, but only those it may run (it is the chosen machine, or the
    choice is "auto") and can run (it has the model the job needs);
  * keeps a WebSocket open to the server, through which the server forwards AI requests from the reader
    (questions, hints, solutions, quizzes, visualizations, page renders) to be executed here;
  * reports its models (local model up? Claude Code / Codex signed in?) on connect and every minute.
The connection goes out from the worker, so it works behind a laptop firewall; it reconnects automatically.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import platform
import socket

log = logging.getLogger("axiom.worker")

STATUS_EVERY_S = 60


def _ws_url(server: str) -> str:
    s = server.rstrip("/")
    return ("wss://" + s[8:] if s.startswith("https://") else "ws://" + s.split("://", 1)[-1]) + "/internal/worker"


async def _handle(msg: dict, client, ws, send_lock: asyncio.Lock) -> None:
    try:
        url = msg["path"] + (("?" + msg["query"]) if msg.get("query") else "")
        r = await client.request(msg["method"], url, headers=msg.get("headers") or {},
                                 content=base64.b64decode(msg.get("body") or ""))
        reply = {"type": "response", "id": msg["id"], "status": r.status_code, "headers": dict(r.headers),
                 "body": base64.b64encode(r.content).decode()}
    except Exception as e:  # never leave the server waiting
        log.exception("request %s %s failed", msg.get("method"), msg.get("path"))
        body = json.dumps({"detail": f"worker error: {type(e).__name__}: {e}"}).encode()
        reply = {"type": "response", "id": msg["id"], "status": 500, "headers": {"content-type": "application/json"},
                 "body": base64.b64encode(body).decode()}
    async with send_lock:
        await ws.send(json.dumps(reply))


async def _session(server: str, token: str, name: str, client) -> None:
    import websockets
    from axiom.orchestration import models

    info = {"hostname": socket.gethostname(), "platform": platform.platform(terse=True)}
    async with websockets.connect(_ws_url(server), additional_headers={"Authorization": f"Bearer {token}"},
                                  max_size=None, ping_interval=20, ping_timeout=60) as ws:
        send_lock = asyncio.Lock()
        models._avail_cache.clear()
        report = await asyncio.to_thread(models.report)
        await ws.send(json.dumps({"type": "hello", "name": name, "info": info, "models": report}))
        ok = [m["id"] for m in report["models"] if m["available"]]
        log.info("connected to %s as %r — models: %s", server, name, ", ".join(ok) or "none")

        async def status_loop():
            while True:
                await asyncio.sleep(STATUS_EVERY_S)
                models._avail_cache.clear()
                rep = await asyncio.to_thread(models.report)
                async with send_lock:
                    await ws.send(json.dumps({"type": "status", "models": rep}))

        tasks: set[asyncio.Task] = set()
        status = asyncio.create_task(status_loop())
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "request":
                    t = asyncio.create_task(_handle(msg, client, ws, send_lock))
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
        finally:
            status.cancel()
            for t in tasks:
                t.cancel()


async def _main(server: str, token: str, name: str, run_jobs: bool) -> None:
    import httpx

    from axiom.api.app import create_app
    from axiom.orchestration import jobs
    from axiom.storage import db, files

    db.init_remote(server, token)
    files.init_remote(server, token)
    jobs.WORKER_NAME = name
    db.q1("SELECT 1")  # fail fast on a wrong URL or token
    app = create_app(start_worker=False)
    if run_jobs:
        jobs.start_workers()
    delay = 2.0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker",
                                 timeout=None) as client:
        while True:
            try:
                await _session(server, token, name, client)
                delay = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # server restarting, network gone, ...
                log.warning("connection to %s lost (%s); retrying in %.0fs", server, e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)


def run(server: str, token: str, name: str | None = None, run_jobs: bool = True) -> None:
    os.environ["AXIOM_ROLE"] = "worker"
    name = name or os.environ.get("AXIOM_WORKER_NAME") or socket.gethostname()
    os.environ["AXIOM_WORKER_NAME"] = name
    if not token or len(token) < 16:
        raise SystemExit("AXIOM_TOKEN must be set (≥ 16 characters, the same as on the server)")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per remote SQL statement otherwise
    try:
        asyncio.run(_main(server, token, name, run_jobs))
    except KeyboardInterrupt:
        pass
    finally:
        from axiom.orchestration import jobs
        jobs.stop_workers()
