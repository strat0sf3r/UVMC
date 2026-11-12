"""UVMC client: lets your own program send reports to UVMC (and say it's alive), and keeps it
working without UVMC. One file, Python 3.8+, standard library only: copy it into your program.

    from uvmc_client import Uvmc
    uvmc = Uvmc()
    uvmc.report(title="Queue", status=("ok", "3 running"),
                blocks=[uvmc.table(["Job", "State"], [["a", "running"]]), uvmc.text("All fine.")])
    uvmc.heartbeat()

Where it reads its settings (the host contract):
  UVMC_URL       where UVMC answers, e.g. http://127.0.0.1:8080 (the panel's address)
  UVMC_KEY_FILE  a file holding the program's key (or UVMC_KEY, the key itself, or a systemd
                 credential named uvmc-key); the key comes from the panel: Settings > Programs
  UVMC_DATA      the program's data folder (default: the current folder); a report waiting to
                 be sent and the local reports are kept here

Where a report goes:
  UVMC_URL set    to UVMC. If UVMC can't be reached, is locked or busy, the report is kept and
                  sent with the next call that gets through. Nothing here ever raises because of
                  UVMC: a call that fails returns None (see .last_error).
  UVMC_REPORT set (the program runs as a UVMC tool) to that file, as tools do.
  neither         to UVMC_DATA/reports/latest.json (and the few before it), with a one-line
                  summary on stderr.

The report format is UVMC's (the panel draws it; nothing in it runs). The helpers below build
its blocks. The HTTP interface behind this file: GET /api/programs/self, POST
/api/programs/self/reports (a report), POST /api/programs/self/heartbeat, each with
"Authorization: Bearer <key>".
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

__version__ = "1.0.0"

PENDING = "uvmc-pending.json"
KEEP_LOCAL = 10


class Uvmc:
    def __init__(self, url: str | None = None, key: str | None = None, data: str | None = None,
                 timeout: float = 5.0):
        self.url = (url if url is not None else os.environ.get("UVMC_URL", "")).rstrip("/")
        self.key = key if key is not None else _read_key()
        self.data = Path(data or os.environ.get("UVMC_DATA") or ".")
        self.tool_report = os.environ.get("UVMC_REPORT", "")
        self.timeout = timeout
        self.last_error = ""

    @property
    def mode(self) -> str:
        """Where reports go: "uvmc", "tool" or "local"."""
        return "uvmc" if self.url else "tool" if self.tool_report else "local"

    # --- calls -----------------------------------------------------------------------------

    def whoami(self) -> dict | None:
        """The program's name and permissions as UVMC sees them (None without UVMC or on failure)."""
        return self._call("GET", "") if self.mode == "uvmc" else None

    def heartbeat(self) -> bool:
        """Says the program is alive (for UVMC's "tell me if it goes silent"). Also sends a report
        that was kept because UVMC couldn't be reached."""
        if self.mode != "uvmc":
            return True
        ok = self._call("POST", "/heartbeat", {}) is not None
        if ok:
            self._send_pending()
        return ok

    def report(self, title: str = "", status=None, blocks=(), notify: str | None = None) -> dict | None:
        """Sends a report. status: ("ok" | "warn" | "bad" | "info", "text") or a dict.
        notify: a text asks UVMC for a notification (only when something needs attention).
        Returns UVMC's answer ({"id", "problems"}), the file written, or None if it failed."""
        rep: dict = {"title": title, "blocks": list(blocks)}
        if status is not None:
            rep["status"] = {"state": status[0], "text": status[1]} if isinstance(status, (tuple, list)) else status
        if notify:
            rep["notify"] = notify
        mode = self.mode
        if mode == "uvmc":
            body = _images(rep, inline=True)
            got = self._call("POST", "/reports", body)
            if got is None:
                if self._keep_trying:
                    self._write(self.data / PENDING, body)
            else:
                self._drop_pending()
            return got
        if mode == "tool":
            folder = Path(self.tool_report).parent
            body = _images(rep, inline=False, folder=folder)
            self._write(Path(self.tool_report), body)
            return {"file": self.tool_report}
        folder = self.data / "reports"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        body = _images(rep, inline=False, folder=folder, prefix=stamp + "-")
        self._write(folder / f"{stamp}.json", body)
        self._write(folder / "latest.json", body)
        olds = sorted(p for p in folder.glob("2*.json"))
        for old in olds[:-KEEP_LOCAL]:
            for img in folder.glob(old.stem + "-img*"):
                img.unlink(missing_ok=True)
            old.unlink(missing_ok=True)
        st = rep.get("status") or {}
        print(f"report: {title or '(no title)'}{' - ' + st.get('text', '') if st else ''} -> {folder / 'latest.json'}",
              file=sys.stderr)
        return {"file": str(folder / "latest.json")}

    # --- block helpers (UVMC's report format) ----------------------------------------------

    @staticmethod
    def heading(text: str) -> dict:
        return {"type": "heading", "text": text}

    @staticmethod
    def text(md: str) -> dict:
        """Markdown: **bold**, `code`, - lists, ### headings."""
        return {"type": "text", "md": md}

    @staticmethod
    def code(text: str) -> dict:
        return {"type": "code", "text": text}

    @staticmethod
    def table(columns, rows) -> dict:
        return {"type": "table", "columns": [str(c) for c in columns],
                "rows": [[None if c is None else str(c) for c in r] for r in rows]}

    @staticmethod
    def stats(items) -> dict:
        """[{"label", "value", "note"?, "trend"?: up | down | flat}] or {label: value}."""
        if isinstance(items, dict):
            items = [{"label": k, "value": str(v)} for k, v in items.items()]
        return {"type": "stats", "items": [{**i, "value": str(i.get("value", ""))} for i in items]}

    @staticmethod
    def progress(label: str, value: float, max: float = 100, note: str = "") -> dict:  # noqa: A002
        return {"type": "progress", "label": label, "value": value, "max": max, "note": note}

    @staticmethod
    def items(items, numbered: bool = False) -> dict:
        """A list block."""
        return {"type": "list", "items": [str(i) for i in items], "numbered": numbered}

    @staticmethod
    def chart(x, series: dict, kind: str = "line", unit: str = "") -> dict:
        """series: {name: [numbers, one per x label; None for a gap]}."""
        return {"type": "chart", "kind": kind, "x": [str(v) for v in x], "unit": unit,
                "series": [{"name": n, "values": list(v)} for n, v in series.items()]}

    @staticmethod
    def diff(before: str, after: str) -> dict:
        return {"type": "diff", "before": before, "after": after}

    @staticmethod
    def link(text: str, url: str) -> dict:
        return {"type": "link", "text": text, "url": url}

    @staticmethod
    def image(picture, alt: str = "") -> dict:
        """A PNG, JPEG, GIF or WebP: a file path or its bytes (up to 2 MB)."""
        data = picture if isinstance(picture, (bytes, bytearray)) else Path(picture).read_bytes()
        return {"type": "image", "_bytes": bytes(data), "alt": alt}

    @staticmethod
    def section(title: str, blocks, open: bool = False) -> dict:  # noqa: A002
        return {"type": "section", "title": title, "open": open, "blocks": list(blocks)}

    @staticmethod
    def columns(*cols) -> dict:
        """2 or 3 columns, each a list of blocks."""
        return {"type": "columns", "columns": [list(c) for c in cols]}

    # --- inside ----------------------------------------------------------------------------

    _keep_trying = True   # set per call: False when UVMC refused the report itself

    def _call(self, method: str, path: str, body: dict | None = None) -> dict | None:
        self._keep_trying = True
        if not self.key:
            self.last_error = "No program key (UVMC_KEY_FILE, UVMC_KEY or a credential named uvmc-key)."
            self._keep_trying = False
            return None
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + "/api/programs/self" + path, data=data, method=method,
                                     headers={"Authorization": f"Bearer {self.key}",
                                              "Content-Type": "application/json",
                                              "User-Agent": f"uvmc-client/{__version__}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                self.last_error = ""
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")[:500]
            try:
                text = json.loads(text).get("detail", text)
            except (ValueError, AttributeError):
                pass
            self.last_error = f"UVMC answered {e.code}: {text}"
            # Locked (423), too many (429) or UVMC's own trouble (5xx): worth sending again later.
            # A refused report or key (4xx) would be refused again.
            self._keep_trying = e.code in (423, 429) or e.code >= 500
        except (OSError, ValueError) as e:
            self.last_error = f"UVMC can't be reached: {e}"
        return None

    def _send_pending(self) -> None:
        path = self.data / PENDING
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if self._call("POST", "/reports", body) is not None or not self._keep_trying:
            self._drop_pending()

    def _drop_pending(self) -> None:
        try:
            (self.data / PENDING).unlink()
        except OSError:
            pass

    @staticmethod
    def _write(path: Path, body: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(body), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            print(f"uvmc_client: could not write {path}: {e}", file=sys.stderr)


def _read_key() -> str:
    paths = [os.environ.get("UVMC_KEY_FILE", "")]
    if os.environ.get("CREDENTIALS_DIRECTORY"):
        paths.append(os.path.join(os.environ["CREDENTIALS_DIRECTORY"], "uvmc-key"))
    for p in paths:
        if p:
            try:
                return Path(p).read_text(encoding="utf-8").strip()
            except OSError:
                pass
    return os.environ.get("UVMC_KEY", "").strip()


def _images(rep: dict, inline: bool, folder: Path | None = None, prefix: str = "") -> dict:
    """A copy of the report with its pictures as UVMC wants them: base64 inside (UVMC_URL),
    or files next to the report (a tool's HOME, or the local reports folder)."""
    count = [0]

    def fix(block):
        if not isinstance(block, dict):
            return block
        b = dict(block)
        if b.get("type") == "image" and "_bytes" in b:
            data = b.pop("_bytes")
            if inline:
                b["data"] = base64.b64encode(data).decode()
            else:
                count[0] += 1
                name = f"{prefix}img{count[0]}{_ext(data)}"
                try:
                    folder.mkdir(parents=True, exist_ok=True)
                    (folder / name).write_bytes(data)
                except OSError:
                    pass
                b["file"] = name
        if "blocks" in b:
            b["blocks"] = [fix(x) for x in b["blocks"]]
        if "columns" in b and b.get("type") == "columns":
            b["columns"] = [[fix(x) for x in col] for col in b["columns"]]
        return b

    return {**rep, "blocks": [fix(b) for b in rep.get("blocks", [])]}


def _ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data.startswith(b"GIF8"):
        return ".gif"
    if data[8:12] == b"WEBP":
        return ".webp"
    return ".bin"
