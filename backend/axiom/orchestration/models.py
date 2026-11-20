"""Which model does which job.

Model ids
  local                  the local model (llama.cpp, vision) — default for everything token-heavy
  claude-code:<alias>    Claude Code CLI on the user's Claude subscription (sonnet, opus, haiku, fable, ...)
  codex:<model|default>  Codex CLI on the user's ChatGPT subscription
  api:anthropic          Anthropic API (only if ANTHROPIC_API_KEY is set; billed per token)
  api:openai             OpenAI API (only if OPENAI_API_KEY is set; billed per token)

Task groups and the "auto" policy
  * token-heavy groups (transcription, optimization, digests, faithfulness checks) run locally;
  * short tasks that need reasoning (questions, hints, solutions, answer checks, quizzes, visualizations) prefer a
    premium model — the user's subscription agents first — and fall back to the local model when none is
    available, the daily cap is reached or the call fails;
  * the "economy" profile keeps "auto" local everywhere; "max" also sends faithfulness checks to premium.
The user can override every group in Settings (stored in the `meta` table) or per request in the UI; an explicit
per-request choice is honoured as-is (no silent fallback: an error is shown instead).
"""

from __future__ import annotations

import contextvars
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from axiom.storage import db

ROUTING_KEY = "routing.v1"

# model chosen for the current processing run (set by the job runner); applies to the token-heavy groups
processing_model: contextvars.ContextVar[str | None] = contextvars.ContextVar("processing_model", default=None)


@dataclass(frozen=True)
class Group:
    id: str
    label: str
    tasks: tuple[str, ...]
    heavy: bool
    vision: bool = False
    help: str = ""


GROUPS: tuple[Group, ...] = (
    Group("transcribe", "Transcribing the scan", ("transcribe",), heavy=True, vision=True,
          help="Thousands of image calls per book; needs a vision model."),
    Group("optimize", "Rewriting sections (3 levels)", ("optimize",), heavy=True,
          help="Long prompts for every section window."),
    Group("digest", "Section summaries", ("digest",), heavy=True),
    Group("faithfulness", "Faithfulness checks", ("faithfulness",), heavy=True,
          help="One reasoning call per ~10 rewritten passages."),
    Group("ask", "Ask AI about a selection", ("ask",), heavy=False),
    Group("hint", "Exercise hints", ("hint",), heavy=False),
    Group("solve", "Worked solutions", ("solve_a", "solve_b", "solve_compare"), heavy=False,
          help="Two independent solutions + a comparison; the second uses another vendor when available."),
    Group("check", "Checking your answer", ("check_answer",), heavy=False),
    Group("quiz", "Quizzes and exams", ("quiz", "quiz_blind", "quiz_grade"), heavy=False),
    Group("visualize", "Visualizations", ("visualize", "visualize_review"), heavy=False),
)
GROUP_BY_ID = {g.id: g for g in GROUPS}
TASK_GROUP = {t: g.id for g in GROUPS for t in g.tasks}

# preference order for "auto premium": subscriptions first (no per-token cost), then paid APIs
PREMIUM_ORDER = ("claude-code:sonnet", "codex:default", "claude-code:opus", "api:anthropic", "api:openai")


def _claude_models() -> list[str]:
    return [m.strip() for m in os.environ.get("AXIOM_CLAUDE_MODELS", "sonnet,opus,haiku").split(",") if m.strip()]


def _codex_models() -> list[str]:
    return [m.strip() for m in os.environ.get("AXIOM_CODEX_MODELS", "default").split(",") if m.strip()]


def daily_limit(task: str | None = None) -> int:
    """Calls per subscription model per day. Bulk processing (one chapter ≈ 700–800 calls) has its own cap."""
    if task and TASK_GROUP.get(task) and GROUP_BY_ID[TASK_GROUP[task]].heavy:
        return int(os.environ.get("AXIOM_SUBSCRIPTION_DAILY_LIMIT_BULK", "4000"))
    return int(os.environ.get("AXIOM_SUBSCRIPTION_DAILY_LIMIT", "300"))


