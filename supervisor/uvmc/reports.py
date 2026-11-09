"""Reports from the tools the agent built: data, never a page.

A tool writes a JSON report to the file $UVMC_REPORT; the panel draws it with its own code
from a fixed set of blocks (text, tables, charts, ...). Nothing the tool writes runs in the
panel: text is escaped, links are only http(s), charts are drawn by the panel, and images
are copied out after the run and checked to really be PNG, JPEG, GIF or WebP. A report
that's partly wrong is still shown: the bad parts are dropped and listed as problems.
"""

from __future__ import annotations

import difflib
import json
import math
import os
import re

MAX_REPORT = 300_000       # bytes of JSON
MAX_BLOCKS = 100           # counting the ones inside columns and sections
MAX_IMAGES, MAX_IMAGE = 10, 2_000_000
STATES = ("ok", "warn", "bad", "info")
CONTAINERS = ("columns", "section")

FORMAT_HELP = """A tool can also leave a report, which the panel shows as a card in the open conversation
after each run (otherwise it shows what the tool printed). Write JSON to the file named by the
environment variable UVMC_REPORT (in the tool's HOME; an old one is removed before each run):
{
  "title": "Site check, 9 Nov",                              optional
  "status": {"state": "warn", "text": "2 pages changed"},  optional; state: ok, warn, bad or info
  "notify": "The Pro price went up to 24 EUR",            optional: asks for a notification with this text
  "blocks": [                                              up to 100 in all, shown in order
    {"type": "heading", "text": "Changes"},
    {"type": "text", "md": "Checked **14 pages**."},        Markdown: **bold**, `code`, - lists, ### headings
    {"type": "stats", "items": [{"label": "Pages", "value": "14", "note": "+2 since yesterday", "trend": "up"}]},
    {"type": "progress", "label": "Disk", "value": 72, "max": 100, "note": "72 of 100 GB"},
    {"type": "table", "columns": ["Page", "Change"], "rows": [["/pricing", "price went up"]]},
    {"type": "list", "items": ["first", "second"], "numbered": false},
    {"type": "chart", "kind": "line", "x": ["Mon", "Tue"], "series": [{"name": "ms", "values": [210, 340]}], "unit": "ms"},
    {"type": "diff", "before": "old text", "after": "new text"},   or {"type": "diff", "text": "<a unified diff>"}
    {"type": "image", "file": "plot.png", "alt": "What it shows"},  a PNG, JPEG, GIF or WebP in HOME, up to 2 MB
    {"type": "link", "text": "Open the pricing page", "url": "https://example.com/pricing"},
    {"type": "code", "text": "raw text, shown as is"},
    {"type": "columns", "columns": [[<blocks>], [<blocks>]]},     2 or 3 side by side; they stack on a phone
    {"type": "section", "title": "Details", "open": false, "blocks": [<blocks>]}   can be folded
  ]
}
The user picks per tool whether notifications pop up: never, when the tool asks (with
"notify"), fails or its status is bad (the default), or after every run. Ask only when
something needs the user's attention, not on every run.
Columns and sections hold the other blocks, not each other. Chart kind: line or bar; up to 6
series of up to 500 numbers (null for a gap), one per x label. Trend: up, down or flat. Links:
http or https only. At most 10 images. Nothing in a report runs: it's data the panel draws.
When you test the tool in your folder, set UVMC_REPORT=uvmc-report.json there (images then
come from the folder): propose_tool checks that report, tells you what's wrong, and shows it
to the user on the approval card as a sample."""

IMAGE_TYPES = {b"\x89PNG\r\n\x1a\n": "image/png", b"\xff\xd8\xff": "image/jpeg", b"GIF87a": "image/gif",
               b"GIF89a": "image/gif"}


def image_type(data: bytes) -> str | None:
    """The image's type from its first bytes (never from its name), or None."""
    for magic, kind in IMAGE_TYPES.items():
        if data.startswith(magic):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _s(v, n: int) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)[:n]


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return v


