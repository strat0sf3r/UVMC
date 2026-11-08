"""Reports every program the agent starts, as recorded by Linux's audit system.

The VM's audit rules record each `execve` by the agent user under the key
"uvmc-agent". This follows /var/log/audit/audit.log and turns those records into
one event per program: what ran, with which arguments, in which folder.
Programs started inside scripts are included, which a list of tool calls can't show.
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Callable

AUDIT_LOG = Path(os.environ.get("UVMC_AUDIT_LOG", "/var/log/audit/audit.log"))
KEY = "uvmc-agent"
FIELD = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')
SERIAL = re.compile(r"msg=audit\(([\d.]+):(\d+)\)")


def _value(raw: str) -> str:
    if raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    if raw != "(null)" and len(raw) % 2 == 0 and re.fullmatch(r"[0-9A-Fa-f]+", raw):
        try:
            return bytes.fromhex(raw).decode("utf-8", errors="replace")
        except ValueError:
            pass
    return raw


def parse_fields(line: str) -> dict[str, str]:
    line = line.split("\x1d", 1)[0]  # drop the "enriched" part some setups add
    return {k: v for k, v in FIELD.findall(line)}


class ExecParser:
    """Feed it audit.log lines; it calls `emit` once per finished execve event."""

    def __init__(self, emit: Callable[[dict], None]):
        self.emit = emit
        self.records: dict[str, dict] = {}

    def feed(self, line: str) -> None:
        m = SERIAL.search(line)
        if not m:
            return
        ts, serial = float(m.group(1)), m.group(2)
        kind = line.split(" ", 1)[0].removeprefix("type=")
        rec = self.records.setdefault(serial, {"t": ts, "args": {}})
        fields = parse_fields(line[m.end():])
        if kind == "SYSCALL":
            rec["key"] = _value(fields.get("key", ""))
            rec["pid"] = int(fields.get("pid", 0) or 0)
            rec["ppid"] = int(fields.get("ppid", 0) or 0)
            rec["exe"] = _value(fields.get("exe", ""))
            rec["ok"] = fields.get("success", "yes") == "yes"
        elif kind == "EXECVE":
            for k, v in fields.items():
                if re.fullmatch(r"a\d+", k):
                    rec["args"][int(k[1:])] = _value(v)
        elif kind == "CWD":
            rec["cwd"] = _value(fields.get("cwd", ""))
        elif kind == "EOE":
            self._finish(serial)
        if len(self.records) > 500:  # never let half-seen events pile up
            for s in list(self.records)[:250]:
                self.records.pop(s, None)

    def _finish(self, serial: str) -> None:
        rec = self.records.pop(serial, None)
        if not rec or rec.get("key") != KEY:
            return
        args = [rec["args"][i] for i in sorted(rec["args"])]
        self.emit({"t": rec["t"], "pid": rec.get("pid"), "ppid": rec.get("ppid"), "exe": rec.get("exe", ""),
                   "args": args, "cwd": rec.get("cwd", ""), "ok": rec.get("ok", True)})


class ExecWatcher:
    """Follows the audit log in a background thread (also across log rotation)."""

    def __init__(self, emit: Callable[[dict], None], path: Path = AUDIT_LOG):
        self.path = path
        self.parser = ExecParser(emit)
        self.available = False
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="uvmc-audit", daemon=True)

    def start(self) -> "ExecWatcher":
        self.thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        f = None
        inode = None
        while not self._stop.is_set():
            if f is None:
                try:
                    f = self.path.open("rb")
                    st = os.fstat(f.fileno())
                    inode = st.st_ino
                    if not self.available:
                        f.seek(0, os.SEEK_END)  # first open: only new activity
                    self.available = True
                except OSError:
                    self.available = False
                    f = None
                    self._stop.wait(5)
                    continue
            line = f.readline()
            if line:
                if line.endswith(b"\n"):
                    self.parser.feed(line.decode("utf-8", errors="replace"))
                else:  # half-written line: read it again in a moment
                    f.seek(f.tell() - len(line))
                    time.sleep(0.1)
                continue
            try:
                st = self.path.stat()
                if st.st_ino != inode or st.st_size < f.tell():  # rotated
                    f.close()
                    f = None
                    continue
            except OSError:
                pass
            self._stop.wait(0.3)


class ExecFilter:
    """Leaves out the supervisor's own plumbing so the log shows what the agent ran.

    Each tool call starts `env ... timeout ... bash -c 'cd "<work>" && ...'` and, for
    run_command, `bash -lc "<command>"` (already shown as the tool call). The panel's
    file browser runs helpers marked "uvmc-panel"; those and their children are hidden.
    """

    def __init__(self, workdir: str = ""):
        self.prefix = 'cd "'   # every tool call starts in its conversation's folder
        self.hidden: dict[int, None] = {}  # pids whose children are panel helpers

    def _hide(self, pid) -> None:
        if pid:
            self.hidden[pid] = None
            if len(self.hidden) > 5000:
                for old in list(self.hidden)[:2500]:
                    self.hidden.pop(old, None)

    def keep(self, e: dict) -> bool:
        args = e.get("args") or []
        name = os.path.basename(args[0]) if args else os.path.basename(e.get("exe", ""))
        if e.get("ppid") in self.hidden:
            self._hide(e.get("pid"))
            return False
        if name == "env" and any(a.startswith("HTTP_PROXY=") for a in args):
            return False
        if name == "timeout" and "-k" in args[1:3]:
            return False
        if name == "bash" and len(args) > 2 and args[1] == "-c" and args[2].startswith(self.prefix) and '" && ' in args[2]:
            if "uvmc-panel" in args[2]:
                self._hide(e.get("pid"))
            return False
        if name == "bash" and args[1:2] == ["-lc"]:
            return False
        return True
