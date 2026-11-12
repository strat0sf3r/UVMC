"""The lock: the VM closed to the PC, and only the model on the PC used.

Everything on the PC (Multipass exec, shell, transfer and mount, UVMC's scripts, any program
or AI agent) comes into the VM by SSH as ubuntu with Multipass's key. setup.sh points SSH at a
copy of that key in memory (/run/uvmc-ssh/ubuntu.keys), refilled at every boot; locking
empties it (/usr/local/sbin/uvmc-lock on) and ends the open sessions, except the ones serving
a shared folder. While locked, every request to a model must go to the model on the PC
(Controller checks it before each task and job). The panel and that model keep working: the
VM calls the PC, never the other way.

No PIN: the lock keeps programs out, not you. Locking also gives the panel a new token
(service.py, PanelToken), handed only to the browser that clicked Lock: every token a program on
the PC read before stops working, so only that open panel can unlock (or a restart).
Restarting the VM always unlocks it (the way back if the panel can't): the supervisor notices
and says so.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .executor import no_sudo, root

KEYS = Path("/run/uvmc-ssh/ubuntu.keys")
HELPER = "/usr/local/sbin/uvmc-lock"


class LockError(Exception):
    pass


class Lock:
    def __init__(self, state_dir: Path, keys: Path = KEYS):
        self.file = Path(state_dir) / "lock.json"
        self.keys = keys
        self._mutex = threading.Lock()
        self.released_by_restart = False
        self._fake = False   # outside the VM (tests): the lock's state, in memory
        data = self._load()
        if data.get("locked") and not self.locked:   # it was locked, and now the VM is open again
            self.released_by_restart = True
            data["locked"] = False
            self._save(data)

    # --- state -------------------------------------------------------------------------------

    def _load(self) -> dict:
        try:
            return json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.file)

    @property
    def available(self) -> bool:
        """setup.sh made the key file (an older setup: run update)."""
        return no_sudo() or self.keys.exists()

    @property
    def locked(self) -> bool:
        if no_sudo():
            return self._fake
        try:
            return self.keys.exists() and self.keys.stat().st_size == 0
        except OSError:
            return False

    def view(self) -> dict:
        return {"available": self.available, "locked": self.locked,
                "released_by_restart": self.released_by_restart}

    # --- locking -----------------------------------------------------------------------------

    def set(self, on: bool) -> None:
        """Locks (on) or unlocks the VM."""
        with self._mutex:
            if not self.available:
                raise LockError("The lock isn't set up in the VM yet: run update on your PC.")
            self._apply(on)
            data = self._load()
            data.pop("pin", None)   # left by 0.19.3-0.22.2, which had one
            data["locked"] = on
            self._save(data)
            self.released_by_restart = False

    def _apply(self, on: bool) -> None:
        if no_sudo():
            self._fake = on
            return
        p = root([HELPER, "on" if on else "off"], timeout=30)
        if p.returncode != 0:
            raise LockError(f"The VM couldn't be {'locked' if on else 'unlocked'}: "
                            + (p.stderr.decode(errors="replace").strip() or f"exit code {p.returncode}"))
        if self.locked != on:
            raise LockError(f"The VM didn't become {'locked' if on else 'unlocked'}.")

    def forget_restart(self) -> None:
        self.released_by_restart = False
