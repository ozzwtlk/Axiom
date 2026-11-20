"""Files shared between the server and workers.

All-in-one and server roles read and write `settings.data_dir` directly. A worker keeps only a local cache in its
own data dir: the original PDFs are fetched from the server on first use, and files the worker creates that the
server must own (an imported PDF) are uploaded. Rendered images never need uploading: the server stores the
bodies of forwarded image requests itself (api/internal.py).
"""

from __future__ import annotations

from pathlib import Path

from axiom.config import settings

_remote: tuple[str, str] | None = None  # (server base url, token) in worker mode


def init_remote(base_url: str, token: str) -> None:
    global _remote
    _remote = (base_url.rstrip("/"), token)


def _client():
    import httpx
    return httpx.Client(timeout=600, headers={"Authorization": f"Bearer {_remote[1]}"})


def fetch(rel: str) -> Path:
    """Local path of a shared file, downloading it from the server first when running as a worker."""
    p = settings.data_dir / rel
    if _remote and not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        with _client() as c, c.stream("GET", f"{_remote[0]}/internal/files/{rel}") as r:
            if r.status_code == 404:
                raise FileNotFoundError(f"{rel} is not on the server")
            r.raise_for_status()
            tmp = p.with_suffix(p.suffix + ".part")
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
            tmp.replace(p)
    return p


def publish(rel: str) -> None:
    """Upload a file the worker created to the same place on the server (no-op unless running as a worker)."""
    if _remote:
        with _client() as c:
            c.put(f"{_remote[0]}/internal/files/{rel}", content=(settings.data_dir / rel).read_bytes()).raise_for_status()


def remove(rel: str) -> None:
    if _remote:
        with _client() as c:
            c.delete(f"{_remote[0]}/internal/files/{rel}").raise_for_status()
    else:
        (settings.data_dir / rel).unlink(missing_ok=True)
