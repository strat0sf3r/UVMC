"""Every event of a session goes to a JSON Lines file and, live, to any listener.

One file per session (conversation). Each event carries a sequence number so the
panel can join a running session, replay the file and continue live without gaps
or duplicates.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

Listener = Callable[[dict], None]


class RunLog:
    def __init__(self, log_dir: Path, listener: Listener | None = None):
        self.listener = listener
        self.seq = 0
        self._lock = threading.Lock()
        self.name = f"{datetime.now():%Y%m%d-%H%M%S}"
        self.path: Path | None = None
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            path = log_dir / f"{self.name}.jsonl"
            n = 1
            while path.exists():  # two sessions in the same second
                n += 1
                path = log_dir / f"{self.name}-{n}.jsonl"
            path.touch()
            self.path, self.name = path, path.stem
        except OSError as e:
            print(f"(logging disabled: {e})")

    def event(self, kind: str, **data) -> dict:
        with self._lock:
            self.seq += 1
            record = {"seq": self.seq, "session": self.name, "t": round(time.time(), 3), "kind": kind, **data}
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        if self.listener:
            self.listener(json.loads(json.dumps(record, default=str)))
        return record

    def read(self) -> list[dict]:
        return read_log(self.path) if self.path else []


def read_log(path: Path) -> list[dict]:
    events = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass  # a half-written last line
    except OSError:
        pass
    return events


class ChildLog:
    """A worker's view of its parent's log: same file and live stream, every event tagged."""

    def __init__(self, base: RunLog, **tags):
        self.base, self.tags = base, tags

    @property
    def name(self) -> str:
        return self.base.name

    def event(self, kind: str, **data) -> dict:
        return self.base.event(kind, **{**data, **self.tags})

    def read(self) -> list[dict]:
        return []
