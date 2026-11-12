"""Programs that use UVMC: your own software, running on its own (in the VM, on the PC, ...),
which calls UVMC's API with a key of its own.

A program is not a tool: UVMC doesn't run it, contain it or hold its secrets. It only gets what
its key allows, and in this version that is sending reports (shown in the Reports tab next to
the tools', with the same notifications) and saying it's alive (a notification if it goes
silent for longer than you set). The routes are under /api/programs/self/ (service.py); a
program key reaches nothing else, and is refused while UVMC is locked.

Keys look like uvmcp_<id>_<secret>. Only a hash of the secret is kept (it's 32 random bytes,
so a plain SHA-256 is enough), compared in constant time; the key is shown once.
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

from . import reports

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
RESERVED = {"self", "add", "key", "remove", "update", "seen"}
PERMISSIONS = {"reports": "Send reports"}
NOTIFY_MODES = ("asks", "always", "never")
KEY_PREFIX = "uvmcp_"
RATE = 30            # reports per minute per program
KEEP_CALLS = 50      # calls listed on a program's page


class ProgramError(Exception):
    """Something you or a program asked that can't be done; the message says why."""


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def split_key(key: str) -> tuple[str, str] | None:
    """(id, secret) of a program key, or None if it isn't shaped like one."""
    if not key.startswith(KEY_PREFIX):
        return None
    kid, sep, secret = key[len(KEY_PREFIX):].partition("_")
    if not sep or not re.match(r"^[a-z0-9]{8}$", kid) or not re.match(r"^[A-Za-z0-9]{40,}$", secret):
        return None
    return kid, secret


