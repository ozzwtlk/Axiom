"""Runtime configuration. Everything overridable by environment variables (AXIOM_*)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class ProfileConfig:
    name: str
    vlm_thinking: bool
    optimize_thinking: bool
    premium_enabled: bool
    max_premium_calls: int


PROFILES: dict[str, ProfileConfig] = {
    "economy": ProfileConfig("economy", False, False, premium_enabled=False, max_premium_calls=0),
    "balanced": ProfileConfig("balanced", False, False, premium_enabled=True, max_premium_calls=40),
    "max": ProfileConfig("max", False, True, premium_enabled=True, max_premium_calls=400),
}


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("AXIOM_DATA_DIR", str(REPO_ROOT / "data"))))
    local_llm_url: str = field(default_factory=lambda: _env("AXIOM_LOCAL_LLM_URL", "http://127.0.0.1:8081/v1"))
    local_model_name: str = field(default_factory=lambda: _env("AXIOM_LOCAL_MODEL", "qwen3.6-35b-a3b-q4kxl"))
    local_concurrency: int = field(default_factory=lambda: int(_env("AXIOM_LOCAL_CONCURRENCY", "2")))
    premium_provider: str = field(default_factory=lambda: _env("AXIOM_PREMIUM_PROVIDER", "anthropic"))
    premium_model: str = field(default_factory=lambda: _env("AXIOM_PREMIUM_MODEL", "claude-sonnet-5-5"))
    premium_concurrency: int = field(default_factory=lambda: int(_env("AXIOM_PREMIUM_CONCURRENCY", "1")))
    subscription_concurrency: int = field(default_factory=lambda: int(_env("AXIOM_SUBSCRIPTION_CONCURRENCY", "2")))
    render_dpi: int = field(default_factory=lambda: int(_env("AXIOM_RENDER_DPI", "200")))
    llm_timeout_s: float = field(default_factory=lambda: float(_env("AXIOM_LLM_TIMEOUT", "1800")))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "axiom.db"

    def doc_dir(self, doc_id: str) -> Path:
        return self.data_dir / "docs" / doc_id


settings = Settings()
