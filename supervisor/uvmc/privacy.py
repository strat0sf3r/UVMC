"""Which model may see which files: three levels, projects, zones and marks.

    local   stays on your PC (the model on its graphics card)
    paid    a provider trusted by contract (the Anthropic API, your Claude plan, other paid APIs)
    open    free tiers, or anything that may log or train on what it gets

Models: each profile has a level (its ceiling). Files: the main work folder's top-level folders
are projects; everything at the top level that no project claims is the General project. A
project has a level, the default for its files; a subfolder can have its own (a zone, any level)
and a single file can be marked stricter than where it sits (never looser).

A conversation opens some projects and has a level (its model's, or lower). It sees the paths
of its projects whose level is at most its own: that is the one check (Scope.visible) used by
its copy, its bring-in and what it's told. Its "seen" level is the most private file it has
had in its copy: changes it brings into a less private place than that wait for your review.

Folders shared from the PC (/mnt/shared/<name>) are projects too, named "share:<name>": one
level for the whole share (they stay live, so nothing inside is filtered), local until you set
it. A conversation sees a share only if it opened it and its level allows; opening one counts
as having seen its level.

Levels are kept here, by the supervisor (projects.json), never in the folder itself.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

LEVELS = ("open", "paid", "local")          # least private first
RANK = {name: i for i, name in enumerate(LEVELS)}
LABELS = {"local": "local (stays on your PC)", "paid": "paid (trusted by contract)",
          "open": "open (no privacy expected)"}
GENERAL = ""                                # the General project: top-level things no project claims
GENERAL_LABEL = "General"
NAME = re.compile(r"^[^./\\\x00][^/\\\x00]{0,99}$")   # a top-level folder name; not hidden, no slashes
RESERVED = {".uvmc-merge"}
SHARE = "share:"                            # a project that is a folder shared from the PC
SHARE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")   # as shares.py


def is_share(name: str) -> bool:
    return name.startswith(SHARE)


def share_of(name: str) -> str:
    """'share:photos' -> 'photos'."""
    return name[len(SHARE):]


class PrivacyError(Exception):
    """Refused: the model or conversation may not see this."""


class NeedsConfirm(Exception):
    """Allowed, but only after you confirm (the message says what will happen)."""


def check(level: str) -> str:
    if level not in RANK:
        raise ValueError(f"'{level}' is not a privacy level (use {', '.join(LEVELS)}).")
    return level


def rank(level: str | None) -> int:
    return RANK.get(level or "open", 0)


def higher(a: str, b: str) -> str:
    return a if rank(a) >= rank(b) else b


def lower(a: str, b: str) -> str:
    return a if rank(a) <= rank(b) else b


def suggest(provider: str, base_url: str) -> str:
    """The level UVMC suggests for a model profile with none set."""
    if "{pc}" in base_url or base_url.startswith(("http://localhost", "http://127.0.0.1")):
        return "local"
    if provider in ("anthropic", "claude-code"):
        return "paid"
    return "open"


def project_name(rel: str) -> str:
    """The project a path (relative to the main folder) belongs to, by its first part only."""
    return rel.replace("\\", "/").split("/", 1)[0]


class Projects:
    """The levels you set: per project, per zone (a folder) and per file (a mark)."""

    def __init__(self, path: Path, general_level: str = "local"):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.data = {"projects": {}, "general": {"level": check(general_level), "asked": False},
                     "zones": {}, "marks": {}, "shares": {}}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            for k in self.data:
                if isinstance(loaded.get(k), dict):
                    self.data[k] = loaded[k]
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    # --- reading ---------------------------------------------------------------------------

    def names(self) -> list[str]:
        return sorted(self.data["projects"])

    def has(self, name: str) -> bool:
        """Shares count as known whatever their state: the controller checks they are shared now."""
        if is_share(name):
            return bool(SHARE_NAME.match(share_of(name)))
        return name == GENERAL or name in self.data["projects"]

    def project_of(self, rel: str) -> str:
        first = project_name(rel)
        return first if first in self.data["projects"] else GENERAL

    def project_level(self, name: str) -> str:
        if name == GENERAL:
            return self.data["general"]["level"]
        if is_share(name):
            return self.share_level(share_of(name))
        return self.data["projects"][name]["level"]

    def share_level(self, share: str) -> str:
        """A shared folder's level: local until you set it."""
        return (self.data["shares"].get(share) or {}).get("level", "local")

    def level(self, rel: str, with_mark: bool = True) -> str:
        """A path's level: its project's, then the deepest zone above it, then its own mark
        (which can only make it stricter)."""
        rel = rel.replace("\\", "/").strip("/")
        lvl = self.project_level(self.project_of(rel))
        parts = rel.split("/")
        zones = self.data["zones"]
        for i in range(1, len(parts) + 1):
            z = zones.get("/".join(parts[:i]))
            if z:
                lvl = z
        mark = self.data["marks"].get(rel) if with_mark else None
        return higher(lvl, mark) if mark else lvl

    def lowest(self, name: str) -> str:
        """The least private level anywhere in a project (what a conversation needs to open it)."""
        lvl = self.project_level(name)
        if is_share(name):
            return lvl
        for z, zl in self.data["zones"].items():
            if self.project_of(z) == name:
                lvl = lower(lvl, zl)
        return lvl

    def describe(self) -> dict:
        projects = [{"name": GENERAL, "label": GENERAL_LABEL, "level": self.data["general"]["level"],
                     "asked": bool(self.data["general"].get("asked")), "lowest": self.lowest(GENERAL)}]
        for name, p in sorted(self.data["projects"].items()):
            projects.append({"name": name, "label": name, "level": p["level"], "lowest": self.lowest(name),
                             "scratch": bool(p.get("scratch")), "created": p.get("created")})
        shares = {n: {"level": s.get("level", "local"), "asked": bool(s.get("asked"))}
                  for n, s in sorted(self.data["shares"].items())}
        return {"levels": list(LEVELS), "labels": LABELS, "projects": projects, "shares": shares,
                "zones": dict(sorted(self.data["zones"].items())),
                "marks": dict(sorted(self.data["marks"].items()))}

    # --- changing (you, from the panel; and new projects) -----------------------------------

    def add(self, name: str, level: str, scratch: bool = False, by: str = "") -> None:
        if not NAME.match(name) or name in RESERVED or is_share(name):
            raise ValueError(f"'{name}' can't be a project name (a folder name, not starting with a dot).")
        with self.lock:
            if name not in self.data["projects"]:
                self.data["projects"][name] = {"level": check(level), "created": time.time(),
                                               "scratch": scratch, "by": by}
                self._save()

    def remove(self, name: str) -> None:
        with self.lock:
            if self.data["projects"].pop(name, None) is not None:
                for table in ("zones", "marks"):
                    self.data[table] = {k: v for k, v in self.data[table].items() if project_name(k) != name}
                self._save()

    def set_level(self, name: str, level: str) -> None:
        check(level)
        with self.lock:
            if is_share(name):
                if not SHARE_NAME.match(share_of(name)):
                    raise KeyError(f"There's no shared folder called '{share_of(name)}'.")
                self.data["shares"][share_of(name)] = {"level": level, "asked": True}
            elif name == GENERAL:
                self.data["general"] = {"level": level, "asked": True}
            elif name in self.data["projects"]:
                self.data["projects"][name]["level"] = level
                self.data["projects"][name]["scratch"] = False
            else:
                raise KeyError(f"There's no project called '{name}'.")
            self._save()

    def set_zone(self, rel: str, level: str | None) -> None:
        """A folder inside a project with its own level (None: back to its project's)."""
        rel = _clean(rel)
        if "/" not in rel and self.project_of(rel) != GENERAL:
            raise ValueError("That's a whole project: change the project's level instead.")
        with self.lock:
            if level is None:
                self.data["zones"].pop(rel, None)
            else:
                self.data["zones"][rel] = check(level)
            self._save()

    def set_mark(self, rel: str, level: str | None) -> None:
        """A single file marked stricter than where it sits (None: no mark)."""
        rel = _clean(rel)
        with self.lock:
            old = self.data["marks"].pop(rel, None)
            if level is not None:
                around = self.level(rel)            # without its mark
                if rank(check(level)) <= rank(around):
                    if old:
                        self.data["marks"][rel] = old
                    raise ValueError(f"A mark can only make a file stricter: it is already {around} where it is.")
                self.data["marks"][rel] = level
            self._save()

    def label(self, name: str) -> str:
        if name == GENERAL:
            return GENERAL_LABEL
        return f"the shared folder {share_of(name)}" if is_share(name) else name

    def loosening(self, name: str, level: str) -> bool:
        return rank(level) < rank(self.project_level(name))


