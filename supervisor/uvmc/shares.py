"""Folders from your computer, shared with the agent read-only, with approved writes.

Each folder listed in shares.txt (on your computer) is mounted into the VM twice:

    /srv/uvmc-host/<name>   writable, only the supervisor can reach it (folder mode 700)
    /mnt/shared/<name>      what the agent sees: read-only

The agent reads freely. To change or add a file it prepares the new version in its
work folder and asks with the `update_shared_file` tool. You approve it in the panel;
then the supervisor (never the agent) backs up the original into
<name>/_uvmc_backups/<time>/... on your computer, writes the new file, and checks it.
Deleting files on your computer is not offered at all.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .executor import root

RW_ROOT = Path(os.environ.get("UVMC_SHARES_RW", "/srv/uvmc-host"))
VIEW_ROOT = Path(os.environ.get("UVMC_SHARES_VIEW", "/mnt/shared"))
# Each read-only view sits in a folder of its own, /srv/uvmc-views/<name>/files, which /mnt/shared/<name>
# links to. That folder is the supervisor's (mode 700); an agent user gets in only through an ACL entry
# the supervisor sets when one of its conversations opens the share (privacy.py), so a share is closed
# to every conversation that didn't open it or may not see it at its level.
HOLD_ROOT = Path(os.environ.get("UVMC_SHARES_HOLD", "/srv/uvmc-views"))
BACKUPS = "_uvmc_backups"
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
MAX_SIZE = 200 * 1024 * 1024
TEXT_DIFF_LIMIT = 512 * 1024

SPEC = {
    "name": "update_shared_file",
    "description": (
        "Change or add a file in one of the user's shared folders (the read-only folders under "
        "/mnt/shared). Prepare the new version in your work folder first, then call this; the user "
        "approves each change, and the original is backed up. Files can't be deleted this way."),
    "input_schema": {
        "type": "object",
        "properties": {
            "share": {"type": "string", "description": "The shared folder's name (a folder in /mnt/shared)."},
            "path": {"type": "string", "description": "Where the file goes, relative to that folder."},
            "source": {"type": "string", "description": "Your new version, a file in your home or work folder."},
            "reason": {"type": "string", "description": "What changed and why, in one sentence."},
        },
        "required": ["share", "path", "source", "reason"],
    },
}


class ShareError(Exception):
    pass


@dataclass
class Update:
    id: str
    share: str
    path: str
    backup: str | None     # backup path relative to the share, None for a new file
    size: int
    sha256: str
    at: float


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Shares:
    def __init__(self, agent_home: str = "/home/agent", rw_root: Path = RW_ROOT, view_root: Path = VIEW_ROOT,
                 hold_root: Path = HOLD_ROOT):
        self.rw_root, self.view_root, self.hold_root = rw_root, view_root, hold_root
        self.agent_home = agent_home.rstrip("/")
        self.updates: dict[str, Update] = {}
        self.access: dict[str, frozenset[str]] = {}   # what was last set, per share

    def set_access(self, access: dict[str, set[str]]) -> list[str]:
        """Lets exactly these users into each share's folder (setfacl --set, replacing what was
        there). A share's folder that isn't there (outside the VM, or not synced yet) is skipped;
        its view isn't reachable then anyway. Returns the shares changed."""
        changed = []
        for name, users in access.items():
            hold = self.hold_root / name
            want = frozenset(users)
            if self.access.get(name) == want or not NAME.match(name) or not hold.is_dir():
                continue
            spec = ",".join(["u::rwx", "g::---", "o::---"] + [f"u:{u}:--x" for u in sorted(want)])
            p = subprocess.run(["setfacl", "--set", spec, str(hold)], capture_output=True, timeout=15)
            if p.returncode != 0:
                raise ShareError(f"setfacl on {hold} failed: {p.stderr.decode(errors='replace').strip()}")
            self.access[name] = want
            changed.append(name)
        return changed

    def names(self) -> list[str]:
        try:
            return sorted(p.name for p in self.rw_root.iterdir() if p.is_dir() and NAME.match(p.name))
        except OSError:
            return []

    def describe(self) -> list[dict]:
        return [{"name": n, "view": str(self.view_root / n)} for n in self.names()]

    # --- checks ------------------------------------------------------------------

    def target(self, share: str, rel: str) -> Path:
        if share not in self.names():
            raise ShareError(f"There's no shared folder called '{share}'. Shared folders: {', '.join(self.names()) or 'none'}.")
        rel = rel.strip().lstrip("/")
        parts = Path(rel).parts
        if not rel or ".." in parts or (parts and parts[0] == BACKUPS):
            raise ShareError(f"'{rel}' is not a valid path inside the shared folder.")
        base = (self.rw_root / share).resolve()
        target = (base / rel).resolve()
        if base not in target.parents:
            raise ShareError(f"'{rel}' points outside the shared folder.")
        return target

    def source(self, run_as_agent, path: str) -> Path:
        """Resolves the agent's file as the agent sees it; it must be inside the agent's home."""
        p = run_as_agent('realpath -e -- "$1"', path, timeout=15)
        resolved = p.stdout.decode(errors="replace").strip()
        if p.returncode != 0 or not resolved:
            raise ShareError(f"Your file '{path}' doesn't exist.")
        if not (resolved + "/").startswith(self.agent_home + "/"):
            raise ShareError("The new version must be a file in your home or work folder.")
        return Path(resolved)

    # --- preview for the approval card -----------------------------------------------

    def preview(self, src: Path, target: Path) -> dict:
        size = self._agent_file_size(src)
        if size > MAX_SIZE:
            raise ShareError(f"The new version is {size // (1024 * 1024)} MB; the limit is {MAX_SIZE // (1024 * 1024)} MB.")
        info = {"new_size": size, "exists": target.exists(), "old_size": target.stat().st_size if target.exists() else None}
        if size <= TEXT_DIFF_LIMIT and (not info["exists"] or info["old_size"] <= TEXT_DIFF_LIMIT):
            new = self._agent_file_bytes(src)
            old = target.read_bytes() if info["exists"] else b""
            if b"\x00" not in new[:8000] and b"\x00" not in old[:8000]:
                import difflib
                diff = difflib.unified_diff(old.decode(errors="replace").splitlines(), new.decode(errors="replace").splitlines(),
                                            "original", "new version", lineterm="", n=2)
                text = "\n".join(diff)
                info["diff"] = text[:20000] + ("\n[... diff shortened ...]" if len(text) > 20000 else "")
        return info

    @staticmethod
    def _agent_file_size(src: Path) -> int:
        p = root(["stat", "-c", "%s", str(src)], timeout=15)
        if p.returncode != 0:
            raise ShareError("Couldn't read your file.")
        return int(p.stdout.decode().strip() or 0)

    @staticmethod
    def _agent_file_bytes(src: Path) -> bytes:
        p = root(["cat", "--", str(src)], timeout=60)
        if p.returncode != 0:
            raise ShareError("Couldn't read your file.")
        return p.stdout

    # --- the write itself ----------------------------------------------------------

    def write(self, share: str, rel: str, src: Path, target: Path) -> Update:
        base = self.rw_root / share
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_rel = None
        if target.exists():
            backup_rel = f"{BACKUPS}/{stamp}/{rel.strip().lstrip('/')}"
            backup = base / backup_rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.uvmc-{uuid.uuid4().hex[:6]}")
        try:
            with tmp.open("wb") as out:
                p = subprocess.run(["sudo", "-n", "cat", "--", str(src)] if os.environ.get("UVMC_NO_SUDO") != "1"
                                   else ["cat", "--", str(src)], stdout=out, stderr=subprocess.PIPE, timeout=600)
            if p.returncode != 0:
                raise ShareError(f"Couldn't copy your file: {p.stderr.decode(errors='replace').strip()}")
            expected = hashlib.sha256(self._agent_file_bytes(src)).hexdigest()
            got = _sha256_file(tmp)
            if got != expected:
                raise ShareError("The copy didn't match the new version; nothing was changed.")
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                tmp.unlink()
        upd = Update(uuid.uuid4().hex[:10], share, rel.strip().lstrip("/"), backup_rel,
                     target.stat().st_size, _sha256_file(target), time.time())
        self.updates[upd.id] = upd
        self.refresh_views()
        return upd

    @staticmethod
    def refresh_views() -> None:
        """Copy-mode views catch up right away instead of within 30 seconds."""
        sync = Path("/usr/local/sbin/uvmc-shares-sync")
        if sync.exists():
            try:
                root([str(sync)], timeout=120)
            except Exception:
                pass

    def restore(self, update_id: str) -> str:
        upd = self.updates.get(update_id)
        if upd is None:
            raise KeyError("That change is not known (it may be from before the supervisor restarted).")
        target = self.target(upd.share, upd.path)
        base = self.rw_root / upd.share
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if target.exists():  # keep what's there now, too
            keep = base / BACKUPS / f"{stamp}-before-restore" / upd.path
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, keep)
        if upd.backup:
            shutil.copy2(base / upd.backup, target)
            self.refresh_views()
            return f"Restored {upd.share}/{upd.path} to how it was before the change."
        if target.exists():
            target.unlink()  # it was a new file: undoing means removing it (a copy was kept above)
        self.refresh_views()
        return f"Removed {upd.share}/{upd.path}, which the change had created (a copy is in {BACKUPS})."
