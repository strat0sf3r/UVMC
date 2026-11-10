"""The lock: the VM closed to the PC, and only the model on the PC used.

Everything on the PC (Multipass exec, shell, transfer and mount, UVMC's scripts, any program
or AI agent) comes into the VM by SSH as ubuntu with Multipass's key. setup.sh points SSH at a
copy of that key in memory (/run/uvmc-ssh/ubuntu.keys), refilled at every boot; locking
empties it (/usr/local/sbin/uvmc-lock on) and ends the open sessions, except the ones serving
a shared folder. While locked, every request to a model must go to the model on the PC
(Controller checks it before each task and job). The panel and that model keep working: the
VM calls the PC, never the other way.

Locking and unlocking both need a PIN you choose the first time, kept only as a scrypt hash.
Wrong PINs are slowed down: after 3 in a row, each new try waits longer (up to 5 minutes).
Restarting the VM always unlocks it (the way back if the panel can't): the supervisor notices
and says so.

Until the supervisor has its own user (design.md 7o), anything already inside the VM as
ubuntu or root could change the PIN's file; the lock keeps new programs from the PC out.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

from .executor import no_sudo, root

KEYS = Path("/run/uvmc-ssh/ubuntu.keys")
HELPER = "/usr/local/sbin/uvmc-lock"
PIN_MIN, PIN_MAX = 4, 64


class LockError(Exception):
    pass


class Lock:
    def __init__(self, state_dir: Path, keys: Path = KEYS):
        self.file = Path(state_dir) / "lock.json"
        self.keys = keys
        self._mutex = threading.Lock()
        self.fails = 0
        self.wait_until = 0.0
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

    @property
    def pin_set(self) -> bool:
        return bool(self._load().get("pin"))

    def view(self) -> dict:
        return {"available": self.available, "locked": self.locked, "pin_set": self.pin_set,
                "released_by_restart": self.released_by_restart,
                "wait": max(0, round(self.wait_until - time.time()))}

    # --- the PIN -----------------------------------------------------------------------------

    @staticmethod
    def _hash(pin: str, salt: bytes) -> str:
        return hashlib.scrypt(pin.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32).hex()

    def _check(self, pin: str, data: dict) -> None:
        now = time.time()
        if now < self.wait_until:
            raise LockError(f"Too many wrong PINs: try again in {round(self.wait_until - now)} seconds.")
        stored = data.get("pin") or {}
        good = hmac.compare_digest(self._hash(pin, bytes.fromhex(stored["salt"])), stored["hash"])
        if not good:
            self.fails += 1
            if self.fails >= 3:
                self.wait_until = now + min(300, 15 * 2 ** (self.fails - 3))
            raise LockError("Wrong PIN.")
        self.fails, self.wait_until = 0, 0.0

    # --- locking -----------------------------------------------------------------------------

    def set(self, on: bool, pin: str) -> None:
        """Locks (on) or unlocks with the PIN. The first lock sets the PIN."""
        pin = str(pin or "")
        with self._mutex:
            if not self.available:
                raise LockError("The lock isn't set up in the VM yet: run update on your PC.")
            data = self._load()
            if not data.get("pin"):
                if not on:
                    raise LockError("No PIN is set yet: lock first, which sets it.")
                if not PIN_MIN <= len(pin) <= PIN_MAX:
                    raise LockError(f"Choose a PIN of {PIN_MIN} to {PIN_MAX} characters.")
                salt = secrets.token_bytes(16)
                data["pin"] = {"salt": salt.hex(), "hash": self._hash(pin, salt)}
            else:
                self._check(pin, data)
            self._apply(on)
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