class _Parser:
    def __init__(self):
        self.problems: list[str] = []
        self.count = 0
        self.images: list[str] = []      # the files the report names, in order

    def bad(self, where: str, msg: str) -> None:
        self.problems.append(f"{where}: {msg}")

    def blocks(self, items, where: str, inside: bool = False) -> list[dict]:
        if not isinstance(items, list):
            self.bad(where, "blocks must be a list.")
            return []
        out = []
        for i, b in enumerate(items):
            if self.count >= MAX_BLOCKS:
                self.bad(where, f"at most {MAX_BLOCKS} blocks in all; the rest were left out.")
                break
            here = f"{where} {i + 1}" if not inside else f"{where}, block {i + 1}"
            got = self.block(b, here, inside)
            if got is not None:
                self.count += 1
                out.append(got)
        return out

    def block(self, b, where: str, inside: bool) -> dict | None:
        bad = lambda msg: self.bad(where, msg)  # noqa: E731
        if not isinstance(b, dict):
            bad("each block is an object with a type.")
            return None
        kind = b.get("type")
        if kind in CONTAINERS and inside:
            bad(f"{kind} can't be inside columns or a section.")
            return None
        if kind == "heading":
            return {"type": "heading", "text": _s(b.get("text"), 200)}
        if kind == "text":
            return {"type": "text", "md": _s(b.get("md", b.get("text")), 20_000)}
        if kind == "code":
            return {"type": "code", "text": _s(b.get("text"), 50_000)}
        if kind == "stats":
            items = b.get("items")
            if not isinstance(items, list) or not items:
                bad("stats needs items: [{label, value}].")
                return None
            out = []
            for it in items[:12]:
                if isinstance(it, dict):
                    trend = it.get("trend")
                    out.append({"label": _s(it.get("label"), 80), "value": _s(it.get("value"), 40),
                                "note": _s(it.get("note"), 120),
                                "trend": trend if trend in ("up", "down", "flat") else ""})
            if len(items) > 12:
                bad("stats: at most 12 items; the rest were left out.")
            return {"type": "stats", "items": out}
        if kind == "progress":
            value, top = _num(b.get("value")), _num(b.get("max", 100))
            if value is None or top is None or top <= 0:
                bad("progress needs a number value and a max above 0.")
                return None
            return {"type": "progress", "label": _s(b.get("label"), 80), "value": value, "max": top,
                    "note": _s(b.get("note"), 120)}
        if kind == "table":
            cols, rows = b.get("columns"), b.get("rows")
            if not isinstance(rows, list):
                bad("table needs rows: a list of lists.")
                return None
            cols = [_s(c, 80) for c in cols[:20]] if isinstance(cols, list) else []
            width = min(len(cols) or max((len(r) for r in rows if isinstance(r, list)), default=0), 20)
            out = [[_s(c, 1000) for c in (r[:width] + [None] * (width - len(r)))]
                   for r in rows[:500] if isinstance(r, list)]
            if len(rows) > 500:
                bad(f"table: at most 500 rows; {len(rows) - 500} were left out.")
            return {"type": "table", "columns": cols, "rows": out}
        if kind == "list":
            items = b.get("items")
            if not isinstance(items, list):
                bad("list needs items: a list of texts.")
                return None
            return {"type": "list", "items": [_s(i, 2000) for i in items[:200]], "numbered": bool(b.get("numbered"))}
        if kind == "chart":
            x, series = b.get("x"), b.get("series")
            if not isinstance(x, list) or not x or not isinstance(series, list) or not series:
                bad("chart needs x (labels) and series: [{name, values}].")
                return None
            x = [_s(v, 40) for v in x[:500]]
            out = []
            for sr in series[:6]:
                if not isinstance(sr, dict) or not isinstance(sr.get("values"), list):
                    bad("chart: each series needs name and values.")
                    continue
                vals = [_num(v) for v in sr["values"][:len(x)]]
                out.append({"name": _s(sr.get("name"), 60), "values": vals + [None] * (len(x) - len(vals))})
            if not out:
                return None
            if len(series) > 6:
                bad("chart: at most 6 series; the rest were left out.")
            return {"type": "chart", "kind": "bar" if b.get("kind") == "bar" else "line", "x": x,
                    "series": out, "unit": _s(b.get("unit"), 20)}
        if kind == "diff":
            if "before" in b or "after" in b:
                before, after = _s(b.get("before"), 100_000), _s(b.get("after"), 100_000)
                text = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                                    "before", "after", n=3))
                text = text or "(no difference)"
            else:
                text = _s(b.get("text"), 100_000)
            return {"type": "diff", "text": text}
        if kind == "image":
            name = _s(b.get("file"), 200).strip()
            if not re.match(r"^[A-Za-z0-9._/-]+$", name) or name.startswith("/") or ".." in name.split("/"):
                bad("image: file is a path inside the tool's HOME, like plot.png.")
                return None
            if name not in self.images:
                if len(self.images) >= MAX_IMAGES:
                    bad(f"at most {MAX_IMAGES} images.")
                    return None
                self.images.append(name)
            return {"type": "image", "file": name, "alt": _s(b.get("alt"), 300)}
        if kind == "link":
            url = _s(b.get("url"), 2000).strip()
            if not url.lower().startswith(("https://", "http://")) or any(c in url for c in " \n\r\t\"'<>"):
                bad("link: the url must start with https:// or http://.")
                return None
            return {"type": "link", "text": _s(b.get("text"), 200) or url, "url": url}
        if kind == "columns":
            cols = b.get("columns")
            if not isinstance(cols, list) or not 2 <= len(cols) <= 3:
                bad("columns needs 2 or 3 columns, each a list of blocks.")
                return None
            return {"type": "columns", "columns": [self.blocks(c, f"{where}, column {j + 1}", True)
                                                   for j, c in enumerate(cols)]}
        if kind == "section":
            return {"type": "section", "title": _s(b.get("title"), 200), "open": bool(b.get("open")),
                    "blocks": self.blocks(b.get("blocks", []), f"{where} (section)", True)}
        bad(f"unknown block type {kind!r} (heading, text, stats, progress, table, list, chart, diff, "
            "image, link, code, columns or section).")
        return None


