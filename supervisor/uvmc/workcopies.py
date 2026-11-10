"""Each conversation works in its own copy of the work folder; you bring its changes in.

    /home/agent/work            the main work folder: the main version
    /home/agent/conv/<id>       a conversation's copy, owned by its own user (agent1, agent2, ...)
    <state>/convs/<id>/base     the main folder as it was when the copy was last in step with it

Nothing a conversation does touches the main folder. "Bring into the main folder" compares
three versions of every file: the base, the conversation's copy and the main folder now.

- Only the conversation changed a file: its version goes into the main folder.
- Only the main folder changed (another conversation brought something in): it goes into the
  conversation's copy, so the copy is up to date afterwards.
- Both changed a text file in different places: the changes are merged line by line.
- Both changed the same lines, or a file that isn't text: a conflict. You pick which version
  stays, or let the agent merge them: it gets the other version next to its own, in
  .uvmc-merge/, and you bring its result in like any other change.

Afterwards the base is the main folder as it is now (except for conflicts left open). Undo
works on the conversation's copy only; the main folder gets its own undo point before every
bring-in.

With a scope (privacy.py), the copy and the base hold only what the conversation may see, and
the comparison ignores everything else. A change to a path it may not see is "blocked" (never
brought in); a change to a place less private than what it has seen is "held" until you
release it.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .checkpoints import Checkpoints
from .executor import no_sudo, root

MERGE_DIR = ".uvmc-merge"
CONTEXT_DIR = ".uvmc-context"     # what UVMC hands a conversation to read (another's transcript)
SKIP_DIRS = {MERGE_DIR, CONTEXT_DIR}


class SyncError(Exception):
    pass


def _is_text(data: bytes) -> bool:
    if b"\x00" in data[:8000]:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _read(p: Path) -> bytes:
    """A file's bytes; through root when the agent made it unreadable for others (mode 600)."""
    try:
        return p.read_bytes()
    except PermissionError:
        r = root(["cat", "--", str(p)], timeout=120)
        if r.returncode != 0:
            raise
        return r.stdout


def _files(top: Path) -> dict[str, Path]:
    """Every file under top (not .uvmc-merge). A folder the supervisor can't read (the agent
    made it private) is listed through root, never silently left out: a missing file would
    look like a deleted one."""
    out: dict[str, Path] = {}
    if not top.is_dir():
        return out

    def fail(err):
        raise err
    try:
        for dirpath, dirnames, filenames in os.walk(top, onerror=fail):
            rel_dir = os.path.relpath(dirpath, top)
            if rel_dir == ".":
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for f in filenames:
                p = Path(dirpath) / f
                if p.is_file() and not p.is_symlink():
                    out[os.path.normpath(os.path.join(rel_dir, f))] = p
        return out
    except PermissionError:
        r = root(["find", str(top), "-type", "f", "-printf", "%P\\0"], timeout=300)
        if r.returncode != 0:
            raise SyncError(f"Could not list {top}: {r.stderr.decode(errors='replace').strip()}") from None
        return {rel: top / rel for rel in r.stdout.decode(errors="surrogateescape").split("\0")
                if rel and rel.split("/", 1)[0] not in SKIP_DIRS}


def _dirs(top: Path) -> list[str]:
    """Every folder under top (not .uvmc-merge), relative to it."""
    out = []
    for dirpath, dirnames, _ in os.walk(top):
        rel_dir = os.path.relpath(dirpath, top)
        if rel_dir == ".":
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for d in dirnames:
            out.append(os.path.normpath(os.path.join(rel_dir, d)))
    return out


