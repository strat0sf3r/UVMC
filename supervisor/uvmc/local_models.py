"""The models local-model can put on the PC's graphics card (local_models.json), and which one UVMC uses.

local-model (windows\\local-model.ps1, unix/local-model.sh) runs llama.cpp's server in router mode:
every downloaded model is listed at /models, and the one a request names is loaded on the card,
one at a time. The panel picks which one UVMC asks for (Settings.local_model). The same JSON file
gives the scripts and the panel each model's description, strengths and weaknesses.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

CATALOG = Path(__file__).with_name("local_models.json")
ANY = "local"          # the profile's model name meaning "whichever local-model has" (config.toml)
INFO = ("label", "family", "maker", "kind", "by", "size_gb", "min_mib", "tested", "about", "good", "weak")


@lru_cache(maxsize=1)
def catalog() -> dict:
    try:
        return json.loads(CATALOG.read_text(encoding="utf-8")).get("models") or {}
    except (OSError, ValueError):
        return {}


def info(mid: str) -> dict:
    """What the panel shows about a model. One not on the list (from an older local-model) gets its name."""
    m = catalog().get(mid)
    if not m:
        return {"id": mid, "label": mid, "known": False}
    return {"id": mid, "known": True, **{k: m.get(k) for k in INFO}}


def router_models(data) -> list[dict] | None:
    """The models in a /models answer from llama.cpp's router: [{"id", "status"}], or None when the
    server is not a router (local-model before 0.16 ran one model, which answers whatever is asked)."""
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) and "status" in r for r in rows):
        return None
    out = []
    for r in rows:
        st = r.get("status")
        out.append({"id": str(r.get("id") or ""), "status": str(st.get("value") if isinstance(st, dict) else st or "")})
    return [r for r in out if r["id"]]


def choose(wanted: str, rows: list[dict]) -> str:
    """The model to ask for: the one picked in the panel if it's there, else the one on the card,
    else the first on the list."""
    ids = [r["id"] for r in rows]
    if wanted and wanted != ANY and wanted in ids:
        return wanted
    loaded = [r["id"] for r in rows if r["status"] == "loaded"]
    return loaded[0] if loaded else (ids[0] if ids else wanted)
