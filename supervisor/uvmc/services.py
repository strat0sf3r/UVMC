"""Services: your own long-running programs, installed from git when you approve them.

A service is a standalone app (docs/app-contract.md): it runs on any similar Linux, and UVMC is
one host for it. Its repository holds a manifest, uvmc-app.toml: what to run, the sites and SSH
machines it needs, its settings and secrets, its memory. UVMC fetches the repository, shows you
an approval card, and only then installs the approved commit and runs it:

- as its own user (uvmc-s-<name>, group uvmc-services), from a read-only copy of that commit,
  with its own data folder (APP_DATA);
- as a systemd service (started with the VM, restarted when it fails) inside systemd's sandbox
  and the services' resource box;
- reaching only its declared sites (80/443) and SSH machines (22, those addresses only) through
  a proxy of its own; the firewall gives its user nothing else but UVMC's API;
- with its secrets as systemd credentials (files only it can read while it runs), and the ssh
  config for its machines written by UVMC (pinned host keys you give);
- as a program (programs.py), so its reports and "tell me if it's silent" work with no setup.

New commits are looked for every few minutes and shown with their changes; nothing changes
until you approve. A manifest change (sites, machines, secrets, memory) is part of what you see.
"""

from __future__ import annotations

import collections
import json
import os
import re
import secrets as secrets_mod
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
from pathlib import Path
from typing import Callable

from .checkpoints import Checkpoints
from .executor import no_sudo, root
from .netproxy import Proxy, SiteList, valid_site