class Programs:
    """The registry (state_dir/programs/registry.json), each program's reports and the silent check."""

    def __init__(self, cfg, notify: Callable[[], None] = lambda: None,
                 on_report: Callable[[str, str], None] = lambda name, rid: None,
                 on_silent: Callable[[str, dict], None] = lambda name, info: None):
        self.base = Path(cfg.state_dir) / "programs"
        self.keep = max(1, int(getattr(cfg, "made_tools_keep_runs", 30)))
        self.notify, self.on_report, self.on_silent = notify, on_report, on_silent
        self.lock = threading.RLock()
        self.recent: dict[str, collections.deque] = {}    # report times, for the rate limit
        self.registry: dict[str, dict] = self._load()

    # --- storage ---------------------------------------------------------------------------

    def _load(self) -> dict:
        try:
            return json.loads((self.base / "registry.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        os.chmod(self.base, 0o700)
        path = self.base / "registry.json"
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.registry, f)
        os.replace(tmp, path)

    def _dir(self, name: str) -> Path:
        return self.base / name

    def get(self, name: str) -> dict:
        e = self.registry.get(name)
        if e is None:
            raise KeyError(f"There's no program called {name}.")
        return e

    # --- managing them (the panel) ---------------------------------------------------------

    def _new_key(self, e: dict) -> str:
        kid = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(8))
        secret = "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
                         for _ in range(43))   # ~256 bits
        e["key_id"], e["key_hash"], e["key_made"] = kid, _hash(secret), time.time()
        return f"{KEY_PREFIX}{kid}_{secret}"

    def add(self, name: str, description: str = "", kind: str = "program") -> str:
        """A new program; returns its key, which is never shown again."""
        name = str(name).strip().lower()
        if not NAME_RE.match(name) or name in RESERVED:
            raise ProgramError("A program's name: 2-32 lowercase letters, digits and dashes, starting with a "
                               "letter (like tidebreak-dispatcher).")
        with self.lock:
            if name in self.registry:
                raise ProgramError(f"There's already a program called {name}.")
            e = {"name": name, "description": str(description).strip()[:500], "permissions": ["reports"], "kind": kind,
                 "created": time.time(), "last_seen": None, "silent_after": 0, "silent": False,
                 "notify": "asks", "reports": [], "calls": []}
            key = self._new_key(e)
            self.registry[name] = e
            self._save()
        self.notify()
        return key

    def new_key(self, name: str) -> str:
        """A new key for the program; the old one stops working at once."""
        with self.lock:
            key = self._new_key(self.get(name))
            self._save()
        self.notify()
        return key

    def update(self, name: str, description: str | None = None, permissions: list | None = None,
               silent_after: int | None = None, notify: str | None = None) -> None:
        with self.lock:
            e = self.get(name)
            if description is not None:
                e["description"] = str(description).strip()[:500]
            if permissions is not None:
                if not isinstance(permissions, list) or any(p not in PERMISSIONS for p in permissions):
                    raise ProgramError(f"Permissions: some of {', '.join(PERMISSIONS)}.")
                e["permissions"] = sorted(set(permissions))
            if silent_after is not None:
                try:
                    minutes = int(silent_after)
                except (TypeError, ValueError):
                    raise ProgramError("Silent after: a number of minutes (0 = never tell me).") from None
                if not 0 <= minutes <= 7 * 24 * 60:
                    raise ProgramError("Silent after: between 0 (never tell me) and 10080 minutes (a week).")
                e["silent_after"] = minutes
                e["silent"] = False
            if notify is not None:
                if notify not in NOTIFY_MODES:
                    raise ProgramError(f"Notify: one of {', '.join(NOTIFY_MODES)}.")
                e["notify"] = notify
            self._save()
        self.notify()

    def remove(self, name: str) -> None:
        """The program, its key and its reports."""
        with self.lock:
            self.get(name)
            del self.registry[name]
            self.recent.pop(name, None)
            self._save()
            shutil.rmtree(self._dir(name), ignore_errors=True)
        self.notify()

    def mark_seen(self, name: str, report_id: str | None = None) -> None:
        """You looked at a report: it and the earlier ones stop counting as new."""
        with self.lock:
            for r in self.get(name)["reports"]:
                if r.get("seen") is False:
                    r["seen"] = True
                if r["id"] == report_id:
                    break
            self._save()
        self.notify()

    # --- what the programs call ------------------------------------------------------------

    def identify(self, key: str) -> str | None:
        """The program a key belongs to, or None."""
        parts = split_key(key)
        if parts is None:
            return None
        kid, secret = parts
        digest = _hash(secret)
        with self.lock:
            for name, e in self.registry.items():
                if hmac.compare_digest(e.get("key_id", ""), kid):
                    return name if hmac.compare_digest(e.get("key_hash", ""), digest) else None
        return None

    def allowed(self, name: str, permission: str) -> bool:
        return permission in self.get(name).get("permissions", [])

    def called(self, name: str, route: str, status: int) -> None:
        """Every call: it's alive (and back, if it was silent), and the call is listed."""
        came_back = False
        with self.lock:
            e = self.registry.get(name)
            if e is None:
                return
            e["last_seen"] = time.time()
            came_back = bool(e.get("silent"))
            e["silent"] = False
            e["calls"] = (e.get("calls", []) + [{"at": e["last_seen"], "route": route[:80], "status": status}])[-KEEP_CALLS:]
            self._save()
        if came_back:
            self.on_silent(name, {"back": True})
        self.notify()

    def receive_report(self, name: str, data: bytes) -> dict:
        """A report from the program: checked like a tool's (bad parts dropped and listed),
        its inline images checked by their bytes and kept, the oldest reports dropped."""
        now = time.time()
        with self.lock:
            e = self.get(name)
            times = self.recent.setdefault(name, collections.deque())
            while times and now - times[0] > 60:
                times.popleft()
            if len(times) >= RATE:
                raise ProgramError(f"At most {RATE} reports a minute.")
            times.append(now)
        report, problems = reports.parse(data, inline=True)
        if report is None:
            raise ProgramError("The report can't be shown: " + "; ".join(problems[:5]))
        inline = report.pop("inline", {})
        with self.lock:
            rid = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(now * 1000) % 1000:03d}"
            while any(r["id"] == rid for r in e["reports"]):
                rid = rid[:-3] + f"{(int(rid[-3:]) + 1) % 1000:03d}"
            folder = self._dir(name)
            folder.mkdir(parents=True, exist_ok=True)
            os.chmod(folder, 0o700)
            reports.attach_images(report, inline.get, folder / f"{rid}.images", problems)
            (folder / f"{rid}.report.json").write_text(json.dumps({"report": report, "problems": problems[:20]}),
                                                       encoding="utf-8")
            record = {"id": rid, "at": now, "title": report["title"], "status": report["status"],
                      "notify": report["notify"], "problems": len(problems),
                      "stats": reports.first_stats(report)[:4], "seen": False}
            e["reports"] = (e["reports"] + [record])[-self.keep:]
            keep = {r["id"] for r in e["reports"]}
            for old in folder.iterdir():
                rest = old.name.split(".")[0]
                if rest not in keep:
                    shutil.rmtree(old, ignore_errors=True) if old.is_dir() else old.unlink(missing_ok=True)
            self._save()
        self.notify()
        try:
            self.on_report(name, rid)
        except Exception as err:   # the notification is a nicety; the report is kept either way
            print(f"Warning: could not announce the report of {name}: {err}")
        return {"id": rid, "problems": problems[:20]}

    # --- what the panel shows --------------------------------------------------------------

    def result(self, name: str, report_id: str) -> dict:
        e = self.get(name)
        record = next((r for r in e["reports"] if r["id"] == report_id), None)
        if record is None:
            raise KeyError("No such report.")
        out = {"name": name, "title": name, "description": e["description"], "record": record,
               "report": None, "problems": [], "notify": e.get("notify", "asks")}
        try:
            out.update(json.loads((self._dir(name) / f"{report_id}.report.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            out["problems"] = ["The report is gone."]
        return out

    def image(self, name: str, report_id: str, n: int) -> tuple[bytes, str]:
        self.get(name)
        if not re.match(r"^[0-9]{8}-[0-9]{6}-[0-9]{3}$", report_id):
            raise KeyError("No such report.")
        try:
            data = (self._dir(name) / f"{report_id}.images" / str(int(n))).read_bytes()
        except (OSError, ValueError):
            raise KeyError("No such image.") from None
        kind = reports.image_type(data)
        if kind is None:
            raise KeyError("No such image.")
        return data, kind

    def view(self, name: str | None = None) -> list[dict] | dict:
        now = time.time()
        out = []
        with self.lock:
            for n, e in sorted(self.registry.items()):
                if name and n != name:
                    continue
                rs = e.get("reports", [])
                item = {"name": n, "description": e["description"], "permissions": e.get("permissions", []),
                        "kind": e.get("kind", "program"),
                        "created": e["created"], "key_id": e.get("key_id"), "key_made": e.get("key_made"),
                        "last_seen": e.get("last_seen"), "silent_after": e.get("silent_after", 0),
                        "silent": bool(e.get("silent")),
                        "silent_for": (now - e["last_seen"]) if e.get("silent") and e.get("last_seen") else None,
                        "notify": e.get("notify", "asks"), "last_report": rs[-1] if rs else None,
                        "unseen": sum(1 for r in rs if r.get("seen") is False)}
                if name:
                    item["reports"] = rs[::-1]
                    item["calls"] = e.get("calls", [])[::-1]
                out.append(item)
        if name:
            if not out:
                raise KeyError(f"There's no program called {name}.")
            return out[0]
        return out

    # --- the silent check ------------------------------------------------------------------

    def check_silent(self, now: float | None = None) -> list[str]:
        """Programs that haven't called for longer than their limit: told once, until they call again."""
        now = time.time() if now is None else now
        gone = []
        with self.lock:
            for name, e in self.registry.items():
                limit = e.get("silent_after", 0)
                if limit and e.get("last_seen") and not e.get("silent") and now - e["last_seen"] > limit * 60:
                    e["silent"] = True
                    gone.append(name)
            if gone:
                self._save()
        for name in gone:
            e = self.registry.get(name) or {}
            self.on_silent(name, {"back": False, "minutes": int((now - (e.get("last_seen") or now)) // 60)})
        if gone:
            self.notify()
        return gone

    def start(self) -> None:
        def loop():
            while True:
                time.sleep(30)
                try:
                    self.check_silent()
                except Exception as err:   # never let the check stop
                    print(f"Warning: the programs' silent check failed: {err}")
        threading.Thread(target=loop, daemon=True, name="uvmc-programs").start()