def catalog() -> list[dict]:
    """All models AXIOM knows how to use, with their kind; availability is checked separately."""
    out = [{"id": "local", "label": "Local model (Qwen, llama.cpp)", "kind": "local", "vision": True}]
    names = {"sonnet": "Sonnet", "opus": "Opus", "haiku": "Haiku", "fable": "Fable"}
    out += [{"id": f"claude-code:{m}", "label": f"Claude Code · {names.get(m, m)}", "kind": "subscription",
             "vision": True} for m in _claude_models()]
    out += [{"id": f"codex:{m}", "label": "Codex" + ("" if m == "default" else f" · {m}"), "kind": "subscription",
             "vision": False} for m in _codex_models()]
    if os.environ.get("ANTHROPIC_API_KEY"):
        out.append({"id": "api:anthropic", "label": "Anthropic API (per-token billing)", "kind": "api", "vision": True})
    if os.environ.get("OPENAI_API_KEY"):
        out.append({"id": "api:openai", "label": "OpenAI API (per-token billing)", "kind": "api", "vision": True})
    return out


_avail_cache: dict[str, tuple[float, bool]] = {}
_avail_lock = threading.Lock()


def available(model_id: str) -> bool:
    """Cheap availability check (binary installed / key present / local server up), cached for 60 s."""
    now = time.monotonic()
    with _avail_lock:
        hit = _avail_cache.get(model_id)
        if hit and now - hit[0] < 60:
            return hit[1]
    if model_id == "local":
        from axiom.orchestration.llm import get_adapter
        ok = bool(getattr(get_adapter("local_text"), "health", lambda: True)())
    elif model_id.startswith("claude-code:"):
        ok = shutil.which("claude") is not None and model_id.split(":", 1)[1] in _claude_models()
    elif model_id.startswith("codex:"):
        ok = shutil.which("codex") is not None and model_id.split(":", 1)[1] in _codex_models()
    elif model_id == "api:anthropic":
        ok = bool(os.environ.get("ANTHROPIC_API_KEY"))
    elif model_id == "api:openai":
        ok = bool(os.environ.get("OPENAI_API_KEY"))
    else:
        ok = False
    with _avail_lock:
        _avail_cache[model_id] = (now, ok)
    return ok


def kind(model_id: str) -> str:
    if model_id in ("local", "local_text", "local_vlm"):
        return "local"
    return "api" if model_id.startswith("api:") else "subscription"


def calls_today(model_id: str) -> int:
    from axiom.orchestration.llm import model_name
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    row = db.q1("SELECT COUNT(*) n FROM llm_calls WHERE model=? AND cached=0 AND ts>=?", (model_name(model_id), start))
    return int(row["n"]) if row else 0


# ---------------------------------------------------------------- user routing settings
def load_routing() -> dict:
    row = db.q1("SELECT value FROM meta WHERE key=?", (ROUTING_KEY,))
    d = json.loads(row["value"]) if row and row["value"] else {}
    return {"groups": {g.id: d.get("groups", {}).get(g.id, "auto") for g in GROUPS},
            "premium_default": d.get("premium_default", "auto"),
            # "auto" may send passages to cloud models only after the user switched this on (privacy by default);
            # a model the user picks explicitly (here per group, or per request) is always honoured
            "cloud_enabled": bool(d.get("cloud_enabled", False))}


def save_routing(groups: dict[str, str] | None = None, premium_default: str | None = None,
                 cloud_enabled: bool | None = None) -> dict:
    cur = load_routing()
    ids = {m["id"] for m in catalog()} | {"auto"}
    for gid, mid in (groups or {}).items():
        if gid not in GROUP_BY_ID:
            raise ValueError(f"unknown task group {gid!r}")
        if mid not in ids:
            raise ValueError(f"unknown model {mid!r}")
        if GROUP_BY_ID[gid].vision and mid != "auto" and not next(m for m in catalog() if m["id"] == mid)["vision"]:
            raise ValueError(f"{mid} cannot read images; '{GROUP_BY_ID[gid].label}' needs a vision model")
        cur["groups"][gid] = mid
    if premium_default is not None:
        if premium_default not in ids or premium_default == "local":
            raise ValueError(f"unknown premium model {premium_default!r}")
        cur["premium_default"] = premium_default
    if cloud_enabled is not None:
        cur["cloud_enabled"] = bool(cloud_enabled)
    with db.connect() as c:
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (ROUTING_KEY, json.dumps(cur)))
    return cur


