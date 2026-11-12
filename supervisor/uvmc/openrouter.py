"""OpenRouter: one key, many models, many providers behind them (openrouter.ai/docs).

It speaks the OpenAI-compatible API, so the `openai` adapter does the talking. This module adds
what is particular to it:

  routing    Each request names what a provider must do: support every parameter sent (tools),
             and for a profile above `open`, keep nothing (zdr) and collect nothing. OpenRouter
             then refuses rather than send the conversation to a provider that would.
  hints      Its errors (no credit, the free models' limits, no provider that keeps nothing)
             told in a way that says what to do.
  models     The models that can use tools, for the panel's picker (kept an hour).
  credit     What the key has left, for Settings › API keys.

No attribution headers are sent (HTTP-Referer, X-OpenRouter-Title): UVMC doesn't announce itself.
Recognised by its address only; the tests add their own stand-in host to HOSTS.
"""

from __future__ import annotations

import threading
import time
from urllib.parse import urlsplit

HOSTS = ["openrouter.ai"]                 # also eu.openrouter.ai and us.openrouter.ai
BASE_URL = "https://openrouter.ai/api/v1"
MODELS_TTL = 3600
_models_cache: dict[str, tuple[float, list[dict]]] = {}
_lock = threading.Lock()


class OpenRouterError(Exception):
    pass


def is_openrouter(base_url: str) -> bool:
    host = (urlsplit(base_url or "").hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in HOSTS)


def is_free(model: str) -> bool:
    """A `:free` model: no price, its own limits, and its providers may keep or train on what they get."""
    return "free" in (model or "").split(":")[1:]   # suffixes combine, e.g. "x/y:free:nitro"


FREE_PRIVATE = ("OpenRouter's free models (:free) may keep or train on what they get: "
                "their privacy can only be open.")


def free_but_private(base_url: str, model: str, level: str) -> bool:
    return is_openrouter(base_url) and is_free(model) and level in ("paid", "local")


def routing(level: str, tools: bool) -> dict:
    """The request's `provider` object. `level` is the profile's privacy level."""
    route: dict = {}
    if tools:
        route["require_parameters"] = True   # otherwise tools are only a preference
    if level in ("paid", "local"):
        route["zdr"] = True
        route["data_collection"] = "deny"
    return route


def hint(status: int, error: dict | None, retry_after: str | None, model: str, route: dict) -> str:
    """Words to add to an error from OpenRouter, saying what to do about it."""
    meta = (error or {}).get("metadata") if isinstance(error, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    out = ""
    if meta.get("provider_name"):
        out += f" [provider: {meta['provider_name']}]"
    if status == 402:
        out += " (no credit left on OpenRouter: add some at https://openrouter.ai/settings/credits)"
    elif status == 429:
        wait = f"try again in {retry_after} s" if retry_after else "try again in a moment"
        out += f" (OpenRouter's limit: {wait}"
        if is_free(model):
            out += ("; free models allow 20 requests a minute, and 50 a day until you have bought "
                    "10 dollars of credit, after that 1000")
        out += ")"
    elif status in (404, 503) and route.get("zdr"):
        out += (" (no provider of this model keeps nothing, which this profile's privacy level asks for: "
                "pick another model, or set the profile to open)")
    return out


def _get(base_url: str, key: str, path: str, params: dict | None = None) -> dict:
    import httpx
    if not is_openrouter(base_url):
        raise OpenRouterError("That is not OpenRouter's address.")
    if not key:
        raise OpenRouterError("No OpenRouter key yet: add it in Settings › API keys.")
    try:
        r = httpx.get(base_url.rstrip("/") + path, params=params, timeout=20,
                      headers={"Authorization": f"Bearer {key}"})
    except Exception as e:
        raise OpenRouterError(f"Could not reach OpenRouter: {e}") from e
    if r.status_code >= 400:
        try:
            err = r.json().get("error")
            msg = err.get("message") if isinstance(err, dict) else str(err or r.text[:300])
        except ValueError:
            msg = r.text[:300]
        raise OpenRouterError(f"OpenRouter answered {r.status_code}: {msg}")
    try:
        data = r.json()
    except ValueError as e:
        raise OpenRouterError("OpenRouter's answer wasn't JSON.") from e
    if not isinstance(data, dict):
        raise OpenRouterError("Unexpected answer from OpenRouter.")
    return data


def _per_million(v) -> float | None:
    try:
        return round(float(v) * 1_000_000, 4)
    except (TypeError, ValueError):
        return None


def models(base_url: str, key: str, fresh: bool = False) -> list[dict]:
    """The models that can use tools: id, name, price per million tokens in and out, context."""
    with _lock:
        hit = _models_cache.get(base_url)
        if hit and not fresh and time.time() - hit[0] < MODELS_TTL:
            return hit[1]
    data = _get(base_url, key, "/models", {"supported_parameters": "tools"})
    rows = []
    for m in data.get("data") or []:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str):
            continue
        if "tools" not in (m.get("supported_parameters") or ["tools"]):
            continue
        price = m.get("pricing") if isinstance(m.get("pricing"), dict) else {}
        p_in, p_out = _per_million(price.get("prompt")), _per_million(price.get("completion"))
        rows.append({"id": m["id"], "name": str(m.get("name") or m["id"]),
                     "in": p_in, "out": p_out, "context": m.get("context_length"),
                     "free": is_free(m["id"]) or (p_in == 0 and p_out == 0)})
    rows.sort(key=lambda r: r["name"].lower())
    with _lock:
        _models_cache[base_url] = (time.time(), rows)
    return rows


def credit(base_url: str, key: str) -> dict:
    """What the key has used and has left (never the key itself)."""
    d = _get(base_url, key, "/key").get("data") or {}
    free = d.get("free_model_daily_requests") if isinstance(d.get("free_model_daily_requests"), dict) else {}
    return {"limit": d.get("limit"), "remaining": d.get("limit_remaining"), "usage": d.get("usage"),
            "usage_daily": d.get("usage_daily"), "usage_monthly": d.get("usage_monthly"),
            "free_tier": d.get("is_free_tier"),
            "free_requests": {k: free.get(k) for k in ("used", "limit", "remaining")} if free else None}
