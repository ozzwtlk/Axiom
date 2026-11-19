import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CARROLL = next(REPO.glob("Carroll*.pdf"), None)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Fresh isolated database + data dir for each test."""
    from axiom import config
    from axiom.storage import db

    monkeypatch.setenv("AXIOM_DATA_DIR", str(tmp_path))
    s = config.Settings()
    monkeypatch.setattr(config, "settings", s)
    import sys
    for mod in ("axiom.ingestion.pdf", "axiom.extraction.extract", "axiom.api.app"):
        __import__(mod, fromlist=["settings"])
    # every module that bound `settings` at import time must see the temp settings (else tests touch real data/)
    for name, m in list(sys.modules.items()):
        if name.startswith("axiom") and getattr(m, "settings", None) is not None and m is not config:
            monkeypatch.setattr(m, "settings", s)
    db.init(s.db_path)
    yield s
    db._local.__dict__.clear()


@pytest.fixture
def carroll():
    if not CARROLL or os.environ.get("AXIOM_SKIP_PDF"):
        pytest.skip("Carroll PDF not available")
    return CARROLL


@pytest.fixture(autouse=True)
def no_real_external_models(monkeypatch):
    """Tests never reach a real model: only the local model (faked per test) and models a test installs count as
    available, and the per-model availability cache starts empty."""
    from axiom.orchestration import llm, models
    monkeypatch.setattr(models, "_avail_cache", {})
    monkeypatch.setattr(models, "available", lambda mid: mid == "local" or mid in llm._adapters)
    yield