# ---------------------------------------------------------------- resolution
@dataclass(frozen=True)
class Route:
    model: str                 # model id to try first
    fallback: str | None       # model id to use if the first is unavailable / over its cap / fails to answer
    reason: str                # human-readable: why this model


def best_premium(exclude: tuple[str, ...] = ()) -> str | None:
    pref = load_routing()["premium_default"]
    order = ((pref,) if pref != "auto" else ()) + PREMIUM_ORDER
    for mid in order:
        if mid not in exclude and mid in {m["id"] for m in catalog()} and available(mid):
            return mid
    return None


def second_opinion(first: str) -> str:
    """A premium model from a *different vendor* than `first` (independent solutions), else `first` itself."""
    vendor = first.split(":", 1)[0]
    for mid in PREMIUM_ORDER:
        if mid.split(":", 1)[0] != vendor and mid in {m["id"] for m in catalog()} and available(mid):
            return mid
    return first


def resolve(group: str, override: str | None = None, profile: str = "balanced", local_as: str = "local") -> Route:
    r = _resolve(group, override, profile)
    if local_as != "local":  # e.g. "local_vlm" so metering keeps telling vision and text calls apart
        r = Route(local_as if r.model == "local" else r.model, local_as if r.fallback == "local" else r.fallback,
                  r.reason)
    return r


def _resolve(group: str, override: str | None, profile: str) -> Route:
    g = GROUP_BY_ID[group]
    if override and override != "auto":
        return Route(override, None, "chosen for this request")
    run = processing_model.get()
    if g.heavy and run and run != "auto":
        vision_ok = not g.vision or next((m["vision"] for m in catalog() if m["id"] == run), False)
        if vision_ok:  # a long run should finish: fall back to the local model if the subscription runs out
            return Route(run, None if run == "local" else "local", "chosen for this processing run")
    choice = load_routing()["groups"].get(group, "auto")
    if choice != "auto":
        return Route(choice, None if choice == "local" else "local", "set in Settings")
    cloud = load_routing()["cloud_enabled"]
    wants_premium = cloud and ((not g.heavy and profile != "economy") or (group == "faithfulness" and profile == "max"))
    if wants_premium and not g.vision:
        p = best_premium()
        if p:
            return Route(p, "local", "auto: short reasoning task → premium")
        return Route("local", None, "auto: no premium model available")
    if not cloud and not g.heavy:
        return Route("local", None, "auto: cloud models are off (Settings)")
    return Route("local", None, "auto: token-heavy task → local")


def report() -> dict:
    """This machine's models with availability (installed, signed in, server up) and today's usage, plus what
    "auto" resolves to for every task group. Sent by workers to the server when they connect."""
    from axiom.model_adapters.cli_agents import ClaudeCodeAdapter, CodexAdapter
    status = {"claude-code": ClaudeCodeAdapter().login_status(), "codex": CodexAdapter().login_status()}
    out = []
    for m in catalog():
        st = status.get(m["id"].split(":", 1)[0], {})
        usable = available(m["id"]) and st.get("logged_in", True)
        sub = m["kind"] == "subscription"
        out.append(dict(m, available=usable, detail=st.get("detail") or ("" if usable else "not reachable"),
                        calls_today=calls_today(m["id"]) if sub else None, daily_limit=daily_limit() if sub else None))
    return {"models": out, "groups": [{"id": g.id, "label": g.label, "heavy": g.heavy, "vision": g.vision,
                                       "help": g.help, "auto": resolve(g.id).model} for g in GROUPS]}
