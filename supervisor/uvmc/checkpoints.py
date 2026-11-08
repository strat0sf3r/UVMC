"""A copy of the agent's work folder before every task, so a task can be undone.

Copies are hard-linked against the previous one, so unchanged files cost no extra
space. They live outside the agent's reach (owned by the supervisor's user, and
written by root so file owners are kept).
"""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

from .config import Config
from .executor import root

ID = re.compile(r"^[0-9]{8}-[0-9]{6}(-[0-9]+)?$")


class Checkpoints:
    def __init__(self, cfg: Config, src: str | None = None, base: Path | None = None):
        self.src = (src or cfg.workdir).rstrip("/")
        self.base = base or cfg.state_dir / "checkpoints"
        self.keep = max(1, cfg.checkpoints_keep)

    def list(self) -> list[str]:
        try:
            return sorted(p.name for p in self.base.iterdir() if p.is_dir() and ID.match(p.name))
        except OSError:
            return []

    def create(self) -> str:
        cid = time.strftime("%Y%m%d-%H%M%S")
        existing = self.list()
        n = 1
        while (cid if n == 1 else f"{cid}-{n}") in existing:
            n += 1
        cid = cid if n == 1 else f"{cid}-{n}"
        cmd = ["rsync", "-a", "--delete"]
        if existing:
            cmd.append(f"--link-dest={self.base / existing[-1]}")
        cmd += [f"{self.src}/", f"{self.base / cid}/"]
        self.base.mkdir(parents=True, exist_ok=True)
        p = root(cmd, timeout=600)
        if p.returncode not in (0, 24):  # 24: files vanished while copying, fine
            raise RuntimeError(p.stderr.decode(errors="replace").strip() or "copy failed")
        self._prune()
        return cid

    def restore(self, cid: str) -> None:
        if not ID.match(cid) or cid not in self.list():
            raise KeyError("That checkpoint no longer exists.")
        p = root(["rsync", "-a", "--delete", f"{self.base / cid}/", f"{self.src}/"], timeout=600)
        if p.returncode != 0:
            raise RuntimeError(p.stderr.decode(errors="replace").strip() or "restore failed")

    def _prune(self) -> None:
        for old in self.list()[:-self.keep]:
            p = root(["rm", "-rf", "--", str(self.base / old)], timeout=120)
            if p.returncode != 0:
                shutil.rmtree(self.base / old, ignore_errors=True)
