"""SQLite persistence. One connection per thread; WAL mode so the reader stays fast while workers write."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, filename TEXT NOT NULL, sha256 TEXT NOT NULL,
  page_count INTEGER NOT NULL, created_at TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS chapters (
  id TEXT PRIMARY KEY, doc_id TEXT NOT NULL REFERENCES documents(id), number INTEGER NOT NULL,
  title TEXT NOT NULL, page_start INTEGER NOT NULL, page_end INTEGER NOT NULL,
  summary_md TEXT
);

CREATE TABLE IF NOT EXISTS sections (
  id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, chapter_id TEXT NOT NULL REFERENCES chapters(id),
  ord INTEGER NOT NULL, number TEXT NOT NULL, title TEXT NOT NULL,
  page_start INTEGER NOT NULL, page_end INTEGER NOT NULL, is_exercises INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending', summary_md TEXT, stage TEXT NOT NULL DEFAULT 'none'
);

-- Immutable-ish source blocks (re-extraction replaces them only if their content hash changes)
CREATE TABLE IF NOT EXISTS blocks (
  id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, section_id TEXT NOT NULL, ord INTEGER NOT NULL,
  page INTEGER NOT NULL, x0 REAL, y0 REAL, x1 REAL, y1 REAL,
  block_type TEXT NOT NULL, raw_text TEXT NOT NULL, math_score REAL NOT NULL DEFAULT 0,
  eq_number TEXT, content_hash TEXT NOT NULL, continues_prev INTEGER NOT NULL DEFAULT 0,
  transcribed_md TEXT, transcription_method TEXT NOT NULL DEFAULT 'none',
  confidence REAL NOT NULL DEFAULT 0, flags TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS blocks_section ON blocks(section_id, ord);

CREATE TABLE IF NOT EXISTS units (
  id TEXT PRIMARY KEY, section_id TEXT NOT NULL, ord INTEGER NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS units_section ON units(section_id, ord);

CREATE TABLE IF NOT EXISTS coverage (
  block_id TEXT NOT NULL, section_id TEXT NOT NULL, status TEXT NOT NULL, unit_ids TEXT NOT NULL,
  note TEXT, PRIMARY KEY (block_id)
);

CREATE TABLE IF NOT EXISTS notation (
  id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id TEXT NOT NULL, section_id TEXT NOT NULL,
  symbol TEXT NOT NULL, meaning TEXT NOT NULL, convention TEXT
);
CREATE INDEX IF NOT EXISTS notation_doc ON notation(doc_id);

CREATE TABLE IF NOT EXISTS concepts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id TEXT NOT NULL, section_id TEXT NOT NULL,
  name TEXT NOT NULL, kind TEXT NOT NULL, summary TEXT, prerequisites TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS exercises (
  id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, chapter_number INTEGER NOT NULL, section_id TEXT NOT NULL,
  number TEXT NOT NULL, statement_md TEXT NOT NULL, page INTEGER NOT NULL, block_ids TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_cache (
  key TEXT PRIMARY KEY, task TEXT NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
  response TEXT NOT NULL, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id TEXT, task TEXT NOT NULL, model TEXT NOT NULL,
  model_class TEXT NOT NULL, cached INTEGER NOT NULL, ok INTEGER NOT NULL, seconds REAL NOT NULL,
  prompt_tokens INTEGER, completion_tokens INTEGER, ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, kind TEXT NOT NULL, target_id TEXT NOT NULL,
  status TEXT NOT NULL, depends_on TEXT NOT NULL DEFAULT '[]', attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3, profile TEXT NOT NULL DEFAULT 'economy',
  error TEXT, progress TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);

-- learner-side data (local only)
CREATE TABLE IF NOT EXISTS solutions (
  exercise_id TEXT PRIMARY KEY, data TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS answer_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, exercise_id TEXT NOT NULL, answer TEXT NOT NULL, data TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quizzes (
  id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, scope TEXT NOT NULL, scope_id TEXT NOT NULL, data TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quiz_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, quiz_id TEXT NOT NULL, answers TEXT NOT NULL, result TEXT NOT NULL,
  score REAL NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS visualizations (
  unit_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workers (
  name TEXT PRIMARY KEY, info TEXT, models TEXT, last_seen TEXT, online INTEGER NOT NULL DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
  block_id UNINDEXED, section_id UNINDEXED, doc_id UNINDEXED, text, tokenize='porter unicode61'
);
"""

_local = threading.local()
_db_path: Path | None = None


def init(path: Path) -> None:
    global _db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    _db_path = path
    with connect() as c:
        c.executescript(SCHEMA)
        _migrate(c)
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))


_COLUMNS = {  # columns added after schema v1 (CREATE TABLE IF NOT EXISTS does not add them to old databases)
    "sections": [("digest_md", "TEXT")],
    "jobs": [("model", "TEXT"), ("worker", "TEXT")],
}