def parse(data: bytes | str) -> tuple[dict | None, list[str]]:
    """The report the panel can draw, and what was wrong with it. None if there's nothing to show.
    report["images"] lists the files it names; the caller copies them out (see attach_images)."""
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    if len(data) > MAX_REPORT:
        return None, [f"The report is over {MAX_REPORT // 1000} KB."]
    try:
        raw = json.loads(data.decode("utf-8", "replace"))
    except ValueError as e:
        return None, [f"The report is not valid JSON: {e}"]
    if not isinstance(raw, dict):
        return None, ['The report must be a JSON object: {"title", "status", "blocks"}.']
    p = _Parser()
    status = raw.get("status")
    if isinstance(status, dict) and status.get("state") in STATES:
        status = {"state": status["state"], "text": _s(status.get("text"), 300)}
    else:
        if status is not None:
            p.problems.append('status: {"state": ok, warn, bad or info, "text": ...}.')
        status = None
    blocks = p.blocks(raw.get("blocks", []), "block")
    title = _s(raw.get("title"), 120)
    ask = raw.get("notify")   # the tool asks for a notification: its text ("" = it doesn't ask)
    ask = ask.strip()[:200] if isinstance(ask, str) else (title or "New result") if ask is True else ""
    report = {"title": title, "status": status, "blocks": blocks, "images": p.images, "notify": ask}
    if not blocks and not status and not report["title"]:
        return None, p.problems or ["The report is empty."]
    return report, p.problems


def first_stats(report: dict) -> list[dict]:
    """The items of the report's first stats block (also inside columns and sections): the
    overview card shows a few of them."""
    def walk(blocks):
        for b in blocks:
            if b["type"] == "stats":
                return b["items"]
            inner = b.get("columns") or ([b["blocks"]] if b["type"] == "section" else [])
            for c in inner:
                got = walk(c)
                if got:
                    return got
        return []
    return walk(report.get("blocks", []))


def attach_images(report: dict, read, out_dir, problems: list[str]) -> None:
    """Copies the images the report names into out_dir as 1, 2, ... (read(name) -> bytes or None),
    keeping only real PNG/JPEG/GIF/WebP files. Image blocks then say which copy: {"n": 1}."""
    kept: dict[str, int] = {}
    for name in report.get("images", []):
        data = read(name)
        if data is None:
            problems.append(f"image {name}: not found.")
            continue
        if len(data) > MAX_IMAGE:
            problems.append(f"image {name}: over {MAX_IMAGE // 1_000_000} MB.")
            continue
        if image_type(data) is None:
            problems.append(f"image {name}: not a PNG, JPEG, GIF or WebP.")
            continue
        n = len(kept) + 1
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, str(n)), "wb") as f:
            f.write(data)
        kept[name] = n

    def fix(blocks):
        for b in blocks:
            if b["type"] == "image":
                b["n"] = kept.get(b["file"])
            elif b["type"] == "columns":
                for c in b["columns"]:
                    fix(c)
            elif b["type"] == "section":
                fix(b["blocks"])
    fix(report["blocks"])
    report["images"] = len(kept)