MANIFEST = "uvmc-app.toml"
CONTRACT = 1
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
SETTING_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
SETTING_TYPES = ("text", "secret", "number", "yes/no", "long text")
FILE_SETTINGS = ("secret", "long text")    # handed over as files, not in the environment
MEMORY_RE = re.compile(r"^[0-9]{1,6}[KMG]$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
URL_RE = re.compile(r"^(https://[A-Za-z0-9.-]+(:[0-9]+)?/[A-Za-z0-9._~/-]+|file:///[A-Za-z0-9._~/-]+)$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
UNIT_PREFIX = "uvmc-s-"
LOCAL = "local"           # the "commit" of a service installed in place (no repository)
SNAPSHOTS_KEPT = 5
SLICE = "uvmc-services.slice"
MAX_DIFF = 400_000
API_PORT = 8080


class ServiceError(Exception):
    """Something that can't be done; the message says why (for you, in plain words)."""


class NoManifest(ServiceError):
    """The repository has no uvmc-app.toml: you can write one for it, kept by UVMC."""


# Every key this version knows. Keys planned for later contract versions are refused with a
# clear message (silently ignoring a build step or a config file would run the app wrong);
# keys nobody knows are kept and shown as a warning on the card. A higher contract number is
# refused: it needs a newer UVMC. So an app written for a later version never runs half-set-up.
KNOWN_KEYS = {"contract", "name", "title", "description", "run", "health", "restart", "memory",
              "runtime", "network", "ssh", "settings"}
RESERVED_KEYS = {
    "build": "a build step (installing dependencies, compiling)",
    "files": "config files written from the settings",
    "env": "settings mapped to environment variables of the app's choosing",
    "web": "a web page shown in the panel",
    "image": "a container image", "container": "a container",
    "volumes": "extra data folders", "ports": "ports it listens on", "depends": "other services it needs",
}
RUNTIMES = ("process",)            # later: "container" (rootless Podman as the service's user)


# --- the manifest --------------------------------------------------------------------------

def parse_manifest(text: str) -> dict:
    """uvmc-app.toml, checked. Raises ServiceError listing every problem."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ServiceError(f"{MANIFEST} is not valid TOML: {e}") from None
    problems: list[str] = []
    bad = problems.append

    contract = raw.get("contract")
    if isinstance(contract, int) and contract > CONTRACT:
        raise ServiceError(f"This app follows version {contract} of the app contract; this UVMC knows version "
                           f"{CONTRACT}. Update UVMC first.")
    if contract != CONTRACT:
        bad(f"contract = {CONTRACT} is required (the version of the app contract it follows).")
    for key in sorted(set(raw) & set(RESERVED_KEYS)):
        bad(f"{key}: {RESERVED_KEYS[key]} isn't supported by this UVMC yet.")
    runtime = raw.get("runtime", "process")
    if runtime not in RUNTIMES:
        bad(f'runtime: "{runtime}" isn\'t supported by this UVMC (only "process").')
    name = str(raw.get("name", ""))
    if not NAME_RE.match(name):
        bad("name: 2-32 lowercase letters, digits and dashes, starting with a letter.")
    run = raw.get("run")
    if not isinstance(run, str) or not run.strip() or "\n" in run:
        bad("run: the command that starts the app, on one line.")
    health = raw.get("health")
    if health is not None and (not isinstance(health, str) or not health.strip() or "\n" in health):
        bad("health: a command on one line (exit 0 = healthy), or leave it out.")
    restart = raw.get("restart", "on-failure")
    if restart not in ("on-failure", "always"):
        bad('restart: "on-failure" or "always".')
    memory = str(raw.get("memory", "256M"))
    if not MEMORY_RE.match(memory):
        bad('memory: like "256M" or "1G".')

    settings, names = [], set()
    for i, st in enumerate(raw.get("settings", []) if isinstance(raw.get("settings", []), list) else []):
        where = f"settings {i + 1}"
        if not isinstance(st, dict):
            bad(f"{where}: a [[settings]] table.")
            continue
        sname, kind = str(st.get("name", "")), st.get("type", "text")
        if not SETTING_RE.match(sname) or sname == "uvmc_key":
            bad(f"{where}: name is lowercase letters, digits and _ (like github_token).")
            continue
        if sname in names:
            bad(f"{where}: {sname} appears twice.")
        if kind not in SETTING_TYPES:
            bad(f"{where}: type is one of {', '.join(SETTING_TYPES)}.")
            continue
        names.add(sname)
        settings.append({"name": sname, "label": str(st.get("label", sname))[:200], "type": kind,
                         "required": bool(st.get("required", True)),
                         "default": "" if kind in FILE_SETTINGS else str(st.get("default", ""))[:2000]})
    secrets_ = {s["name"] for s in settings if s["type"] == "secret"}

    net = raw.get("network", {}) if isinstance(raw.get("network", {}), dict) else {}
    sites = net.get("sites", [])
    if not isinstance(sites, list) or len(sites) > 20 or not all(isinstance(s, str) and valid_site(s) for s in sites):
        bad("network.sites: up to 20 site names, like github.com.")
        sites = []
    ssh_names = net.get("ssh", [])
    machines = []
    ssh_tables = raw.get("ssh", {}) if isinstance(raw.get("ssh", {}), dict) else {}
    if not isinstance(ssh_names, list) or len(ssh_names) > 10:
        bad("network.ssh: up to 10 machine names.")
        ssh_names = []
    for m in ssh_names:
        t = ssh_tables.get(m) if isinstance(m, str) else None
        if not isinstance(m, str) or not re.match(r"^[a-z][a-z0-9-]{0,31}$", m) or not isinstance(t, dict):
            bad(f"network.ssh: {m!r} needs a [ssh.{m}] table with host, user and key.")
            continue
        host, user, key = str(t.get("host", "")), str(t.get("user", "")), str(t.get("key", ""))
        if not valid_site(host):
            bad(f"ssh.{m}.host: an address or host name.")
        if not USER_RE.match(user):
            bad(f"ssh.{m}.user: a user name on that machine.")
        if key not in secrets_:
            bad(f"ssh.{m}.key: the name of a secret setting (its private key).")
        machines.append({"name": m, "host": host.lower(), "user": user, "key": key})
    if problems:
        raise ServiceError(f"{MANIFEST} has problems:\n- " + "\n- ".join(problems))
    return {"contract": CONTRACT, "runtime": runtime, "unknown": sorted(set(raw) - KNOWN_KEYS - set(RESERVED_KEYS)),
            "name": name, "title": str(raw.get("title", name))[:120],
            "description": str(raw.get("description", ""))[:1000], "run": run.strip(),
            "health": health.strip() if isinstance(health, str) else None, "restart": restart,
            "memory": memory, "sites": [s.lower() for s in sites], "machines": machines, "settings": settings}


def walls(m: dict) -> dict:
    """The parts of a manifest that decide what the service may reach, hold and run."""
    return {"sites": m["sites"], "machines": m["machines"], "memory": m["memory"], "runtime": m.get("runtime", "process"),
            "settings": [(s["name"], s["type"]) for s in m["settings"]], "run": m["run"], "health": m["health"]}


TEMPLATE = """contract = 1
name = "my-app"                       # 2-32 lowercase letters, digits, dashes
title = "My app"
description = "What it does"
run = "python3 main.py"               # starts it, from the repository's folder
memory = "256M"

[network]
sites = []                            # web sites it needs, like "api.example.com"
ssh = []                              # SSH machines by name, each with an [ssh.<name>] table

# [[settings]]
# name = "api_key"
# label = "API key"
# type = "secret"                     # text, secret, number, yes/no or long text
"""


# --- the registry ----------------------------------------------------------------------------

class Services:
    def __init__(self, cfg, programs=None, notify: Callable[[], None] = lambda: None,
                 on_event: Callable[[dict], None] = lambda ev: None):
        self.cfg = cfg
        self.base = Path(cfg.services_dir)
        self.state_file = Path(cfg.state_dir) / "services.json"
        self.secret_root = Path(cfg.state_dir) / "service-secrets"
        self.programs = programs
        self.notify, self.on_event = notify, on_event
        self.lock = threading.RLock()
        self.proxies: dict[str, Proxy] = {}
        self.asks: dict[str, dict] = {}       # an install session's site questions, waiting for you
        self.net: dict[str, collections.deque] = {}
        self.registry: dict[str, dict] = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.registry, f)
        os.replace(tmp, self.state_file)

    def get(self, name: str) -> dict:
        e = self.registry.get(name)
        if e is None:
            raise KeyError(f"There's no service called {name}.")
        return e

    # paths and names
    def unit(self, name: str) -> str:
        return f"{UNIT_PREFIX}{name}.service"

    def user(self, name: str) -> str:
        return f"{UNIT_PREFIX}{name}"

    def repo(self, name: str) -> Path:
        return self.base / name / "repo.git"

    def code(self, name: str, commit: str) -> Path:
        """The app's code: a read-only copy of a commit, or the app folder of one installed in place."""
        return self.app(name) if commit == LOCAL else self.base / name / "versions" / commit[:12]

    def app(self, name: str) -> Path:
        """Where an app installed in place lives (written only during install sessions)."""
        return self.base / name / "app"

    def is_local(self, name: str) -> bool:
        return (self.get(name).get("source") or {}).get("kind") == LOCAL

    def home(self, name: str) -> Path:
        return self.base / name / "data"

    def secrets_dir(self, name: str) -> Path:
        return self.secret_root / name

    # --- git (as the supervisor, through its own network) ---------------------------------------

    def _git(self, repo: Path, *args: str, token: str = "", timeout: int = 120, check: bool = True) -> str:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "UVMC_GIT_TOKEN": token, "GIT_CONFIG_NOSYSTEM": "1"}
        helper = ('!f() { test "$1" = get || exit 0; test -n "$UVMC_GIT_TOKEN" || exit 0; '
                  'echo username=x-access-token; echo "password=$UVMC_GIT_TOKEN"; }; f')
        cmd = ["git", "-c", "credential.helper=", "-c", f"credential.helper={helper}", "-c", "protocol.file.allow=always",
               "--git-dir", str(repo), *args]
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env)
        if check and p.returncode != 0:
            msg = p.stderr.decode(errors="replace").strip().replace(token, "***") if token else p.stderr.decode(errors="replace").strip()
            raise ServiceError(f"git {args[0]} failed: {msg[-500:]}")
        return p.stdout.decode(errors="replace")

    def _token(self, name: str) -> str:
        try:
            return (self.secrets_dir(name) / ".fetch-token").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _fetch(self, repo: Path, url: str, branch: str, token: str) -> str:
        """Fetches the branch; returns its newest commit."""
        if not repo.exists():
            repo.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "-q", "--bare", str(repo)], check=True, capture_output=True)
        self._git(repo, "fetch", "-q", "--no-tags", "--depth", "200", url, f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                  token=token, timeout=300)
        return self._git(repo, "rev-parse", f"refs/remotes/origin/{branch}").strip()

    def _manifest_at(self, repo: Path, commit: str, path: str) -> dict:
        rel = f"{path.strip('/')}/{MANIFEST}" if path.strip("/") else MANIFEST
        p = subprocess.run(["git", "--git-dir", str(repo), "show", f"{commit}:{rel}"], capture_output=True)
        if p.returncode != 0:
            raise NoManifest(f"There's no {rel} in that commit. Write one for it (kept by UVMC, the repository "
                             "stays as it is), or add one to the repository (docs/app-contract.md).")
        return parse_manifest(p.stdout.decode("utf-8", "replace"))

    def _manifest_for(self, name: str, commit: str) -> dict:
        """The manifest that applies: the one you wrote for it in UVMC if any, else the repository's."""
        e = self.get(name)
        return parse_manifest(e["adapter"]) if e.get("adapter") else self._manifest_at(self.repo(name), commit, e["path"])

    def profile_text(self, name: str) -> dict:
        """The profile as text, for editing: the one kept by UVMC, else the repository's file as it is."""
        e = self.get(name)
        if e.get("adapter"):
            return {"text": e["adapter"], "from": "uvmc"}
        if self.is_local(name):
            return {"text": TEMPLATE.replace('"my-app"', f'"{name}"'), "from": "none"}
        commit = e["commit"] or (e.get("proposal") or {}).get("commit")
        rel = f"{e['path']}/{MANIFEST}" if e["path"] else MANIFEST
        p = subprocess.run(["git", "--git-dir", str(self.repo(name)), "show", f"{commit}:{rel}"], capture_output=True)
        if p.returncode == 0:
            return {"text": p.stdout.decode("utf-8", "replace"), "from": "repository"}
        return {"text": TEMPLATE.replace('"my-app"', f'"{name}"'), "from": "none"}

    def set_adapter(self, name: str, text: str | None) -> dict:
        """You write (or change, or drop with None) the manifest UVMC keeps for this app. Like a
        new version, it only applies once you approve it: a proposal for the running commit."""
        with self.lock:
            e = self.get(name)
            if text is not None and not text.strip():
                text = None
            if text is None and self.is_local(name):
                raise ServiceError("An app installed in place has no repository: its profile is the one kept by UVMC.")
            if text is None:
                commit = e["commit"] or e["proposal"]["commit"]
                manifest = self._manifest_at(self.repo(name), commit, e["path"])   # back to the repository's
            else:
                manifest = parse_manifest(text)
            if manifest["name"] != name:
                raise ServiceError(f"The manifest's name must stay {name}.")
            if not e["installed"]:   # still waiting for its first approval: the card shows the new one
                e["adapter"] = text
                e["proposal"]["manifest"] = manifest
            else:
                summary = ({"commit": LOCAL, "author": "", "date": "", "subject": "its profile"} if self.is_local(name)
                           else self._summary(self.repo(name), e["commit"]))
                e["proposal"] = {**summary, "kind": "manifest", "manifest": manifest,
                                 "adapter": text, "walls_changed": walls(manifest) != walls(e["manifest"]), "problem": None}
            self._save()
        self.notify()
        return self.view(name)

    def _files(self, repo: Path, commit: str) -> list[str]:
        return self._git(repo, "ls-tree", "-r", "--name-only", commit).splitlines()[:2000]

    def _summary(self, repo: Path, commit: str) -> dict:
        line = self._git(repo, "log", "-1", "--format=%H%x00%an%x00%ad%x00%s", "--date=iso", commit).strip().split("\0")
        return {"commit": line[0], "author": line[1], "date": line[2], "subject": line[3]}

    # --- adding one (you, in the panel) -------------------------------------------------------

    def add(self, url: str, branch: str = "main", path: str = "", token: str = "", manifest_text: str = "") -> dict:
        """Fetches the repository and reads its manifest (or takes the one you wrote for it, kept by
        UVMC): a proposal for you to approve. Nothing runs."""
        url, branch, path = url.strip(), (branch or "main").strip(), (path or "").strip().strip("/")
        if not URL_RE.match(url):
            raise ServiceError("The repository's address: https://github.com/owner/repo (or file:///path for a local one).")
        if not BRANCH_RE.match(branch) or ".." in branch:
            raise ServiceError("The branch: a name like main.")
        if path and (not re.match(r"^[A-Za-z0-9._/-]+$", path) or ".." in path.split("/")):
            raise ServiceError("The folder in the repository: a path like apps/dispatcher, or empty for the top.")
        incoming = self.base / ".incoming" / secrets_mod.token_hex(6)
        try:
            commit = self._fetch(incoming / "repo.git", url, branch, token)
            adapter = manifest_text.strip() or None
            manifest = parse_manifest(adapter) if adapter else self._manifest_at(incoming / "repo.git", commit, path)
            name = manifest["name"]
            with self.lock:
                if name in self.registry:
                    raise ServiceError(f"There's already a service called {name}.")
                if self.programs is not None and name in self.programs.registry:
                    raise ServiceError(f"A program called {name} already uses that name (Settings > Programs).")
                (self.base / name).mkdir(parents=True, exist_ok=True)
                os.chmod(self.base / name, 0o755)
                shutil.move(str(incoming / "repo.git"), str(self.repo(name)))
                self.registry[name] = {
                    "name": name, "source": {"kind": "git", "url": url, "ref": branch}, "adapter": adapter,
                    "url": url, "branch": branch, "path": path, "created": time.time(),
                    "installed": False, "commit": None, "manifest": None, "enabled": False,
                    "values": {}, "host_keys": {}, "port": None, "checked": time.time(),
                    "proposal": {**self._summary(self.repo(name), commit), "manifest": manifest,
                                 "files": self._files(self.repo(name), commit), "kind": "install"}}
                if token:
                    self._write_secret(name, ".fetch-token", token)
                self._save()
        finally:
            shutil.rmtree(incoming, ignore_errors=True)
        self.notify()
        return self.view(name)

    def add_local(self, profile: str) -> dict:
        """An empty account to install an app into: its profile (kept by UVMC) for you to approve.
        Nothing is made before you approve it; then the account starts with an empty app folder."""
        manifest = parse_manifest(profile)
        name = manifest["name"]
        with self.lock:
            if name in self.registry:
                raise ServiceError(f"There's already a service called {name}.")
            if self.programs is not None and name in self.programs.registry:
                raise ServiceError(f"A program called {name} already uses that name (Settings > Programs).")
            (self.base / name).mkdir(parents=True, exist_ok=True)
            os.chmod(self.base / name, 0o755)
            self.registry[name] = {
                "name": name, "source": {"kind": LOCAL}, "adapter": profile.strip(), "url": "", "branch": "", "path": "",
                "created": time.time(), "installed": False, "commit": None, "manifest": None, "enabled": False,
                "values": {}, "host_keys": {}, "port": None, "checked": None, "level": "open",
                "proposal": {"commit": LOCAL, "author": "", "date": "", "subject": "an empty account", "kind": "install",
                             "manifest": manifest, "files": []}}
            self._save()
        self.notify()
        return self.view(name)

    def check_updates(self, name: str | None = None) -> list[str]:
        """Fetches each installed service's branch; a new commit becomes a proposal (it waits for you)."""
        found = []
        for n in [name] if name else list(self.registry):
            e = self.registry.get(n)
            if not e or not e["installed"] or (e.get("source") or {}).get("kind") == LOCAL:
                continue
            try:
                head = self._fetch(self.repo(n), e["url"], e["branch"], self._token(n))
            except (ServiceError, subprocess.SubprocessError, OSError) as err:
                with self.lock:
                    e["check_error"] = str(err)[:300]
                    e["checked"] = time.time()
                    self._save()
                continue
            with self.lock:
                e["checked"], e["check_error"] = time.time(), None
                if head != e["commit"] and (e.get("proposal") or {}).get("commit") != head and e.get("dismissed") != head:
                    try:
                        manifest = self._manifest_for(n, head)
                        problem = None
                    except ServiceError as err:
                        manifest, problem = None, str(err)
                    log = self._git(self.repo(n), "log", "--format=%h%x00%an%x00%ad%x00%s", "--date=short",
                                    f"{e['commit']}..{head}", check=False).strip().splitlines()[:50]
                    stat = self._git(self.repo(n), "diff", "--stat=120", e["commit"], head, check=False).strip()
                    e["proposal"] = {**self._summary(self.repo(n), head), "kind": "update", "manifest": manifest,
                                     "problem": problem, "stat": stat[-6000:],
                                     "log": [dict(zip(("short", "author", "date", "subject"), ln.split("\0"))) for ln in log],
                                     "walls_changed": manifest is not None and walls(manifest) != walls(e["manifest"])}
                    found.append(n)
                self._save()
        if found:
            self.notify()
            for n in found:
                self.on_event({"kind": "service_update", "name": n, "title": self.registry[n]["manifest"]["title"]})
        return found

    def diff(self, name: str) -> str:
        """The proposed update's full diff (cut at MAX_DIFF)."""
        e = self.get(name)
        p = e.get("proposal")
        if not p or p.get("kind") != "update" or self.is_local(name):
            raise ServiceError("No update is waiting.")
        out = self._git(self.repo(name), "diff", e["commit"], p["commit"], check=False)
        return out if len(out) <= MAX_DIFF else out[:MAX_DIFF] + f"\n\n(cut: the diff is over {MAX_DIFF // 1000} KB)"

    def discard(self, name: str) -> None:
        """Drops the waiting proposal; a service never installed goes entirely."""
        with self.lock:
            e = self.get(name)
            if not e["installed"]:
                del self.registry[name]
                self._save()
                shutil.rmtree(self.secrets_dir(name), ignore_errors=True)
                shutil.rmtree(self.base / name, ignore_errors=True)
            else:
                e["dismissed"] = (e.get("proposal") or {}).get("commit")
                e["proposal"] = None
                self._save()
        self.notify()

    # --- approving: install or update ---------------------------------------------------------

    def approve(self, name: str, commit: str) -> dict:
        """Installs the proposed commit (you approved exactly this one) and, if it may run, (re)starts it."""
        with self.lock:
            e = self.get(name)
            p = e.get("proposal")
            if not p or p["commit"] != commit:
                raise ServiceError("That's not the version waiting for approval (it may have changed); look again.")
            if p.get("manifest") is None:
                raise ServiceError(p.get("problem") or "This version has no valid manifest.")
            manifest = p["manifest"]
            if manifest["name"] != name:
                raise ServiceError(f"This version renames the app to {manifest['name']}; add it as a new service instead.")
            if p["kind"] == "manifest":
                e["adapter"] = p.get("adapter")
            old = e.get("commit")
            if commit != LOCAL:
                self._checkout(name, commit)
            first = not e["installed"]
            e.update({"installed": True, "commit": commit, "manifest": manifest, "proposal": None,
                      "updated": time.time(), "subject": p.get("subject", "")})
            if e.get("port") is None:
                e["port"] = self._free_port()
            names = {s["name"] for s in manifest["settings"]}
            e["values"] = {k: v for k, v in e["values"].items() if k in names}
            e["host_keys"] = {k: v for k, v in e["host_keys"].items() if k in {m["name"] for m in manifest["machines"]}}
            self._save()
            if first:
                self._make_user(name)
                self._ensure_program(name, manifest)
                if commit == LOCAL:
                    self._make_app_folder(name)
            self._write_runtime(name)
            if old and old != commit and old != LOCAL:
                self._drop_version(name, old)
            if e["enabled"]:
                if self.missing(name):
                    e["enabled"] = False
                    self._save()
                    self._systemctl("disable", "--now", self.unit(name))
                else:
                    self._systemctl("restart", self.unit(name))
        self.notify()
        return self.view(name)

    def _checkout(self, name: str, commit: str) -> None:
        """The approved commit as a read-only copy: root's, readable by the services' group."""
        target = self.code(name, commit)
        if target.exists():
            return
        tmp = Path(tempfile.mkdtemp(prefix="uvmc-svc-", dir=self.base))
        try:
            archive = subprocess.run(["git", "--git-dir", str(self.repo(name)), "archive", "--format=tar", commit],
                                     capture_output=True, timeout=300, check=True).stdout
            subprocess.run(["tar", "-x", "-C", str(tmp)], input=archive, check=True, capture_output=True, timeout=300)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(target.parent, 0o755)
            if no_sudo():
                os.rename(tmp, target)
                return
            for cmd in (["chown", "-R", f"root:{self.cfg.services_group}", str(tmp)],
                        ["chmod", "-R", "u=rwX,g=rX,o=", str(tmp)], ["chmod", "750", str(tmp)],
                        ["mv", "-T", str(tmp), str(target)]):
                p = root(cmd, timeout=120)
                if p.returncode != 0:
                    raise ServiceError(f"Could not set up the code: {p.stderr.decode(errors='replace').strip()}")
        finally:
            if tmp.exists():
                (root(["rm", "-rf", "--", str(tmp)], timeout=120) if not no_sudo() else shutil.rmtree(tmp, ignore_errors=True))

    def _drop_version(self, name: str, commit: str) -> None:
        d = self.code(name, commit)
        if d.exists():
            root(["rm", "-rf", "--", str(d)], timeout=120) if not no_sudo() else shutil.rmtree(d, ignore_errors=True)

    def _free_port(self) -> int:
        used = {e.get("port") for e in self.registry.values()}
        for port in range(self.cfg.services_port, self.cfg.services_port + 100):
            if port not in used:
                return port
        raise ServiceError("No free port for another service's proxy (100 at most).")

    def _make_user(self, name: str) -> None:
        home = self.home(name)
        home.mkdir(parents=True, exist_ok=True)
        if no_sudo():
            return
        user, group = self.user(name), self.cfg.services_group
        if subprocess.run(["id", "-u", user], capture_output=True).returncode != 0:
            p = root(["useradd", "--system", "--no-create-home", "--home-dir", str(home), "--shell",
                      "/usr/sbin/nologin", "--gid", group, user], timeout=30)
            if p.returncode != 0:
                raise ServiceError(f"Could not make the service's user: {p.stderr.decode(errors='replace').strip()}")
        root(["chown", f"{user}:{group}", str(home)], timeout=30)
        root(["chmod", "700", str(home)], timeout=30)

    def _make_app_folder(self, name: str) -> None:
        """The empty app folder: the service's user owns it (to install into it during a session);
        while the app runs, systemd's sandbox keeps it read-only."""
        app = self.app(name)
        app.mkdir(parents=True, exist_ok=True)
        if not no_sudo():
            root(["chown", f"{self.user(name)}:{self.cfg.services_group}", str(app)], timeout=30)
            root(["chmod", "750", str(app)], timeout=30)

    def _ensure_program(self, name: str, manifest: dict) -> None:
        """The service's program entry (programs.py): its key as the uvmc-key credential."""
        if self.programs is None:
            return
        if name not in self.programs.registry:
            key = self.programs.add(name, manifest["description"] or manifest["title"], kind="service")
            self._write_secret(name, "uvmc-key", key)

    # --- what it gets at run time --------------------------------------------------------------

    def _write_secret(self, name: str, file: str, value: str) -> None:
        d = self.secrets_dir(name)
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(self.secret_root, 0o700)
        os.chmod(d, 0o700)
        path = d / file
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value)

    def missing(self, name: str) -> list[str]:
        """Settings it needs that aren't filled in yet, and SSH machines without host keys."""
        e = self.get(name)
        if not e["manifest"]:
            return []
        out = []
        for st in e["manifest"]["settings"]:
            if not st["required"]:
                continue
            if st["type"] in FILE_SETTINGS:
                if not (self.secrets_dir(name) / st["name"]).exists():
                    out.append(st["label"])
            elif not str(e["values"].get(st["name"], st["default"])).strip():
                out.append(st["label"])
        for m in e["manifest"]["machines"]:
            if not e["host_keys"].get(m["name"], "").strip():
                out.append(f"host key of {m['name']}")
        return out

    def save_settings(self, name: str, values: dict) -> None:
        """Non-secret settings kept in the registry, secrets and long texts as files; an empty
        secret keeps the stored one. A running service restarts to get them."""
        with self.lock:
            e = self.get(name)
            if not e["manifest"]:
                raise ServiceError("Approve the service first.")
            for st in e["manifest"]["settings"]:
                if st["name"] not in values:
                    continue
                v = str(values[st["name"]])
                if st["type"] in FILE_SETTINGS:
                    if v.strip():
                        self._write_secret(name, st["name"], v.strip() + "\n")
                    continue
                if "\n" in v or len(v) > 2000:
                    raise ServiceError(f"{st['label']}: one line, at most 2000 characters.")
                if st["type"] == "number" and v.strip() and not re.match(r"^-?[0-9]+(\.[0-9]+)?$", v.strip()):
                    raise ServiceError(f"{st['label']}: a number.")
                if st["type"] == "yes/no":
                    v = "yes" if v.strip().lower() in ("yes", "true", "1", "on") else "no"
                e["values"][st["name"]] = v
            self._save()
            self._write_runtime(name)
            if e["enabled"]:
                if self.missing(name):
                    e["enabled"] = False
                    self._save()
                    self._systemctl("disable", "--now", self.unit(name))
                else:
                    self._systemctl("restart", self.unit(name))
        self.notify()

    def set_host_key(self, name: str, machine: str, text: str) -> None:
        with self.lock:
            e = self.get(name)
            if machine not in {m["name"] for m in e["manifest"]["machines"]}:
                raise KeyError(f"{name} declares no machine called {machine}.")
            lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
            for ln in lines:
                if not re.match(r"^\S+ (ssh-|ecdsa-|sk-)\S+ [A-Za-z0-9+/=]+( .*)?$", ln):
                    raise ServiceError("Host keys are known_hosts lines: host, key type, key (from ssh-keyscan).")
            e["host_keys"][machine] = "\n".join(lines)
            self._save()
            self._write_runtime(name)
        self.notify()

    def keyscan(self, name: str, machine: str) -> dict:
        """Reads the machine's host keys (by the supervisor) for you to compare with what you know."""
        e = self.get(name)
        m = next((x for x in e["manifest"]["machines"] if x["name"] == machine), None)
        if m is None:
            raise KeyError(f"{name} declares no machine called {machine}.")
        p = subprocess.run(["ssh-keyscan", "-T", "8", "-t", "ed25519,ecdsa,rsa", m["host"]], capture_output=True, timeout=30)
        lines = [ln for ln in p.stdout.decode(errors="replace").splitlines() if ln and not ln.startswith("#")]
        if not lines:
            raise ServiceError(f"{m['host']} didn't answer on port 22 (from UVMC's machine).")
        fp = subprocess.run(["ssh-keygen", "-lf", "-"], input="\n".join(lines).encode(), capture_output=True, timeout=15)
        return {"lines": "\n".join(lines), "fingerprints": fp.stdout.decode(errors="replace").strip().splitlines()}

    def ssh_config(self, name: str) -> str:
        e = self.get(name)
        out = ["# Written by UVMC from the service's manifest (uvmc-app.toml); changes here are overwritten."]
        for m in e["manifest"]["machines"]:
            out += [f"Host {m['name']}", f"  HostName {m['host']}", f"  User {m['user']}", "  Port 22",
                    f"  IdentityFile ${{CREDENTIALS_DIRECTORY}}/{m['key']}", "  IdentitiesOnly yes",
                    "  UserKnownHostsFile ~/.ssh/known_hosts_uvmc", "  StrictHostKeyChecking yes",
                    f"  ProxyCommand nc -X connect -x 127.0.0.1:{e['port']} %h %p"]
        out += ["Host *", "  BatchMode yes", ""]
        return "\n".join(out)

    def env_file(self, name: str) -> str:
        """The app's non-secret settings as APP_<NAME>="..." lines (systemd EnvironmentFile)."""
        e = self.get(name)
        lines = []
        for st in e["manifest"]["settings"]:
            if st["type"] in FILE_SETTINGS:
                continue
            v = str(e["values"].get(st["name"], st["default"]))
            v = v.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'APP_{st["name"].upper()}="{v}"')
        return "\n".join(lines) + "\n"

    def unit_text(self, name: str, health: bool = False) -> str:
        e = self.get(name)
        m, port, home = e["manifest"], e["port"], self.home(name)
        proxy = f"http://127.0.0.1:{port}"
        env = [f"HOME={home}", f"APP_DATA={home}", f"UVMC_URL=http://127.0.0.1:{API_PORT}", f"UVMC_SERVICE={name}",
               *(f"{k}={proxy}" for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")),
               "NO_PROXY=localhost,127.0.0.1,::1", "no_proxy=localhost,127.0.0.1,::1"]
        creds = ["uvmc-key"] + [s["name"] for s in m["settings"] if s["type"] in FILE_SETTINGS]
        sec = self.secrets_dir(name)
        lines = ["# Written by UVMC (services.py) from the approved manifest; changes here are overwritten.",
                 "[Unit]", f"Description=UVMC service {name}{' (health check)' if health else ''}",
                 "After=network-online.target", "", "[Service]",
                 f"Type={'oneshot' if health else 'simple'}", f"User={self.user(name)}", f"Group={self.cfg.services_group}",
                 f"Slice={SLICE}", f"ExecStart={self.base / name / ('health.sh' if health else 'run.sh')}",
                 *([] if health else [f"Restart={m['restart']}", "RestartSec=5"]),
                 f"TimeoutStartSec={60 if health else 90}", "TimeoutStopSec=15",
                 *(f"Environment={shlex.quote(x)}" for x in env),
                 f"EnvironmentFile=-{sec / 'env'}",
                 *(f"LoadCredential={c}:{sec / c}" for c in creds if (sec / c).exists() or no_sudo()),
                 f"MemoryMax={m['memory']}", "UMask=0077", "NoNewPrivileges=yes",
                 "ProtectSystem=strict", "ProtectHome=yes", f"ReadWritePaths={home}", "PrivateTmp=yes",
                 "PrivateDevices=yes", "ProtectProc=invisible", "ProtectKernelTunables=yes", "ProtectKernelModules=yes",
                 "ProtectKernelLogs=yes", "ProtectControlGroups=yes", "ProtectClock=yes", "ProtectHostname=yes",
                 "RestrictSUIDSGID=yes", "LockPersonality=yes", "RestrictRealtime=yes", "RestrictNamespaces=yes",
                 "CapabilityBoundingSet=", "AmbientCapabilities=", "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
                 "SystemCallArchitectures=native", "StandardOutput=journal", "StandardError=journal"]
        if not health:
            lines += ["", "[Install]", "WantedBy=multi-user.target"]
        return "\n".join(lines) + "\n"

    def run_script(self, name: str, health: bool = False) -> str:
        e = self.get(name)
        folder = self.code(name, e["commit"]) / e["path"] if e["path"] else self.code(name, e["commit"])
        cmd = e["manifest"]["health"] if health else e["manifest"]["run"]
        return f"#!/bin/sh\n# Written by UVMC: the approved manifest's {'health' if health else 'run'} command.\n" \
               f"cd {shlex.quote(str(folder))} || exit 1\nexec {cmd}\n"

    def _write_runtime(self, name: str) -> None:
        """Everything the running service gets from UVMC, from the approved manifest and your settings."""
        e = self.get(name)
        m = e["manifest"]
        self._write_secret(name, "env", self.env_file(name))
        files = {self.base / name / "run.sh": (self.run_script(name), "755", "root:root")}
        if m["health"]:
            files[self.base / name / "health.sh"] = (self.run_script(name, health=True), "755", "root:root")
        files[Path("/etc/systemd/system") / self.unit(name)] = (self.unit_text(name), "644", "root:root")
        if m["health"]:
            files[Path("/etc/systemd/system") / f"{UNIT_PREFIX}{name}-health.service"] = (
                self.unit_text(name, health=True), "644", "root:root")
        user = f"{self.user(name)}:{self.cfg.services_group}"
        ssh = self.home(name) / ".ssh"
        if m["machines"]:
            files[ssh / "config"] = (self.ssh_config(name), "600", user)
            files[ssh / "known_hosts_uvmc"] = ("\n".join(v for v in e["host_keys"].values() if v) + "\n", "600", user)
        if no_sudo():   # tests: the run files next to the service, nothing in /etc
            for path, (text, _mode, _owner) in files.items():
                if str(path).startswith(str(self.base)):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
            return
        if m["machines"]:
            root(["install", "-d", "-m", "700", "-o", self.user(name), "-g", self.cfg.services_group, str(ssh)], timeout=15)
        for path, (text, mode, owner) in files.items():
            p = root(["tee", str(path)], stdin=text.encode(), timeout=15)
            if p.returncode == 0:
                p = root(["chown", owner, str(path)], timeout=15)
            if p.returncode == 0:
                p = root(["chmod", mode, str(path)], timeout=15)
            if p.returncode != 0:
                raise ServiceError(f"Could not write {path}: {p.stderr.decode(errors='replace').strip()}")
        root(["systemctl", "daemon-reload"], timeout=60)
        self.apply_firewall()
        self._restart_proxy(name)

    # --- the walls: firewall and proxy ----------------------------------------------------------

    def apply_firewall(self) -> bool:
        """Each service's user may reach only its own proxy and UVMC's API (on top of setup.sh's
        rule, which keeps the whole group off everything else)."""
        if no_sudo():
            return True
        rules = []
        for name, e in sorted(self.registry.items()):
            if not e.get("installed") or not e.get("port"):
                continue
            try:
                uid = int(subprocess.run(["id", "-u", self.user(name)], capture_output=True, text=True).stdout)
            except ValueError:
                continue
            rules += [f'    meta skuid {uid} oifname "lo" ip daddr 127.0.0.1 tcp dport {{ {e["port"]}, {API_PORT} }} accept',
                      f"    meta skuid {uid} meta l4proto tcp reject with tcp reset",
                      f"    meta skuid {uid} reject with icmpx type admin-prohibited"]
        text = ("# UVMC: each service's user may reach only its own proxy and UVMC's API. Written by the supervisor.\n"
                "table inet uvmc_services\ndelete table inet uvmc_services\ntable inet uvmc_services {\n"
                "  chain output {\n    type filter hook output priority 1; policy accept;\n"
                + "\n".join(rules) + "\n  }\n}\n")
        p = root(["tee", "/etc/uvmc/services.nft"], stdin=text.encode(), timeout=15)
        if p.returncode == 0:
            p = root(["nft", "-f", "/etc/uvmc/services.nft"], timeout=15)
        return p.returncode == 0

    def _restart_proxy(self, name: str) -> None:
        old = self.proxies.pop(name, None)
        if old is not None:
            old.stop()
        e = self.registry.get(name)
        if not e or not e.get("installed"):
            return
        m = e["manifest"]
        try:
            decide = (lambda host, port, n=name: self._ask(n, host, port)) if e.get("session") else (lambda host, port: False)
            self.proxies[name] = Proxy(e["port"], [80, 443], SiteList(m["sites"], Path("/nonexistent/uvmc-svc-sites")),
                                       decide, lambda info, n=name: self._on_net(n, info),
                                       host_ports={x["host"]: [22] for x in m["machines"]}).start()
        except OSError as err:
            print(f"Warning: the proxy for service {name} could not start: {err}")

    def _on_net(self, name: str, info: dict) -> None:
        q = self.net.setdefault(name, collections.deque(maxlen=200))
        q.append({"t": time.time(), "host": info.get("host"), "port": info.get("port"), "verdict": info.get("verdict"),
                  "reason": info.get("reason"), "sent": info.get("sent", 0), "received": info.get("received", 0)})
        if info.get("verdict") == "deny":
            self.on_event({"kind": "service_refused", "name": name, "host": info.get("host"),
                           "port": info.get("port"), "reason": info.get("reason")})

    # --- running it ------------------------------------------------------------------------------

    def _systemctl(self, *args: str) -> subprocess.CompletedProcess | None:
        if no_sudo():
            return None
        return root(["systemctl", *args], timeout=90)

    def set_enabled(self, name: str, on: bool) -> None:
        """On: started now and with the VM. Off: stopped and not started with the VM."""
        with self.lock:
            e = self.get(name)
            if not e["installed"]:
                raise ServiceError("Approve the service first.")
            if on and self.missing(name):
                raise ServiceError("Fill in first: " + ", ".join(self.missing(name)) + ".")
            p = self._systemctl("enable", "--now", self.unit(name)) if on else self._systemctl("disable", "--now", self.unit(name))
            if p is not None and p.returncode != 0:
                raise ServiceError(p.stderr.decode(errors="replace").strip()[-300:])
            e["enabled"] = on
            self._save()
        self.notify()

    def restart(self, name: str) -> None:
        e = self.get(name)
        if not e["enabled"]:
            raise ServiceError("It's switched off; switch it on to start it.")
        p = self._systemctl("restart", self.unit(name))
        if p is not None and p.returncode != 0:
            raise ServiceError(p.stderr.decode(errors="replace").strip()[-300:])
        self.notify()

    def health(self, name: str) -> dict:
        """Runs the health command once, as the service, in its walls."""
        e = self.get(name)
        if not e["installed"] or not e["manifest"]["health"]:
            raise ServiceError("This app declares no health check.")
        if no_sudo():
            return {"ok": None, "text": "(no health checks outside the VM)"}
        unit = f"{UNIT_PREFIX}{name}-health.service"
        p = root(["systemctl", "start", unit], timeout=90)
        show = self._show(unit)
        ok = p.returncode == 0 and show.get("Result") == "success"
        e["health"] = {"ok": ok, "at": time.time(), "text": self.log(name, 20, unit) if not ok else "healthy"}
        self._save()
        self.notify()
        return e["health"]

    def _show(self, unit: str) -> dict:
        if no_sudo():
            return {}
        p = subprocess.run(["systemctl", "show", unit, "-p", "ActiveState,SubState,Result,NRestarts,ExecMainStatus,"
                            "ExecMainCode,ActiveEnterTimestamp,InactiveEnterTimestamp,MemoryCurrent,MainPID,UnitFileState"],
                           capture_output=True, text=True, timeout=15)
        return dict(line.split("=", 1) for line in p.stdout.splitlines() if "=" in line)

    def status(self, name: str) -> dict:
        """How it's doing, in plain words."""
        e = self.get(name)
        if not e["installed"]:
            return {"state": "waiting for approval", "words": "Not installed: waiting for your approval."}
        if e.get("session"):
            who = "you, in a shell" if e["session"]["who"] == "shell" else "an agent"
            return {"state": "installing", "words": f"Being installed by {who}: stopped until the session ends."}
        s = self._show(self.unit(name))
        if not s:
            return {"state": "unknown", "words": "(status is only known in the VM)"}
        active, sub, result = s.get("ActiveState", ""), s.get("SubState", ""), s.get("Result", "")
        mem = s.get("MemoryCurrent", "")
        out = {"state": active, "sub": sub, "result": result, "restarts": int(s.get("NRestarts") or 0),
               "since": s.get("ActiveEnterTimestamp") if active == "active" else s.get("InactiveEnterTimestamp"),
               "memory": int(mem) if mem.isdigit() else None, "pid": int(s.get("MainPID") or 0),
               "memory_max": e["manifest"]["memory"]}
        if active == "active":
            out["words"] = "Running."
        elif active in ("activating", "reloading") and sub == "auto-restart":
            out["words"] = "Restarting after it stopped " + self._why(result, s) + "."
        elif active == "failed":
            out["words"] = "Stopped " + self._why(result, s) + "."
        elif not e["enabled"]:
            out["words"] = "Switched off."
        else:
            out["words"] = f"{active} ({sub})."
        return out

    @staticmethod
    def _why(result: str, s: dict) -> str:
        code = s.get("ExecMainStatus", "")
        return {"exit-code": f"with exit code {code}", "signal": f"by signal {code}", "oom-kill": "for lack of memory",
                "timeout": "after a time-out", "core-dump": "with a crash (core dump)", "start-limit-hit":
                "after failing too often in a row", "success": "normally"}.get(result, result or "for an unknown reason")

    def log(self, name: str, lines: int = 200, unit: str | None = None) -> str:
        """Its log since this service was added: a service removed and added again under the same
        name doesn't show the old one's lines (journald keeps them by unit name)."""
        if no_sudo():
            return ""
        since = int(self.get(name).get("created") or 0)
        p = subprocess.run(["journalctl", "-u", unit or self.unit(name), "--since", f"@{since}",
                            "-n", str(max(1, min(lines, 2000))), "--no-pager", "-o", "short-iso"],
                           capture_output=True, text=True, timeout=20)
        return p.stdout[-200_000:]

    # --- installing in place: sessions, snapshots, the asked network ------------------------------
    # An install session (you in a shell as the service, later an agent) can write the app folder.
    # Before it starts: a snapshot of the app folder; the service is stopped. While it lasts: the
    # service's proxy asks about every site not in its profile (or the session's starting list).
    # When it ends: the service starts again if it was on, and the sites it used are listed.

    def snapshots(self, name: str) -> Checkpoints:
        c = Checkpoints(self.cfg, src=str(self.app(name)), base=self.base / name / "snapshots")
        c.keep = SNAPSHOTS_KEPT
        return c

    def session_marker(self, name: str) -> Path:
        return (Path(self.cfg.state_dir) / "service-sessions" if no_sudo() else Path("/run/uvmc/service-sessions")) / name

    def start_session(self, name: str, who: str = "shell", sites: list | None = None, conv: str | None = None) -> dict:
        """Opens an install session: snapshot, the service stopped, the app folder writable to it,
        the network asked site by site (sites: allowed for this session without asking)."""
        if who not in ("shell", "agent"):
            raise ServiceError("who: shell or agent.")
        allowed = []
        for host in sites or []:
            host = str(host).strip().lower()
            if host:
                if not valid_site(host):
                    raise ServiceError(f"{host} isn't a site name (like pypi.org).")
                allowed.append(host)
        with self.lock:
            e = self.get(name)
            if not e["installed"] or not self.is_local(name):
                raise ServiceError("Install sessions are for apps installed in place (an empty account you approved).")
            if e.get("session"):
                raise ServiceError(f"An install session is already open ({e['session']['who']}, since "
                                   f"{time.strftime('%H:%M', time.localtime(e['session']['started']))}). End it first.")
            snap = self.snapshots(name).create() if not no_sudo() or self.app(name).exists() else ""
            was_on = bool(e["enabled"])
            if was_on:
                self._systemctl("stop", self.unit(name))
            e["session"] = {"who": who, "started": time.time(), "was_on": was_on, "snapshot": snap,
                            "allowed": allowed, "answers": {}, "used": [], "conv": conv}
            self._save()
            self._write_session_files(name)
            proxy = self.proxies.get(name)
            if proxy is not None:
                proxy.decide = lambda host, port, n=name: self._ask(n, host, port)
        self.notify()
        return e["session"]

    def end_session(self, name: str, start_again: bool = True) -> dict:
        """Closes the install session: back to its walls, started again if it was on. Returns the
        sites it reached that its profile doesn't list (offered as a profile change)."""
        with self.lock:
            e = self.get(name)
            sess = e.get("session")
            if not sess:
                raise ServiceError("No install session is open.")
            proxy = self.proxies.get(name)
            if proxy is not None:
                proxy.decide = lambda host, port: False
            for q in self.asks.pop(name, {}).values():   # anything still waiting: refused
                q["result"] = False
                q["event"].set()
            try:
                self.session_marker(name).unlink()
            except OSError:
                pass
            known = set(e["manifest"]["sites"])
            new = sorted({h for h in sess["used"] if h not in known})
            e["session"] = None
            e["last_session"] = {"who": sess["who"], "started": sess["started"], "ended": time.time(),
                                 "snapshot": sess["snapshot"], "new_sites": new}
            self._save()
            if sess["was_on"] and start_again and not self.missing(name):
                self._systemctl("start", self.unit(name))
            elif sess["was_on"] and not start_again:
                e["enabled"] = False
                self._save()
                self._systemctl("disable", self.unit(name))
        self.notify()
        return e["last_session"]

    def _ask(self, name: str, host: str, port: int) -> bool:
        """The service's proxy during a session, for a site its profile doesn't list."""
        e = self.registry.get(name)
        sess = (e or {}).get("session")
        if not sess:
            return False
        if host in sess["allowed"] or sess["answers"].get(host):
            self._used(name, host)
            return True
        if host in sess["answers"]:
            return False
        with self.lock:
            waits = self.asks.setdefault(name, {})
            q = next((x for x in waits.values() if x["host"] == host), None)
            if q is None:
                qid = secrets_mod.token_hex(4)
                q = waits[qid] = {"id": qid, "host": host, "port": port, "t": time.time(), "event": threading.Event(),
                                  "result": False}
                self.on_event({"kind": "service_ask", "name": name, "id": qid, "host": host, "port": port,
                               "who": sess["who"], "conv": sess.get("conv")})
                self.notify()
        q["event"].wait(self.cfg.net_approval_timeout_s)
        with self.lock:
            self.asks.get(name, {}).pop(q["id"], None)
            if (self.registry.get(name) or {}).get("session") is sess:
                sess["answers"].setdefault(host, q["result"])
                self._save()
        if q["result"]:
            self._used(name, host)
        self.notify()
        return q["result"]

    def _used(self, name: str, host: str) -> None:
        sess = (self.registry.get(name) or {}).get("session")
        if sess is not None and host not in sess["used"]:
            sess["used"].append(host)
            self._save()

    def answer(self, name: str, qid: str, allow: bool) -> None:
        """Your answer to a site question of an install session (for this session only)."""
        with self.lock:
            q = self.asks.get(name, {}).get(qid)
            if q is None:
                raise KeyError("That question isn't waiting any more.")
            q["result"] = bool(allow)
            q["event"].set()

    def add_sites(self, name: str, hosts: list) -> dict:
        """The sites a session used, added to the profile kept by UVMC: a profile change for you to approve."""
        e = self.get(name)
        text = e.get("adapter")
        if not text:
            raise ServiceError("Its profile comes from its repository: add the sites there, or edit the profile here.")
        hosts = [str(h).strip().lower() for h in hosts if valid_site(str(h).strip())]
        sites = list(dict.fromkeys(e["manifest"]["sites"] + hosts))
        new_line = "sites = [" + ", ".join(f'"{h}"' for h in sites) + "]"
        lines = text.splitlines()
        section, done = "", False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("[") and not stripped.startswith("[["):
                section = stripped.strip("[]").strip()
            elif stripped.startswith("[["):
                section = ""
            elif section == "network" and re.match(r"^sites\s*=", stripped):
                if "]" not in stripped:
                    raise ServiceError("Its sites list spans several lines: edit the profile by hand.")
                lines[i] = new_line + ("  " + stripped.split("]", 1)[1].strip() if stripped.split("]", 1)[1].strip() else "")
                done = True
                break
        if not done:
            raise ServiceError("Its profile has no sites list under [network]: edit the profile by hand.")
        return self.set_adapter(name, "\n".join(lines) + "\n")

    def set_level(self, name: str, level: str) -> None:
        """Its privacy level (privacy.py): an agent working inside it needs a model at least that level."""
        from . import privacy
        privacy.check(level)
        with self.lock:
            self.get(name)["level"] = level
            self._save()
        self.notify()

    def rollback(self, name: str, snapshot: str) -> None:
        """The app folder as it was before an install session (the service restarted if it's on)."""
        with self.lock:
            e = self.get(name)
            if not self.is_local(name):
                raise ServiceError("Only apps installed in place have snapshots.")
            if e.get("session"):
                raise ServiceError("End the install session first.")
            if e["enabled"]:
                self._systemctl("stop", self.unit(name))
            self.snapshots(name).restore(snapshot)
            if e["enabled"]:
                self._systemctl("start", self.unit(name))
            e["rolled_back"] = {"snapshot": snapshot, "at": time.time()}
            self._save()
        self.notify()

    def session_properties(self, name: str, who: str) -> list[str]:
        """The transient unit's properties for an install session: the running unit's own (user,
        group, box, sandbox) with the app folder writable, without secrets; the agent doesn't see
        the data folder. Read by uvmc-service-shell (and the agent's commands)."""
        e = self.get(name)
        app, home = self.app(name), self.home(name)
        keep = ("User=", "Group=", "Slice=", "EnvironmentFile=", "MemoryMax=", "UMask=", "NoNewPrivileges=",
                "ProtectSystem=", "ProtectHome=", "PrivateTmp=", "PrivateDevices=", "ProtectProc=", "ProtectKernel",
                "ProtectControlGroups=", "ProtectClock=", "ProtectHostname=", "RestrictSUIDSGID=", "LockPersonality=",
                "RestrictRealtime=", "RestrictNamespaces=", "CapabilityBoundingSet=", "AmbientCapabilities=",
                "RestrictAddressFamilies=", "SystemCallArchitectures=")
        props = [ln for ln in self.unit_text(name).splitlines() if ln.startswith(keep)]
        env = [ln[len("Environment="):].strip("'") for ln in self.unit_text(name).splitlines() if ln.startswith("Environment=")]
        env = [x for x in env if not x.startswith(("HOME=", "UVMC_URL="))] + [f"HOME={app}"]
        props += [f"Environment={x}" for x in env]
        props += [f"WorkingDirectory={app}", f"ReadWritePaths={app}" + ("" if who == "agent" else f" {home}")]
        if who == "agent":
            props.append(f"InaccessiblePaths={home}")
        props.append("TimeoutStopSec=15")
        return props

    def _write_session_files(self, name: str) -> None:
        """The session's properties for uvmc-service-shell, and the marker that a session is open."""
        files = {self.base / name / f"session-{who}.properties": "\n".join(self.session_properties(name, who)) + "\n"
                 for who in ("shell", "agent")}
        marker = self.session_marker(name)
        if no_sudo():
            for path, text in files.items():
                path.write_text(text, encoding="utf-8")
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(self.get(name)["session"]["who"], encoding="utf-8")
            return
        for path, text in files.items():
            p = root(["tee", str(path)], stdin=text.encode(), timeout=15)
            if p.returncode == 0:
                root(["chown", "root:root", str(path)], timeout=15)
                root(["chmod", "640", str(path)], timeout=15)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(self.get(name)["session"]["who"], encoding="utf-8")

    # --- removing one ----------------------------------------------------------------------------

    def remove(self, name: str, keep_data: bool = False) -> None:
        with self.lock:
            e = self.get(name)
            for q in self.asks.pop(name, {}).values():   # an open install session ends with it
                q["event"].set()
            try:
                self.session_marker(name).unlink()
            except OSError:
                pass
            if e["installed"] and not no_sudo():
                self._systemctl("disable", "--now", self.unit(name))
                for unit in (self.unit(name), f"{UNIT_PREFIX}{name}-health.service"):
                    root(["rm", "-f", "--", f"/etc/systemd/system/{unit}"], timeout=15)
                root(["systemctl", "daemon-reload"], timeout=60)
                root(["pkill", "-KILL", "-u", self.user(name)], timeout=15)
            old = self.proxies.pop(name, None)
            if old is not None:
                old.stop()
            del self.registry[name]
            self._save()
            self.apply_firewall()
            if self.programs is not None and name in self.programs.registry:
                self.programs.remove(name)
            shutil.rmtree(self.secrets_dir(name), ignore_errors=True)
            folder = self.base / name
            keep = self.home(name) if keep_data else None
            if no_sudo():
                for child in folder.iterdir() if folder.exists() else []:
                    if child != keep:
                        shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink()
                if not keep:
                    shutil.rmtree(folder, ignore_errors=True)
            else:
                for child in folder.iterdir() if folder.exists() else []:
                    if child != keep:
                        root(["rm", "-rf", "--", str(child)], timeout=120)
                if keep:
                    root(["chown", "-R", "root:root", str(keep)], timeout=60)
                else:
                    root(["rm", "-rf", "--", str(folder)], timeout=60)
                    root(["userdel", self.user(name)], timeout=30)
        self.notify()

    # --- what the panel shows --------------------------------------------------------------------

    def view(self, name: str | None = None, detail: bool = False) -> list[dict] | dict:
        out = []
        for n, e in sorted(self.registry.items()):
            if name and n != name:
                continue
            m = e.get("manifest")
            item = {"name": n, "title": (m or (e.get("proposal") or {}).get("manifest") or {}).get("title", n),
                    "description": (m or (e.get("proposal") or {}).get("manifest") or {}).get("description", ""),
                    "url": e["url"], "branch": e["branch"], "path": e["path"], "installed": e["installed"],
                    "adapter": e.get("adapter"), "manifest_from": "uvmc" if e.get("adapter") else "repository",
                    "commit": e["commit"], "subject": e.get("subject", ""), "enabled": e["enabled"],
                    "manifest": m, "proposal": e.get("proposal"), "checked": e.get("checked"),
                    "check_error": e.get("check_error"), "missing": self.missing(n) if m else [],
                    "status": self.status(n), "health": e.get("health"),
                    "source": (e.get("source") or {}).get("kind", "git"), "level": e.get("level", "open"),
                    "session": {k: v for k, v in e["session"].items() if k != "answers"} if e.get("session") else None,
                    "last_session": e.get("last_session"),
                    "asks": [{"id": q["id"], "host": q["host"], "port": q["port"], "t": q["t"]}
                             for q in self.asks.get(n, {}).values()],
                    "refused": sum(1 for x in self.net.get(n, ()) if x["verdict"] == "deny")}
            if detail and m:
                values = []
                for st in m["settings"]:
                    v = dict(st)
                    if st["type"] in FILE_SETTINGS:
                        v["set"] = (self.secrets_dir(n) / st["name"]).exists()
                    else:
                        v["value"] = e["values"].get(st["name"], st["default"])
                    values.append(v)
                if (e.get("source") or {}).get("kind") == LOCAL:
                    item["snapshots"] = self.snapshots(n).list()[::-1] if self.app(n).exists() else []
                item.update({"settings": values, "host_keys": e["host_keys"], "net": list(self.net.get(n, ()))[::-1][:100],
                             "log": self.log(n), "has_token": bool(self._token(n))})
            out.append(item)
        if name:
            if not out:
                raise KeyError(f"There's no service called {name}.")
            return out[0]
        return out

    # --- at supervisor start -----------------------------------------------------------------------

    def start(self) -> None:
        """Firewall and proxies for the installed services (their units run on their own), and the
        regular look for new commits."""
        self.apply_firewall()
        for name, e in self.registry.items():
            if e.get("installed"):
                self._restart_proxy(name)
        # An agent's install session belongs to its conversation, which a restart doesn't bring
        # back: it ends (the service starts again if it was on). A shell's stays until you end it.
        for name, e in list(self.registry.items()):
            if (e.get("session") or {}).get("who") == "agent":
                try:
                    self.end_session(name)
                except Exception as err:   # never keeps the supervisor from starting
                    print(f"Warning: could not end the agent's install session of {name}: {err}")

        def loop():
            while True:
                time.sleep(self.cfg.services_check_minutes * 60)
                try:
                    self.check_updates()
                except Exception as err:   # never let the check stop
                    print(f"Warning: the services' update check failed: {err}")
        threading.Thread(target=loop, daemon=True, name="uvmc-services").start()
