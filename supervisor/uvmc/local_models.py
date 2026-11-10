"""The models local-model can put on the PC's graphics card (local_models.json), and which one UVMC uses.

local-model (windows\\local-model.ps1, unix/local-model.sh) runs llama.cpp's server in router mode:
every downloaded model is listed at /models, and the one a request names is loaded on the card,
one at a time. The panel picks which one UVMC asks for (Settings.local_model). The same JSON file
gives the scripts and the panel the list: grouped by model (description, strengths, weaknesses),
each with its sizes (file, pinned hash, memory needed on a graphics card and on a Mac).

local-model also tells this machine what it found on the PC (HARDWARE: the card or the Mac and its
memory), so the panel can say which sizes fit. Nothing is detected here.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

CATALOG = Path(__file__).with_name("local_models.json")
HARDWARE = Path("/etc/uvmc/pc-hardware.json")   # written by local-model (kind card|mac, name, mib)
ANY = "local"          # the profile's model name meaning "whichever local-model has" (config.toml)
FAMILY = ("label", "maker", "kind", "by", "recommended", "about", "good", "weak")
SIZE = ("label", "size", "version", "size_gb", "card_gb", "mac_gb", "tested")


@lru_cache(maxsize=1)
def _data() -> dict:
    try:
        return json.loads(CATALOG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def families() -> list[dict]:
    """The list as the panel shows it: each model with its sizes, in the file's order."""
    return [f for f in _data().get("families") or [] if isinstance(f, dict) and f.get("sizes")]


@lru_cache(maxsize=1)
def catalog() -> dict:
    """Every size by its name (what local-model -Add takes and UVMC asks the server for), with its
    model's description, strengths and weaknesses."""
    out = {}
    for f in families():
        for s in f["sizes"]:
            out[s["id"]] = {**{k: f.get(k) for k in FAMILY}, **s, "family": f.get("label"), "family_id": f.get("id")}
    return out


def info(mid: str) -> dict:
    """What the panel shows about a size. One not on the list is the user's own (local-model -AddFile),
    or from an older list: it gets its name."""
    m = catalog().get(mid)
    if not m:
        return {"id": mid, "label": mid, "known": False}
    return {"id": mid, "known": True, "family": m["family"], "family_id": m["family_id"],
            **{k: m.get(k) for k in FAMILY if k != "label"}, **{k: m.get(k) for k in SIZE}}


def hardware(path: Path | None = None) -> dict | None:
    """What local-model found on the PC: {"kind": "card"|"mac", "name", "mib"}, or None (not told yet)."""
    try:
        d = json.loads((path or HARDWARE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("kind") not in ("card", "mac"):
        return None
    try:
        mib = int(d.get("mib") or 0)
    except (TypeError, ValueError):
        return None
    return {"kind": d["kind"], "name": str(d.get("name") or "")[:120], "mib": mib} if mib > 0 else None


def fit(size: dict, hw: dict | None) -> str:
    """How a size suits the PC: "fits", "tight" (the file fits but little room is left for the
    conversation, so it may spill and slow down), "too_big", or "" when the PC isn't known.
    hw["mib"] is what llama.cpp can use on the device: a card's memory, or on a Mac the part of its
    memory the graphics may use (so it is compared with card_gb too; mac_gb is the Mac's total
    memory, for people choosing). Same rule as both local-model scripts."""
    if not hw or not size.get("card_gb"):
        return ""
    have = hw["mib"]                 # an "8 GB" card reports about 8,176 MiB
    if have >= 1000 * float(size["card_gb"]):
        return "fits"
    return "tight" if have >= 1000 * float(size.get("size_gb") or 0) + 500 else "too_big"


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
    else the first on the list. (A pick not downloaded yet is used as soon as it is.)"""
    ids = [r["id"] for r in rows]
    if wanted and wanted != ANY and wanted in ids:
        return wanted
    loaded = [r["id"] for r in rows if r["status"] == "loaded"]
    return loaded[0] if loaded else (ids[0] if ids else wanted)