class WorkCopies:
    def __init__(self, cfg):
        self.cfg = cfg
        self.main = Path(cfg.workdir)
        self.root = Path(cfg.conv_root)
        # On the same disk as the copies, so the bases are hard links; only the supervisor reads it.
        self.state = Path(cfg.conv_root) / ".state"
        self.main_undo = Checkpoints(cfg)              # the main folder, before each bring-in
        self._hash_cache: dict[tuple, str] = {}

    # --- places --------------------------------------------------------------------------

    def copy_dir(self, cid: str) -> Path:
        return self.root / cid

    def base_dir(self, cid: str) -> Path:
        return self.state / cid / "base"

    def checkpoints(self, cid: str) -> Checkpoints:
        return Checkpoints(self.cfg, str(self.copy_dir(cid)), self.state / cid / "checkpoints")

    def exists(self, cid: str) -> bool:
        return self.copy_dir(cid).is_dir()

    def _owner(self, user: str | None) -> list[str]:
        """install's owner options, or none outside the VM (tests run as you)."""
        if no_sudo() or not user:
            return []
        return ["-o", user, "-g", self.cfg.user]

    def _close_top(self, top: Path) -> None:
        """A copy's own folder belongs to the supervisor's group, not the agents': no other
        conversation or worker can look inside (its files keep the agent's group)."""
        if not no_sudo():
            self._run(["chgrp", self.cfg.files_group, str(top)])
            self._run(["chmod", "750", str(top)])

    def _rsync_visible(self, dst: Path, scope, what: str, link: bool = False) -> None:
        """Copies the main folder's files that scope may see (all of them without a scope)."""
        link_opt = [f"--link-dest={self.main}"] if link else []
        if scope is None:
            self._run(["rsync", "-a", *(["--delete"] if not link else []), *link_opt,
                       f"{self.main}/", f"{dst}/"], what)
            return
        names = [rel for rel in _files(self.main) if scope.visible(rel)]
        names += [rel for rel in _dirs(self.main) if scope.visible(rel)]   # empty folders too, as before
        if not names:
            return
        with tempfile.NamedTemporaryFile("wb", delete=False, dir=str(self.state) if self.state.exists() else None) as f:
            f.write(b"\0".join(n.encode(errors="surrogateescape") for n in names) + b"\0")
        try:
            os.chmod(f.name, 0o644)
            self._run(["rsync", "-a", "--from0", f"--files-from={f.name}", *link_opt,
                       f"{self.main}/", f"{dst}/"], what)
        finally:
            os.unlink(f.name)

    # --- a conversation's copy ----------------------------------------------------------------

    def create(self, cid: str, user: str, scope=None, folders: tuple[str, ...] = ()) -> None:
        """Copies the main folder (what scope may see of it) for a conversation, and remembers it
        as the base. folders: its projects' folders, made even when empty."""
        copy, base = self.copy_dir(cid), self.base_dir(cid)
        base.parent.mkdir(parents=True, exist_ok=True)
        self._run(["install", "-d", "-m", "750", *self._owner(user), str(copy)])
        self._rsync_visible(copy, scope, "copying the work folder")
        for name in folders:
            if not (copy / name).exists():
                self._run(["install", "-d", "-m", "755", *self._owner(user), str(copy / name)])
        if not no_sudo():
            self._run(["chown", "-R", f"{user}:{self.cfg.user}", str(copy)])
        self._close_top(copy)
        self._snapshot_base(cid, scope=scope)

    def make_folder(self, name: str) -> None:
        """A project's folder in the main folder (a new project starts empty)."""
        d = self.main / name
        if not d.exists():
            self._run(["install", "-d", "-m", "755", *self._owner(self.cfg.user), str(d)])

    def remove_empty_folder(self, name: str) -> bool:
        d = self.main / name
        try:
            if d.is_dir() and not any(d.iterdir()):
                self._run(["rmdir", "--", str(d)])
                return True
        except OSError:
            pass
        return False

    def add_visible(self, cid: str, user: str, scope, folders: tuple[str, ...] = ()) -> list[str]:
        """Files that became visible to a conversation (a project added): copied into its copy
        and its base, so they count as in step. Files already in the copy are left alone."""
        copy, base = self.copy_dir(cid), self.base_dir(cid)
        C = _files(copy)
        added = []
        for rel, p in _files(self.main).items():
            if rel in C or not scope.visible(rel):
                continue
            self._put(p, copy / rel, user)
            self._put(p, base / rel, self.cfg.user)
            added.append(rel)
        for name in folders:
            if not (copy / name).exists():
                self._run(["install", "-d", "-m", "755", *self._owner(user), str(copy / name)])
        return added

    def give(self, cid: str, user: str, rel: str) -> None:
        """One file from the main folder into a conversation's copy and base (you gave it)."""
        src = self.main / rel
        if not src.is_file():
            raise SyncError(f"There's no file {rel} in the main folder.")
        self._put(src, self.copy_dir(cid) / rel, user)
        self._put(src, self.base_dir(cid) / rel, self.cfg.user)

    def visible_levels(self, cid: str, scope) -> set[str]:
        """The levels of the files in a conversation's copy that it may see."""
        return {scope.level_of(rel) for rel in _files(self.copy_dir(cid)) if scope.visible(rel)}

    def hidden_count(self, scope) -> int:
        """How many files of the conversation's projects it may not see (it's told the number)."""
        return sum(1 for rel in _files(self.main)
                   if scope.projects.project_of(rel) in scope.opened and not scope.visible(rel))

    def _snapshot_base(self, cid: str, keep: dict[str, bytes | None] | None = None, scope=None) -> None:
        """The base is the main folder now (hard links: cheap, and the main folder's files
        are only ever replaced, never edited in place), what scope may see of it. keep: base
        versions to leave as they were (open conflicts, held changes)."""
        base = self.base_dir(cid)
        tmp = base.with_name("base.new")
        if tmp.exists():
            self._run(["rm", "-rf", "--", str(tmp)])
        self._run(["install", "-d", "-m", "750", *self._owner(self.cfg.user), str(tmp)])   # as rsync would
        self._rsync_visible(tmp, scope, "saving the base", link=True)
        for rel, data in (keep or {}).items():
            target = tmp / rel
            if data is None:
                self._run(["rm", "-f", "--", str(target)])
            else:
                self._write(target, data, self.cfg.user)
        if base.exists():
            self._run(["rm", "-rf", "--", str(base)])
        self._run(["mv", "--", str(tmp), str(base)])

    def remove(self, cid: str) -> None:
        for p in (self.copy_dir(cid), self.state / cid):
            if p.exists():
                self._run(["rm", "-rf", "--", str(p)])

    # --- comparing -------------------------------------------------------------------------

    def _hash(self, p: Path) -> str:
        try:
            st = p.stat()
        except PermissionError:   # in a folder the supervisor can't enter: through root, uncached
            return hashlib.sha1(_read(p)).hexdigest()
        key = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        h = self._hash_cache.get(key)
        if h is None:
            d = hashlib.sha1()
            try:
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        d.update(chunk)
            except PermissionError:
                d.update(_read(p))
            h = self._hash_cache[key] = d.hexdigest()
            if len(self._hash_cache) > 50000:
                self._hash_cache.clear()
        return h

    def plan(self, cid: str, scope=None, release=()) -> dict:
        """What bringing in would do, without doing it. scope: what the conversation may see
        (privacy.py); release: held paths you let through."""
        if not self.exists(cid):
            return {"mine": [], "theirs": [], "merged": [], "conflicts": [], "same": [], "resolving": [],
                    "handed": [], "held": [], "blocked": [], "ready": False}
        B, C, M = _files(self.base_dir(cid)), _files(self.copy_dir(cid)), _files(self.main)
        blocked = []
        if scope is not None:
            # Changes where it may not look are never brought in; the rest compares as usual.
            for rel in sorted(set(B) | set(C)):
                if scope.visible(rel):
                    continue
                b = self._hash(B[rel]) if rel in B else None
                c = self._hash(C[rel]) if rel in C else None
                if b != c:
                    reason = ("outside its projects" if scope.projects.project_of(rel) not in scope.opened
                              else f"{scope.level_of(rel)}: more private than this conversation")
                    blocked.append({"path": rel, "change": "added" if b is None else "removed" if c is None
                                    else "changed", "reason": reason})
            B = {r: p for r, p in B.items() if scope.visible(r)}
            C = {r: p for r, p in C.items() if scope.visible(r)}
            M = {r: p for r, p in M.items() if scope.visible(r)}
        release = set(release)
        mine, theirs, merged, conflicts, same, held = [], [], [], [], [], []
        waiting = self.resolving(cid)          # an agent's resolution you haven't reviewed: not brought in
        handed = self._live_handed(cid)        # another conversation is resolving these: left alone here
        for rel in sorted(set(B) | set(C) | set(M)):
            if rel in waiting or rel in handed:
                continue
            b = self._hash(B[rel]) if rel in B else None
            c = self._hash(C[rel]) if rel in C else None
            m = self._hash(M[rel]) if rel in M else None
            if c == b and m == b:
                continue
            change = lambda new: "added" if b is None else "removed" if new is None else "changed"  # noqa: E731
            hold = scope is not None and scope.held(rel) and rel not in release and c != b
            if hold and c != m:
                # It would carry what the conversation has seen somewhere less private: you decide.
                held.append({"path": rel, "change": change(c), "level": scope.level_of(rel), "seen": scope.seen,
                             "conflict": m != b})
            elif m == b:
                mine.append({"path": rel, "change": change(c)})
            elif c == b:
                theirs.append({"path": rel, "change": change(m)})
            elif c == m:
                same.append({"path": rel})
            else:
                result = self._merge(B.get(rel), C.get(rel), M.get(rel))
                if result is not None:
                    merged.append({"path": rel})
                else:
                    kind = ("removed here" if c is None else "removed in the main folder" if m is None
                            else "added in both" if b is None else "changed in both")
                    text = all(p is None or _is_text(_read(p)) for p in (B.get(rel), C.get(rel), M.get(rel)))
                    conflicts.append({"path": rel, "kind": kind, "text": text})
        return {"mine": mine, "theirs": theirs, "merged": merged, "conflicts": conflicts, "same": same,
                "resolving": sorted(waiting),
                "handed": [{"path": rel, "conv": e.get("conv", "")} for rel, e in sorted(handed.items())],
                "held": held, "blocked": blocked, "ready": True}

    def held_diff(self, cid: str, rel: str) -> dict:
        """A held change, against the main folder's version, for your review."""
        C = self.copy_dir(cid) / rel
        M = self.main / rel
        now = _read(C) if C.is_file() else None
        main = _read(M) if M.is_file() else None
        out = {"path": rel, "removed": now is None}
        if now is not None and _is_text(now) and (main is None or _is_text(main)):
            diff = "".join(difflib.unified_diff((main or b"").decode().splitlines(True), now.decode().splitlines(True),
                                                "main folder", "this conversation", n=3))
            out["diff"] = diff[:60000] + ("\n... (cut off)" if len(diff) > 60000 else "")
        elif now is not None:
            out["binary"], out["size"] = True, len(now)
        return out

    def _merge(self, b: Path | None, c: Path | None, m: Path | None) -> bytes | None:
        """A line-by-line merge of two text versions, or None when they really conflict."""
        if c is None or m is None:
            return None   # one side removed it, the other changed it
        datas = [_read(p) if p else b"" for p in (c, b, m)]
        if not all(_is_text(d) for d in datas) or shutil.which("git") is None:
            return None
        with tempfile.TemporaryDirectory() as tmp:
            names = []
            for n, d in zip(("mine", "base", "theirs"), datas):
                path = os.path.join(tmp, n)
                with open(path, "wb") as f:
                    f.write(d)
                names.append(path)
            p = subprocess.run(["git", "merge-file", "-p", *names], capture_output=True, timeout=60)
        return p.stdout if p.returncode == 0 else None

    # --- bringing in ----------------------------------------------------------------------

    def sync(self, cid: str, user: str, choices: dict[str, str] | None = None, only: set | None = None,
             scope=None, release=()) -> dict:
        """Brings the conversation's changes into the main folder and the main folder's into
        the copy. choices: for conflicts, path -> "mine" (the conversation's version) or "main".
        Other conflicts stay open (an agent resolves them, or you decide later). scope and
        release: see plan(); held and blocked changes stay in the copy, not brought in."""
        choices = choices or {}
        plan = self.plan(cid, scope, release)
        if not plan["ready"]:
            raise SyncError("This conversation has no copy of the work folder yet.")
        if only is not None:   # just these files (an accepted resolution): the rest stays as it is
            plan = {k: ([x for x in v if x["path"] in only] if isinstance(v, list) and k != "resolving" else v)
                    for k, v in plan.items()}
        B, C, M = _files(self.base_dir(cid)), _files(self.copy_dir(cid)), _files(self.main)
        to_main = [x["path"] for x in plan["mine"]] + [x["path"] for x in plan["conflicts"]
                                                      if choices.get(x["path"]) == "mine"]
        to_copy = [x["path"] for x in plan["theirs"]] + [x["path"] for x in plan["conflicts"]
                                                        if choices.get(x["path"]) in ("main", "shared")]
        open_conflicts = [x["path"] for x in plan["conflicts"]
                          if choices.get(x["path"]) not in ("mine", "main", "shared")]
        undo = None
        if to_main or plan["merged"]:
            undo = self.main_undo.create()           # the main folder can be put back
        for rel in to_main:
            self._put(C.get(rel), self.main / rel, self.cfg.user)
        for rel in to_copy:
            self._put(M.get(rel), self.copy_dir(cid) / rel, user)
        for x in plan["merged"]:
            rel = x["path"]
            data = self._merge(B.get(rel), C.get(rel), M.get(rel))
            if data is None:   # changed meanwhile: leave it for the next time
                open_conflicts.append(rel)
                continue
            self._write(self.main / rel, data, self.cfg.user)
            self._write(self.copy_dir(cid) / rel, data, user)
        if only is not None:   # the base moves on for these files only
            M = _files(self.main)
            for rel in only:
                self._put_bytes(_read(M[rel]) if rel in M else None, self.base_dir(cid) / rel, self.cfg.user)
        else:
            # Open conflicts, held changes, and files another conversation is resolving, keep their base.
            kept = open_conflicts + list(self.handed(cid)) + [x["path"] for x in plan["held"]]
            keep = {rel: (_read(B[rel]) if rel in B else None) for rel in kept}
            self._snapshot_base(cid, keep, scope)
        return {"brought_in": to_main, "got": to_copy,
                "merged": [x["path"] for x in plan["merged"] if x["path"] not in open_conflicts],
                "open_conflicts": open_conflicts, "undo": undo,
                "held": [x["path"] for x in plan["held"]], "blocked": [x["path"] for x in plan["blocked"]]}

    def prepare_agent_merge(self, cid: str, user: str, paths: list[str]) -> list[dict]:
        """For conflicts the agent should merge: the main folder's version (and the common one) next to
        the conversation's own in .uvmc-merge/, and the base moved on, so that the agent's
        result counts as its change when it's brought in."""
        B, M = _files(self.base_dir(cid)), _files(self.main)
        self._start_resolving(cid, paths, _files(self.copy_dir(cid)), B, M)
        out = []
        keep_base = {}
        for rel in paths:
            merge = self.copy_dir(cid) / MERGE_DIR
            entry = {"path": rel}
            if rel in M:
                self._write(merge / (rel + ".main"), _read(M[rel]), user)
                entry["main"] = f"{MERGE_DIR}/{rel}.main"
            if rel in B:
                self._write(merge / (rel + ".before"), _read(B[rel]), user)
                entry["before"] = f"{MERGE_DIR}/{rel}.before"
            keep_base[rel] = _read(M[rel]) if rel in M else None
            out.append(entry)
        # The base for these files becomes the main folder's version: the agent's file will include it.
        base = self.base_dir(cid)
        for rel, data in keep_base.items():
            if data is None:
                if (base / rel).exists():
                    self._run(["rm", "-f", "--", str(base / rel)])
            else:
                self._write(base / rel, data, self.cfg.user, replace=True)
        return out

    def prepare_other_merge(self, cid: str, other: str, other_user: str, paths: list[str]) -> list[dict]:
        """For conflicts another conversation should resolve (e.g. the one that made the main
        folder's version): this conversation's version (and the common one) go into the other's
        .uvmc-merge/. This conversation then counts as in step for those files, so the other's
        result, once brought in, comes back to it as a newer main folder version."""
        A, BA = _files(self.copy_dir(cid)), _files(self.base_dir(cid))
        self._start_resolving(other, paths, _files(self.copy_dir(other)), BA, _files(self.main), A, cid)
        out = []
        merge = self.copy_dir(other) / MERGE_DIR
        for rel in paths:
            entry = {"path": rel}
            if rel in A:
                self._write(merge / (rel + ".other"), _read(A[rel]), other_user)
                entry["other"] = f"{MERGE_DIR}/{rel}.other"
            if rel in BA:
                self._write(merge / (rel + ".before"), _read(BA[rel]), other_user)
                entry["before"] = f"{MERGE_DIR}/{rel}.before"
            out.append(entry)
            self._hand(cid, rel, other, BA.get(rel))
            base = self.base_dir(cid) / rel
            if rel in A:
                self._write(base, _read(A[rel]), self.cfg.user)
            elif base.exists():
                self._run(["rm", "-f", "--", str(base)])
        return out

    # --- an agent's proposed resolution, waiting for you ------------------------------------
    # While an agent resolves conflicts, UVMC keeps every version aside (the conversation's own,
    # the main folder's, the common one and, when another conversation resolves, the other's), so
    # you can accept its result or pick any of them afterwards.

    def _stash_dir(self, cid: str) -> Path:
        return self.state / cid / "resolving"

    # Files this conversation handed to another one to resolve. They stay out of its bring-in
    # (so it doesn't take the main folder's old version meanwhile) until you answer the other's
    # proposal. Its own base from before is kept, to open the conflict again if needed.

    def handed(self, cid: str) -> dict[str, dict]:
        try:
            return json.loads((self.state / cid / "handed.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_handed(self, cid: str, data: dict) -> None:
        (self.state / cid).mkdir(parents=True, exist_ok=True)
        (self.state / cid / "handed.json").write_text(json.dumps(data), encoding="utf-8")

    def _hand(self, cid: str, rel: str, other: str, before: Path | None) -> None:
        h = self.handed(cid)
        h[rel] = {"conv": other, "before": self._stash(cid, rel, "handed", before)}
        self._save_handed(cid, h)

    def _release(self, cid: str, rel: str, reopen: bool) -> None:
        if not (self.state / cid).is_dir():
            return   # that conversation was closed
        h = self.handed(cid)
        e = h.pop(rel, None)
        if e is None:
            return
        if reopen:   # back to the conflict it had
            data = self._stashed(cid, rel, "handed") if e.get("before") else None
            self._put_bytes(data, self.base_dir(cid) / rel, self.cfg.user)
        p = self._stash_dir(cid) / "handed" / rel
        if p.exists():
            p.unlink()
        self._save_handed(cid, h)

    def _live_handed(self, cid: str) -> dict[str, dict]:
        """Handed files the other conversation is still resolving. One it dropped (closed without
        answering) comes back here as the conflict it was."""
        h = self.handed(cid)
        for rel, e in list(h.items()):
            if (self.resolving(e.get("conv", "")).get(rel) or {}).get("other_conv") != cid:
                self._release(cid, rel, reopen=True)
                h.pop(rel)
        return h

    def resolving(self, cid: str) -> dict[str, dict]:
        """Files an agent is resolving or has proposed a resolution for: path -> versions kept."""
        try:
            return json.loads((self.state / cid / "resolving.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_resolving(self, cid: str, data: dict) -> None:
        (self.state / cid).mkdir(parents=True, exist_ok=True)
        (self.state / cid / "resolving.json").write_text(json.dumps(data), encoding="utf-8")

    def _stash(self, cid: str, rel: str, kind: str, src: Path | None) -> bool:
        """Keeps one version aside (owned by the supervisor). False when there is none (removed)."""
        if src is None:
            return False
        dst = self._stash_dir(cid) / kind / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(_read(src))
        return True

    def _stashed(self, cid: str, rel: str, kind: str) -> bytes | None:
        try:
            return (self._stash_dir(cid) / kind / rel).read_bytes()
        except OSError:
            return None

    def _start_resolving(self, cid: str, paths: list[str], C: dict, B: dict, M: dict,
                         other: dict | None = None, other_conv: str = "") -> None:
        index = self.resolving(cid)
        for rel in paths:
            index[rel] = {"yours": self._stash(cid, rel, "yours", C.get(rel)),
                          "before": self._stash(cid, rel, "before", B.get(rel)),
                          "main": self._stash(cid, rel, "main", M.get(rel)),
                          "other": self._stash(cid, rel, "other", (other or {}).get(rel)) if other is not None else None,
                          "other_conv": other_conv}
        self._save_resolving(cid, index)

    def proposal(self, cid: str) -> list[dict]:
        """The agent's result for each file it resolved, against the main folder's version."""
        C = _files(self.copy_dir(cid))
        out = []
        for rel, kept in sorted(self.resolving(cid).items()):
            now = _read(C[rel]) if rel in C else None
            main = self._stashed(cid, rel, "main")
            item = {"path": rel, "removed": now is None, "same_as_main": now == main,
                    "same_as_yours": now == self._stashed(cid, rel, "yours"),
                    "has": {k: bool(kept.get(k)) for k in ("yours", "main", "other")},
                    "other_conv": kept.get("other_conv", "")}
            if now is not None and _is_text(now) and (main is None or _is_text(main)):
                diff = "".join(difflib.unified_diff((main or b"").decode().splitlines(True), now.decode().splitlines(True),
                                                    "main folder", "proposed", n=3))
                item["diff"] = diff[:60000] + ("\n... (cut off)" if len(diff) > 60000 else "")
            elif now is not None:
                item["binary"] = True
                item["size"] = len(now)
            out.append(item)
        return out

    def resolve(self, cid: str, user: str, choices: dict[str, str], scope=None) -> dict:
        """Your answer to a proposal: path -> "accept" (bring the agent's result in), "yours" (this
        conversation's version from before), "main" (keep the main folder's), "other" (the other
        conversation's version, when another conversation resolved it), or "later" (back to an
        open conflict). Returns what was brought in and what is open again."""
        index = self.resolving(cid)
        bring, reopen = [], []
        for rel, choice in choices.items():
            kept = index.get(rel)
            if kept is None:
                continue
            target = self.copy_dir(cid) / rel
            if choice == "accept":
                bring.append(rel)
            elif choice in ("yours", "other", "main"):
                data = self._stashed(cid, rel, choice) if kept.get(choice) else None
                self._put_bytes(data, target, user)
                if choice != "main":
                    bring.append(rel)
            elif choice == "later":
                self._put_bytes(self._stashed(cid, rel, "yours") if kept.get("yours") else None, target, user)
                if not kept.get("other_conv"):   # resolving another conversation's file: this base never moved
                    self._put_bytes(self._stashed(cid, rel, "before") if kept.get("before") else None,
                                    self.base_dir(cid) / rel, self.cfg.user)
                    reopen.append(rel)
            else:
                continue
            if kept.get("other_conv"):
                # The other conversation gets this file back: the result comes to it as the main
                # folder's newer version, or, if you decide later, its conflict is open there again.
                self._release(kept["other_conv"], rel, reopen=choice == "later")
            index.pop(rel)
            for kind in ("yours", "before", "main", "other"):
                p = self._stash_dir(cid) / kind / rel
                if p.exists():
                    p.unlink()
            for suffix in (".main", ".shared", ".before", ".other"):   # .shared: before 0.12.4
                p = self.copy_dir(cid) / MERGE_DIR / (rel + suffix)
                if p.exists() or p.is_symlink():
                    self._run(["rm", "-f", "--", str(p)])
        self._save_resolving(cid, index)
        merge = self.copy_dir(cid) / MERGE_DIR
        if not index and merge.exists():
            self._run(["rm", "-rf", "--", str(merge)])
        # What you accept here is your own review: it isn't held again.
        result = (self.sync(cid, user, {}, only=set(bring), scope=scope, release=bring) if bring
                  else {"brought_in": [], "undo": None})
        result["reopened"] = reopen
        return result

    def _put_bytes(self, data: bytes | None, dst: Path, owner: str) -> None:
        if data is None:
            if dst.exists() or dst.is_symlink():
                self._run(["rm", "-f", "--", str(dst)])
        else:
            self._write(dst, data, owner)

    def undo_bring_in(self, checkpoint: str) -> None:
        self.main_undo.restore(checkpoint)

    # --- a worker's copy of its coordinator's folder ------------------------------------------
    # A coordinator's job runs in a copy of the coordinator's folder, as the worker's own user.
    # When it ends, the copy is frozen (the worker's user may do the next job), and the
    # coordinator brings its changes in, the same three-way way as a bring-in to the main
    # folder, but in one direction only: into the coordinator's folder.

    def create_job(self, jid: str, source: str, user: str) -> None:
        """Copies the coordinator's folder for a job, and keeps it as the base: a real copy, since
        the worker edits its files in place."""
        copy, base = self.copy_dir(jid), self.base_dir(jid)
        base.parent.mkdir(parents=True, exist_ok=True)
        self._run(["install", "-d", "-m", "750", *self._owner(user), str(copy)])
        # .uvmc-merge too: a coordinator resolving a conflict hands the merging to a worker, which
        # finds the other versions where the task says. (Like everywhere, it isn't brought back.)
        self._run(["rsync", "-a", "--delete", f"{source}/", f"{copy}/"], "copying the coordinator's folder")
        self._run(["rsync", "-a", "--delete", f"{copy}/", f"{base}/"], "saving the base")
        if not no_sudo():
            self._run(["chown", "-R", f"{user}:{self.cfg.user}", str(copy)])
        self._close_top(copy)

    def freeze_job(self, jid: str, reader: str | None = None) -> None:
        """A finished job's folder: nobody can change it any more (its user does other jobs),
        and no agent but its coordinator (reader) can look inside: the job holds nothing the
        coordinator may not see."""
        if no_sudo() or not self.exists(jid):
            return
        copy = str(self.copy_dir(jid))
        self._run(["chown", "-R", f"{self.cfg.user}:{self.cfg.user}", copy])
        self._run(["chmod", "-R", "go-w", copy])
        self._close_top(Path(copy))
        if reader:
            self._run(["setfacl", "-m", f"u:{reader}:rx", copy], "letting the coordinator read the job")

    def job_plan(self, jid: str, target: str) -> dict:
        """What bringing a job in would do to the coordinator's folder (target)."""
        B, C, M = _files(self.base_dir(jid)), _files(self.copy_dir(jid)), _files(Path(target))
        changed, merged, conflicts = [], [], []
        for rel in sorted(set(B) | set(C)):
            b = self._hash(B[rel]) if rel in B else None
            c = self._hash(C[rel]) if rel in C else None
            if c == b:
                continue   # the worker didn't change it
            m = self._hash(M[rel]) if rel in M else None
            change = "added" if b is None else "removed" if c is None else "changed"
            if m == c:
                continue   # already like this in the coordinator's folder
            if m == b:
                changed.append({"path": rel, "change": change})
            elif self._merge(B.get(rel), C.get(rel), M.get(rel)) is not None:
                merged.append({"path": rel, "change": change})
            else:
                kind = ("removed by the worker" if c is None else "removed in your folder" if m is None
                        else "added in both" if b is None else "changed in both")
                conflicts.append({"path": rel, "kind": kind})
        return {"changed": changed, "merged": merged, "conflicts": conflicts}

    def bring_in_job(self, jid: str, target: str, owner: str, choices: dict[str, str] | None = None) -> dict:
        """Brings a job's changes into the coordinator's folder. choices, for conflicts: path ->
        "worker" (its version) or "mine" (keep the coordinator's). Others stay open."""
        choices = choices or {}
        plan = self.job_plan(jid, target)
        B, C, M = _files(self.base_dir(jid)), _files(self.copy_dir(jid)), _files(Path(target))
        top = Path(target)
        done, merged, kept, still = [], [], [], []
        for x in plan["changed"]:
            self._put(C.get(x["path"]), top / x["path"], owner)
            done.append(x["path"])
        for x in plan["merged"]:
            rel = x["path"]
            data = self._merge(B.get(rel), C.get(rel), M.get(rel))
            if data is None:
                still.append(rel)
                continue
            self._write(top / rel, data, owner)
            merged.append(rel)
        for x in plan["conflicts"]:
            rel, pick = x["path"], choices.get(x["path"])
            if pick == "worker":
                self._put(C.get(rel), top / rel, owner)
                done.append(rel)
            elif pick == "mine":
                kept.append(rel)
            else:
                still.append(rel)
        return {"brought_in": done, "merged": merged, "kept": kept, "open_conflicts": still}

    # --- writing as the right user ------------------------------------------------------------

    def _put(self, src: Path | None, dst: Path, owner: str) -> None:
        if src is None:
            if dst.exists() or dst.is_symlink():
                self._run(["rm", "-f", "--", str(dst)])
            return
        self._dirs(dst.parent, owner)
        try:
            mode = oct(src.stat().st_mode & 0o777)[2:]
        except PermissionError:
            r = root(["stat", "-c", "%a", str(src)], timeout=30)
            mode = r.stdout.decode().strip() or "644"
        self._run(["install", "-m", mode, *self._owner(owner), str(src), str(dst)])

    def put_files(self, cid: str, user: str, folder: str, files: dict[str, tuple[bytes, bool]]) -> None:
        """Writes files into a conversation's copy under folder, owned by its user (for example an
        approved tool's code). files: relative path -> (data, executable)."""
        top = self.copy_dir(cid)
        for rel, (data, exe) in sorted(files.items()):
            dst = (top / folder / rel)
            if top.resolve() not in dst.resolve().parents:
                raise SyncError(f"{rel}: outside the work folder.")
            self._write(dst, data, user, mode="755" if exe else "644")

    def _write(self, dst: Path, data: bytes, owner: str, replace: bool = False, mode: str | None = None) -> None:
        keep = mode
        mode = "644"
        if dst.exists():
            try:
                mode = oct(dst.stat().st_mode & 0o777)[2:]
            except OSError:
                pass
        mode = keep or mode
        with tempfile.NamedTemporaryFile(delete=False, dir=str(self.state) if self.state.exists() else None) as f:
            f.write(data)
        try:
            os.chmod(f.name, 0o644)
            self._dirs(dst.parent, owner)
            self._run(["install", "-m", mode, *self._owner(owner), f.name, str(dst)])
        finally:
            os.unlink(f.name)

    def _dirs(self, d: Path, owner: str) -> None:
        missing = []
        while not d.exists():
            missing.append(d)
            d = d.parent
        for m in reversed(missing):
            self._run(["install", "-d", "-m", "750", *self._owner(owner), str(m)])

    @staticmethod
    def _run(cmd: list[str], what: str = "") -> None:
        p = root(cmd, timeout=600)
        if p.returncode not in (0, 24):   # rsync 24: files vanished while copying
            msg = p.stderr.decode(errors="replace").strip()
            raise SyncError(f"{'While ' + what + ': ' if what else ''}{msg or 'a step failed'}")
