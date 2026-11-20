"""Model routing, caching, metering and evidence-based escalation.

Every model call in AXIOM goes through `call()` / `call_json()`:
  * cache key = sha256(model, prompt_version, task, system, user text, image hashes, schema, thinking)
  * per-class concurrency limits (local GPU slots, premium rate limits)
  * per-document premium budget (profile-dependent); premium is never used unless configured
  * every call (hit or miss) is metered in `llm_calls`
Escalation is triggered by *evidence* (schema/validation failure after retries), not by model
self-reported confidence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from axiom.config import PROFILES, settings
from axiom.model_adapters.base import AdapterUnavailable, ChatAdapter, ChatRequest, ChatResult
from axiom.orchestration import models  # noqa: F401  (type hints)
from axiom.model_adapters.openai_compat import OpenAICompatAdapter
from axiom.storage import db

log = logging.getLogger("axiom.llm")

_adapters: dict[str, ChatAdapter] = {}
_sems: dict[str, threading.Semaphore] = {}
_lock = threading.Lock()


class BudgetExceeded(RuntimeError):
    pass


class ValidationFailed(RuntimeError):
    pass


def premium_available() -> bool:
    """True when any premium model (subscription agent or API key) can be used."""
    from axiom.orchestration import models
    return models.best_premium() is not None


def _canon(model: str) -> str:
    """Model id for an adapter: the local classes share one adapter; "premium" means the best available one."""
    if model in ("local", "local_text", "local_vlm"):
        return "local"
    if model == "premium":
        from axiom.orchestration import models
        best = models.best_premium()
        if not best:
            raise BudgetExceeded("premium model requested but none is available (no subscription CLI or API key)")
        return best
    return model


def _metering_class(model: str, requested: str) -> str:
    """`llm_calls.model_class`: local_text / local_vlm for the local model, premium for everything else."""
    return requested if requested in ("local_text", "local_vlm") else ("local_text" if model == "local" else "premium")


def model_name(model: str) -> str:
    """The name recorded in llm_calls / cache keys for a model id."""
    return get_adapter(model).name


def get_adapter(model: str) -> ChatAdapter:
    mid = _canon(model)
    with _lock:
        if mid in _adapters:
            return _adapters[mid]
        if mid == "local":
            # one local multimodal model serves text and vision tasks
            ad: ChatAdapter = OpenAICompatAdapter(settings.local_llm_url, settings.local_model_name, "local_text",
                                                  timeout=settings.llm_timeout_s)
            _sems["local"] = _sems.get("local") or threading.Semaphore(settings.local_concurrency)
        elif mid.startswith("claude-code:"):
            from axiom.model_adapters.cli_agents import ClaudeCodeAdapter
            ad = ClaudeCodeAdapter(mid.split(":", 1)[1], timeout=settings.llm_timeout_s)
            _sems[mid] = threading.Semaphore(max(settings.subscription_concurrency, settings.parallel_concurrency))
        elif mid.startswith("codex:"):
            from axiom.model_adapters.cli_agents import CodexAdapter
            m = mid.split(":", 1)[1]
            ad = CodexAdapter(None if m == "default" else m, timeout=settings.llm_timeout_s)
            _sems[mid] = threading.Semaphore(max(settings.subscription_concurrency, settings.parallel_concurrency))
        elif mid == "api:anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
            from axiom.model_adapters.anthropic import AnthropicAdapter
            ad = AnthropicAdapter(os.environ["ANTHROPIC_API_KEY"], settings.premium_model)
            _sems[mid] = threading.Semaphore(max(settings.premium_concurrency, settings.parallel_concurrency))
        elif mid == "api:openai" and os.environ.get("OPENAI_API_KEY"):
            ad = OpenAICompatAdapter("https://api.openai.com/v1", os.environ.get("AXIOM_OPENAI_MODEL", "gpt-5"),
                                     "premium", api_key=os.environ["OPENAI_API_KEY"], local_llamacpp=False)
            _sems[mid] = threading.Semaphore(max(settings.premium_concurrency, settings.parallel_concurrency))
        else:
            raise BudgetExceeded(f"model {model!r} is not available")
        _adapters[mid] = ad
        return ad


def _sem(model: str) -> threading.Semaphore:
    mid = _canon(model)
    return _sems["local"] if mid == "local" else _sems[mid]


def _cache_key(model: str, task: str, prompt_version: str, req: ChatRequest, decoding: str = "") -> str:
    h = hashlib.sha256()
    for part in (model, task, prompt_version, req.system or "", req.user_text,
                 json.dumps(req.json_schema, sort_keys=True) if req.json_schema else "", str(req.thinking),
                 str(req.max_tokens)) + ((decoding,) if decoding and req.json_schema else ()):
        h.update(part.encode())
        h.update(b"\x00")
    for im in req.images:
        h.update(hashlib.sha256(im.png).digest())
    return h.hexdigest()


def _record(doc_id: str | None, task: str, model: str, mclass: str, cached: bool, ok: bool, res: ChatResult | None):
    with db.connect() as c:
        c.execute(
            "INSERT INTO llm_calls(doc_id,task,model,model_class,cached,ok,seconds,prompt_tokens,completion_tokens,ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (doc_id, task, model, mclass, int(cached), int(ok), res.seconds if (res and not cached) else 0.0,
             res.prompt_tokens if res else None, res.completion_tokens if res else None,
             datetime.now(timezone.utc).isoformat()))


def premium_calls_used(doc_id: str) -> int:
    row = db.q1("SELECT COUNT(*) n FROM llm_calls WHERE doc_id=? AND model_class='premium' AND cached=0", (doc_id,))
    return int(row["n"]) if row else 0


def call(task: str, model: "str | models.Route", req: ChatRequest, prompt_version: str, *,
         doc_id: str | None = None, profile: str = "economy", use_cache: bool = True) -> tuple[ChatResult, bool]:
    """Returns (result, cached). `model` is a model id ("local_text", "claude-code:sonnet", ...) or a Route;
    a Route falls back to its fallback model when the first one is unavailable, over its cap, or errors."""
    from axiom.orchestration import models
    if isinstance(model, models.Route):
        try:
            return call(task, model.model, req, prompt_version, doc_id=doc_id, profile=profile, use_cache=use_cache)
        except (AdapterUnavailable, BudgetExceeded) as e:
            fb = model.fallback
            # never fall back to a model this machine cannot reach (a laptop has no Qwen): its "connection
            # refused" would replace the real error of the model that was actually chosen
            if not fb or not models.available("local" if models.kind(fb) == "local" else fb):
                raise
            log.warning("%s: %s unavailable (%s); falling back to %s", task, model.model, e, model.fallback)
            return call(task, fb, req, prompt_version, doc_id=doc_id, profile=profile, use_cache=use_cache)
    mid = _canon(model)
    mclass = _metering_class(mid, model)
    ad = get_adapter(mid)
    if req.images and not ad.supports_vision:
        raise AdapterUnavailable(f"{ad.name} cannot read images")
    key = _cache_key(ad.name, task, prompt_version, req, getattr(ad, "decoding", ""))
    if use_cache:
        row = db.q1("SELECT response FROM llm_cache WHERE key=?", (key,))
        if row:
            d = json.loads(row["response"])
            res = ChatResult(d["text"], d["model"], d.get("pt"), d.get("ct"), d.get("s", 0.0), d.get("fr"), mid)
            _record(doc_id, task, res.model, mclass, True, True, res)
            return res, True
    k = models.kind(mid)
    if k == "api":  # billed per token: per-document budget from the profile
        prof = PROFILES[profile]
        limit = prof.max_premium_calls or 40  # an explicit API choice under "economy" still gets a small budget
        if doc_id and premium_calls_used(doc_id) >= limit:
            raise BudgetExceeded(f"API call budget ({limit}) exhausted for {doc_id}")
    elif k == "subscription" and models.calls_today(mid) >= models.daily_limit(task):
        raise BudgetExceeded(f"daily limit for {mid} reached ({models.daily_limit(task)} calls); "
                             "raise AXIOM_SUBSCRIPTION_DAILY_LIMIT(_BULK) or wait until tomorrow")
    with _sem(mid):
        try:
            res = ad.chat(req)
        except Exception:
            _record(doc_id, task, ad.name, mclass, False, False, None)
            raise
    res.model_id = mid
    if res.finish_reason == "length":
        log.warning("%s: output truncated (max_tokens=%s)", task, req.max_tokens)
    _record(doc_id, task, res.model, mclass, False, True, res)
    if use_cache and res.finish_reason != "length":
        with db.connect() as c:
            c.execute("INSERT OR REPLACE INTO llm_cache(key,task,model,prompt_version,response,created_at) VALUES (?,?,?,?,?,?)",
                      (key, task, res.model, prompt_version,
                       db.jdump({"text": res.text, "model": res.model, "pt": res.prompt_tokens,
                                 "ct": res.completion_tokens, "s": res.seconds, "fr": res.finish_reason}),
                       datetime.now(timezone.utc).isoformat()))
    return res, False


def _invalidate(task: str, model: str, req: ChatRequest, prompt_version: str):
    ad = get_adapter(model)
    with db.connect() as c:
        c.execute("DELETE FROM llm_cache WHERE key=?", (_cache_key(ad.name, task, prompt_version, req,
                                                                getattr(ad, "decoding", "")),))


# LaTeX commands whose leading letter collides with a JSON escape (\t \b \f \n \r). Models often emit
# "\frac" inside a JSON string with a single backslash, which JSON decodes to FORMFEED + "rac".
_LATEX_JSON_COLLISIONS = re.compile(
    # \n and \t + letters may be a real newline / tab-indented code ("\nThe", "\tconst"), so only known
    # commands are repaired; BS/FF/CR directly before a letter never occurs, so \b \f \r + letters is always
    # LaTeX (\rightarrow, \frak, \bigl, ...)
    r"(?<!\\)\\("
    r"n(?:abla|u|eq|e|ot|otin|i|eg|leq|geq|ewline|olimits|onumber|parallel|subseteq|warrow|earrow|atural)(?![A-Za-z])"
    r"|t(?:ext|extbf|extit|extrm|extsf|exttt|extstyle|au|an|anh|heta|imes|ilde|o|op|riangle|frac|ensor|ag"
    r"|hinspace|riangleq)(?![A-Za-z])"
    r"|[bfr][A-Za-z]+"
    r")"
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0d\x0e-\x1f]")


def repair_latex_escapes(raw: str) -> str:
    raw = _LATEX_JSON_COLLISIONS.sub(lambda m: "\\\\" + m.group(1), raw)
    # any other backslash that is not a valid JSON escape (e.g. "\mu") is a literal LaTeX backslash
    return re.sub(r'(?<!\\)((?:\\\\)*)\\(?=[^"\\/bfnrtu])', lambda m: m.group(1) + "\\\\", raw)


def _check_no_control(o: Any) -> None:
    if isinstance(o, str):
        if _CONTROL.search(o):
            raise ValidationFailed("string contains a control character (probably a broken LaTeX backslash escape "
                                   "such as \\t in \\text or \\f in \\frac); write backslashes as \\\\ in JSON")
    elif isinstance(o, dict):
        for v in o.values():
            _check_no_control(v)
    elif isinstance(o, list):
        for v in o:
            _check_no_control(v)


def parse_json(text: str) -> Any:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1].rsplit("```", 1)[0]
    data = json.loads(repair_latex_escapes(t))
    _check_no_control(data)
    return data


def call_json(task: str, model: "str | models.Route", req: ChatRequest, prompt_version: str,
              validate: Callable[[Any], Any], *, doc_id: str | None = None, profile: str = "economy",
              attempts: int = 2) -> tuple[Any, dict]:
    """Call, parse JSON, run `validate` (raises ValidationFailed / ValueError on bad output).
    Retries with the error message appended. A local first choice escalates to premium after repeated invalid
    output (evidence-based) when the profile allows it; a Route falls back to its fallback model when the first
    model is unavailable or over its cap. Returns (validated_value, meta) with the model that actually answered."""
    from axiom.orchestration import models
    route = model if isinstance(model, models.Route) else models.Route(model, None, "fixed")
    chain: list[str] = [route.model] * attempts
    if (_canon(route.model) == "local" and route.model != "local_vlm" and PROFILES[profile].premium_enabled
            and (models.load_routing()["cloud_enabled"] or models.kind(models.best_premium() or "local") == "api")):
        best = models.best_premium()
        if best:
            chain.append(best)
    errors: list[str] = []
    fell_back = False
    i = 0
    while i < len(chain):
        mid = chain[i]
        r = req
        if errors:
            r = ChatRequest(req.system, req.user_text + "\n\nYour previous answer was rejected by the validator: "
                            + errors[-1][:800] + "\nReturn a corrected answer.", req.images, req.json_schema,
                            req.max_tokens, 0.2 if _canon(mid) == "local" else req.temperature, req.thinking)
        try:
            res, cached = call(task, mid, r, prompt_version, doc_id=doc_id, profile=profile)
        except (BudgetExceeded, AdapterUnavailable) as e:
            errors.append(f"{type(e).__name__}: {e}")
            fb_ok = bool(route.fallback) and models.available("local" if models.kind(route.fallback) == "local"
                                                               else route.fallback)  # see call(): no unreachable fallback
            if fb_ok and not fell_back and _canon(mid) != _canon(route.fallback):
                log.warning("%s: %s unavailable (%s); falling back to %s", task, mid, e, route.fallback)
                fell_back = True
                chain = chain[:i] + [route.fallback] * attempts
                continue
            if isinstance(e, BudgetExceeded) or i == len(chain) - 1:
                break
            i += 1
            continue
        try:
            value = validate(parse_json(res.text))
            return value, {"model": res.model, "model_id": _canon(mid), "model_class": _metering_class(_canon(mid), mid),
                           "attempt": i + 1, "cached": cached, "fallback": fell_back,
                           "escalated": _canon(mid) != _canon(route.model) and not fell_back, "errors": errors}
        except (ValueError, ValidationFailed, KeyError, TypeError) as e:
            errors.append(f"{type(e).__name__}: {e}")
            _invalidate(task, mid, r, prompt_version)  # never keep a rejected answer in cache
        i += 1
    raise ValidationFailed(f"{task}: all attempts failed: {errors}")