def _migrate(c: sqlite3.Connection) -> None:
    for table, cols in _COLUMNS.items():
        have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols:
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


# ---------------------------------------------------------------- remote mode (worker → server)
# On a worker machine the database lives on the server. The same q()/q1()/connect() API then runs each statement
# on the server's SQLite over authenticated HTTP (see api/internal.py); a `with connect()` block is a real
# server-side transaction (BEGIN IMMEDIATE), so job claiming stays atomic across machines.
_remote = None


class RemoteRow(Mapping):
    """Behaves like sqlite3.Row: r["col"], r[0], keys(), dict(r)."""
    __slots__ = ("_cols", "_vals")

    def __init__(self, cols: list[str], vals: list[Any]):
        self._cols, self._vals = cols, vals

    def __getitem__(self, k):
        if isinstance(k, int):
            return self._vals[k]
        try:
            return self._vals[self._cols.index(k)]
        except ValueError:
            raise KeyError(k) from None

    def __iter__(self):
        return iter(self._cols)

    def __len__(self):
        return len(self._cols)

    def keys(self):
        return list(self._cols)


def _enc(v: Any) -> Any:
    return {"$b64": base64.b64encode(v).decode()} if isinstance(v, (bytes, bytearray, memoryview)) else v


def _dec(v: Any) -> Any:
    return base64.b64decode(v["$b64"]) if isinstance(v, dict) and "$b64" in v else v


class RemoteCursor:
    def __init__(self, d: dict):
        cols = d.get("cols") or []
        self._rows = [RemoteRow(cols, [_dec(x) for x in r]) for r in d.get("rows") or []]
        self.rowcount = d.get("rowcount", -1)
        self.lastrowid = d.get("lastrowid")

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def __iter__(self):
        return iter(self.fetchall())


class RemoteConn:
    """Connection-like object for one server-side transaction (or autocommit when tx is None)."""

    def __init__(self, client, tx: str | None):
        self._client, self._tx = client, tx

    def execute(self, sql: str, args: tuple | list = ()) -> RemoteCursor:
        return RemoteCursor(self._client.run(self._tx, sql, [_enc(a) for a in args]))

    def commit(self) -> None:  # autocommit object: every statement is already committed
        pass


class RemoteDB:
    def __init__(self, base_url: str, token: str, client=None):
        import httpx
        self.base = base_url.rstrip("/")
        self._http = client or httpx.Client(timeout=120, headers={"Authorization": f"Bearer {token}"})
        self._tl = threading.local()

    def _post(self, path: str, body: dict) -> dict:
        r = self._http.post(self.base + path, json=body)
        if r.status_code >= 400:
            raise sqlite3.OperationalError(f"remote db {path}: {r.status_code} {r.text[:300]}")
        return r.json()

    def run(self, tx: str | None, sql: str, args: list) -> dict:
        return self._post("/internal/db/run", {"tx": tx, "sql": sql, "params": args})

    @contextmanager
    def transaction(self) -> Iterator[RemoteConn]:
        outer = getattr(self._tl, "tx", None)
        if outer:  # nested `with connect()` joins the outer transaction, like a single sqlite connection would
            yield RemoteConn(self, outer)
            return
        tx = self._post("/internal/db/begin", {})["tx"]
        self._tl.tx = tx
        try:
            yield RemoteConn(self, tx)
            self._post("/internal/db/end", {"tx": tx, "commit": True})
        except BaseException:
            try:
                self._post("/internal/db/end", {"tx": tx, "commit": False})
            finally:
                pass
            raise
        finally:
            self._tl.tx = None


def init_remote(base_url: str, token: str, client=None) -> None:
    """Worker mode: every database call goes to the server; the schema is the server's business."""
    global _remote
    _remote = RemoteDB(base_url, token, client)


def is_remote() -> bool:
    return _remote is not None


def conn() -> sqlite3.Connection:
    if _remote is not None:
        tx = getattr(_remote._tl, "tx", None)
        return RemoteConn(_remote, tx)  # type: ignore[return-value]
    if _db_path is None:
        raise RuntimeError("storage.db.init() was not called")
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != _db_path:
        c = sqlite3.connect(_db_path, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn, _local.path = c, _db_path
    return c


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    if _remote is not None:
        with _remote.transaction() as rc:
            yield rc  # type: ignore[misc]
        return
    c = conn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise


def q(sql: str, args: tuple | list = ()) -> list[sqlite3.Row]:
    return conn().execute(sql, args).fetchall()  # remote mode: RemoteRow objects with the same interface


def q1(sql: str, args: tuple | list = ()) -> sqlite3.Row | None:
    return conn().execute(sql, args).fetchone()


def jdump(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))