def _clean(rel: str) -> str:
    rel = rel.replace("\\", "/").strip().strip("/")
    parts = rel.split("/")
    if not rel or any(p in ("", ".", "..") for p in parts) or parts[0] in RESERVED:
        raise ValueError(f"'{rel}' is not a valid path in the main folder.")
    return rel


class Scope:
    """What one conversation (or job) may see: its projects, at its level."""

    def __init__(self, projects: Projects, opened: list[str], level: str, seen: str = "open",
                 given: set[str] | None = None):
        self.projects = projects
        self.opened = set(opened)
        self.level = level
        self.seen = seen
        self.given = set(given or ())       # files you gave this conversation one by one

    def visible(self, rel: str) -> bool:
        """The one check: in an opened project, and no more private than the conversation
        (or a file you gave it yourself)."""
        if rel in self.given:
            return True
        if self.projects.project_of(rel) not in self.opened:
            return False
        return rank(self.projects.level(rel)) <= rank(self.level)

    def shares(self, available: list[str] | None = None) -> list[str]:
        """The shared folders it may see (opened, level allowing), by name; available: the ones
        shared right now."""
        out = []
        for n in sorted(self.opened):
            if is_share(n) and rank(self.projects.share_level(share_of(n))) <= rank(self.level):
                if available is None or share_of(n) in available:
                    out.append(share_of(n))
        return out

    def level_of(self, rel: str) -> str:
        return self.projects.level(rel)

    def held(self, rel: str) -> bool:
        """A change here would move what the conversation has seen to a less private place."""
        return rank(self.projects.level(rel)) < rank(self.seen)
