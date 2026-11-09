"""Tools the agent builds: small programs that keep working after the conversation ends.

The agent writes a tool in its work folder (code plus a uvmc-tool.json that says what it is,
how it runs, which settings and secrets it needs and which sites it must reach), then calls
propose_tool. You see an approval card with all of that and the code (or the changes, for a
new version). Once you approve, the supervisor freezes a copy the agent can't edit, and the
tool appears in the panel's Tools tab: on/off, run now, its settings form, its runs and logs.

Each tool runs:
- as its own Linux user (uvmc-t-<name>), from the frozen copy, which it can read but not
  change; its own home folder keeps its data between runs;
- inside the tools' resource box (uvmc-tools.slice), with a time limit per run;
- with one way online: its own proxy, which lets through only the sites it declared
  (the firewall gives that user nothing else, and only its own proxy);
- with its settings and secrets handed over in its environment only when it runs.
The agent never sees the secrets you type into a tool's settings.
"""

from __future__ import annotations

import base64
import datetime as dt
import difflib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import threading
import time
from pathlib import Path
from typing import Callable

from .executor import no_sudo, root
from .netproxy import Proxy, SiteList, valid_site
from . import reports

MANIFEST = "uvmc-tool.json"
NAME = re.compile(r"^[a-z][a-z0-9-]{1,23}$")
SETTING = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_FILES, MAX_BYTES, MAX_SHOWN = 200, 5_000_000, 150_000
SLICE = "uvmc-tools.slice"

FORMAT_HELP = """A tool is a folder in your work folder with its code and a uvmc-tool.json:
{
  "name": "inbox-digest",             lowercase letters, digits and -, 2 to 24 characters
  "title": "Inbox digest",
  "description": "What it does, in one or two sentences, for the user.",
  "command": "python3 main.py",        run from the tool's folder
  "schedule": {"every_minutes": 60}    or {"daily_at": "08:00"}, or null: only when the user runs it
  "timeout_seconds": 300,              at most 3600
  "sites": ["api.example.com"],        the only sites it can reach (HTTPS/HTTP through a proxy that
                                       HTTP_PROXY/HTTPS_PROXY point to; other protocols don't work)
  "settings": [                        filled in by the user in the panel
    {"name": "email", "label": "Your email address", "type": "text", "required": true},
    {"name": "api_key", "label": "API key", "type": "secret", "required": true}
  ],
  "setup": "Steps the user must take first (e.g. create an app password), shown in the panel."
}
When it runs, it gets each setting as an environment variable UVMC_SETTING_<NAME> (uppercase),
its own data folder in HOME (kept between runs), and its output (stdout and stderr) is its log.
It runs as its own user with the programs installed on the machine; it can't install packages.
The approved copy is frozen: to change the tool, edit your folder and propose it again.

""" + reports.FORMAT_HELP
NOTIFY_MODES = ("never", "asks", "always")
REPORT_FILE = "uvmc-report.json"   # in the tool's HOME; its path is in UVMC_REPORT


class ToolError(Exception):
    pass


def validate(raw: dict) -> dict:
    """The manifest, checked and normalized. Raises ToolError with what to fix."""
    if not isinstance(raw, dict):
        raise ToolError(f"{MANIFEST} must be a JSON object.")
    name = str(raw.get("name", ""))
    if not NAME.match(name):
        raise ToolError("name: use 2 to 24 lowercase letters, digits and -, starting with a letter.")
    command = str(raw.get("command", "")).strip()
    if not command or len(command) > 500:
        raise ToolError("command: say how to start it, e.g. \"python3 main.py\".")
    schedule = raw.get("schedule")
    if schedule in (None, {}, "", "manual"):
        schedule = None
    elif isinstance(schedule, dict) and "every_minutes" in schedule:
        try:
            every = int(schedule["every_minutes"])
        except (TypeError, ValueError):
            raise ToolError("schedule.every_minutes must be a number.") from None
        if every < 5:
            raise ToolError("schedule.every_minutes: at least 5.")
        schedule = {"every_minutes": every}
    elif isinstance(schedule, dict) and "daily_at" in schedule:
        at = str(schedule["daily_at"])
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", at):
            raise ToolError("schedule.daily_at: a time like \"08:00\".")
        schedule = {"daily_at": at}
    else:
        raise ToolError('schedule: {"every_minutes": 60}, {"daily_at": "08:00"} or null.')
    try:
        timeout = int(raw.get("timeout_seconds", 300))
    except (TypeError, ValueError):
        raise ToolError("timeout_seconds must be a number.") from None
    if not 5 <= timeout <= 3600:
        raise ToolError("timeout_seconds: between 5 and 3600.")
    sites = raw.get("sites", [])
    if not isinstance(sites, list) or len(sites) > 20:
        raise ToolError("sites: a list of up to 20 site names.")
    clean_sites = []
    for s in sites:
        s = str(s).strip().lower().lstrip("*.")
        if not valid_site(s):
            raise ToolError(f"sites: '{s}' is not a site name (like api.example.com).")
        clean_sites.append(s)
    settings = raw.get("settings", [])
    if not isinstance(settings, list) or len(settings) > 30:
        raise ToolError("settings: a list of up to 30 settings.")
    clean_settings, seen = [], set()
    for st in settings:
        if not isinstance(st, dict) or not SETTING.match(str(st.get("name", ""))):
            raise ToolError("settings: each needs a name of lowercase letters, digits and _.")
        if st["name"] in seen:
            raise ToolError(f"settings: '{st['name']}' appears twice.")
        seen.add(st["name"])
        kind = str(st.get("type", "text"))
        if kind not in ("text", "secret", "number", "yes/no", "long text"):
            raise ToolError(f"settings.{st['name']}.type: text, secret, number, yes/no or long text.")
        clean_settings.append({"name": st["name"], "label": str(st.get("label") or st["name"])[:120],
                               "type": kind, "required": bool(st.get("required")),
                               "default": "" if kind == "secret" else str(st.get("default", ""))[:2000],
                               "help": str(st.get("help", ""))[:500]})
    return {"name": name, "title": str(raw.get("title") or name)[:80],
            "description": str(raw.get("description", ""))[:1000], "command": command,
            "schedule": schedule, "timeout_seconds": timeout, "sites": clean_sites,
            "settings": clean_settings, "setup": str(raw.get("setup", ""))[:3000]}


def schedule_text(schedule: dict | None) -> str:
    if not schedule:
        return "only when you run it"
    if "every_minutes" in schedule:
        m = schedule["every_minutes"]
        return f"every {m // 60} h" if m % 60 == 0 and m >= 60 else f"every {m} min"
    return f"every day at {schedule['daily_at']}"


def next_run(schedule: dict | None, last_start: float | None, now: float) -> float | None:
    if not schedule:
        return None
    if "every_minutes" in schedule:
        return (last_start or now - schedule["every_minutes"] * 60) + schedule["every_minutes"] * 60
    hh, mm = map(int, schedule["daily_at"].split(":"))
    base = dt.datetime.fromtimestamp(now).replace(hour=hh, minute=mm, second=0, microsecond=0)
    t = base.timestamp()
    if last_start and last_start >= t - 60:   # already ran today
        t = (base + dt.timedelta(days=1)).timestamp()
    elif t < now - 60 and not last_start:
        t = (base + dt.timedelta(days=1)).timestamp()
    return t


def _is_text(data: bytes) -> bool:
    if b"\x00" in data[:8000]:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


class ToolBox:
    """The approved tools: their frozen copies, settings, users, proxies, schedule and runs."""

    def __init__(self, cfg, notify: Callable[[], None] = lambda: None,
                 on_run: Callable[[str, dict], None] = lambda name, record: None):
        self.cfg = cfg
        self.on_run = on_run   # a run ended: its record (the panel shows it as a card)
        self.base = Path(cfg.made_tools_dir)
        self.secrets_dir = Path(cfg.state_dir) / "tool-secrets"
        self.logs = Path(cfg.log_dir).parent / "tools"
        self.notify = notify
        self.lock = threading.RLock()
        self.running: dict[str, dict] = {}       # name -> {"proc", "started", "run"}
        self.proxies: dict[str, Proxy] = {}
        self.registry: dict[str, dict] = self._load()

    # --- storage ------------------------------------------------------------------

    def _load(self) -> dict:
        try:
            return json.loads((self.base / "registry.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        tmp = self.base / "registry.json.new"
        tmp.write_text(json.dumps(self.registry, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o640)
        tmp.replace(self.base / "registry.json")

    def _secrets(self, name: str) -> dict:
        try:
            return json.loads((self.secrets_dir / f"{name}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_secrets(self, name: str, values: dict) -> None:
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.secrets_dir, 0o700)
        path = self.secrets_dir / f"{name}.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(values, f)

    def version_dir(self, name: str, version: int) -> Path:
        return self.base / name / "versions" / str(version)

    def home(self, name: str) -> Path:
        return self.base / name / "home"

    def user(self, name: str) -> str:
        return f"uvmc-t-{name}"

    # --- a proposal from the agent ----------------------------------------------------------

    def read_folder(self, as_agent, folder: str) -> tuple[dict, dict[str, bytes]]:
        """The tool's files from the agent's work folder (read as the agent), and its manifest."""
        folder = folder.strip() or "."
        p = as_agent(': uvmc-panel; cd -- "$1" && tar -cf - --exclude=__pycache__ --exclude=.git '
                     '--exclude=node_modules --exclude=.venv .', folder, timeout=60)
        if p.returncode != 0:
            raise ToolError(f"Could not read {folder}: {p.stderr.decode(errors='replace').strip()}")
        if len(p.stdout) > MAX_BYTES * 2:
            raise ToolError(f"The folder is too big (over {MAX_BYTES // 1_000_000} MB). A tool is code, not data.")
        files: dict[str, bytes] = {}
        total = 0
        with tarfile.open(fileobj=io.BytesIO(p.stdout)) as tar:
            for m in tar.getmembers():
                rel = os.path.normpath(m.name)
                if m.isdir() or rel == ".":
                    continue
                if not m.isfile() or rel.startswith("..") or os.path.isabs(rel):
                    raise ToolError(f"{rel}: only plain files can be part of a tool (no links).")
                data = tar.extractfile(m).read()
                total += len(data)
                files[rel] = data
                if len(files) > MAX_FILES or total > MAX_BYTES:
                    raise ToolError(f"A tool can have at most {MAX_FILES} files and {MAX_BYTES // 1_000_000} MB.")
                if m.mode & 0o100:
                    files[rel + "\0x"] = b""   # remember the executable bit
        if MANIFEST not in files:
            raise ToolError(f"There's no {MANIFEST} in {folder}.\n\n{FORMAT_HELP}")
        try:
            raw = json.loads(files[MANIFEST])
        except ValueError as e:
            raise ToolError(f"{MANIFEST} is not valid JSON: {e}") from None
        return validate(raw), files

    def preview(self, manifest: dict, files: dict[str, bytes]) -> dict:
        """What the approval card shows: the manifest, the files, and the code or the changes."""
        name = manifest["name"]
        current = self.registry.get(name)
        plain = {k: v for k, v in files.items() if not k.endswith("\0x")}
        old_files: dict[str, bytes] = {}
        if current:
            vdir = self.version_dir(name, current["version"])
            for path in vdir.rglob("*"):
                if path.is_file():
                    try:
                        old_files[str(path.relative_to(vdir))] = path.read_bytes()
                    except OSError:
                        pass
        shown, budget = [], MAX_SHOWN
        for rel in sorted(set(plain) | set(old_files)):
            new, old = plain.get(rel), old_files.get(rel)
            if new == old:
                continue
            if new is not None and not _is_text(new) or old is not None and not _is_text(old):
                shown.append({"path": rel, "binary": True, "size": len(new or b"")})
                continue
            if current:
                text = "".join(difflib.unified_diff(
                    (old or b"").decode().splitlines(True), (new or b"").decode().splitlines(True),
                    f"version {current['version']}/{rel}", f"new/{rel}", n=3))
            else:
                text = new.decode()
            if len(text) > budget:
                text = text[:max(0, budget)] + "\n... (cut off here; the whole file is in the agent's folder)"
            budget -= len(text)
            shown.append({"path": rel, "text": text, "change": "added" if old is None else
                          "removed" if new is None else "changed"})
        return {"manifest": manifest, "update": bool(current), "sample": self.sample(plain),
                "current_version": current["version"] if current else None,
                "files": [{"path": k, "size": len(v)} for k, v in sorted(plain.items())],
                "shown": shown, "schedule_text": schedule_text(manifest["schedule"])}

    def sample(self, files: dict[str, bytes]) -> dict | None:
        """The report the agent left in the folder when it tested the tool: checked (its problems
        go back to the agent before you're asked), and shown on the approval card."""
        data = files.get(REPORT_FILE)
        if data is None:
            return None
        report, problems = reports.parse(data)
        if problems:
            raise ToolError(f"The sample report ({REPORT_FILE}) has problems; fix them and propose again:\n- "
                            + "\n- ".join(problems[:20]) + "\n\n" + reports.FORMAT_HELP)
        budget = 1_500_000   # its images go on the card itself, so keep them small
        def fix(blocks):
            nonlocal budget
            for b in blocks:
                if b["type"] == "image":
                    data = files.get(b["file"])
                    kind = reports.image_type(data or b"")
                    if kind is None or len(data) > reports.MAX_IMAGE:
                        raise ToolError(f"The sample report's image {b['file']} is missing or not a PNG, JPEG, "
                                        "GIF or WebP up to 2 MB (in the test, images come from the folder).")
                    if len(data) <= budget:
                        budget -= len(data)
                        b["src"] = f"data:{kind};base64,{base64.b64encode(data).decode()}"
                for inner in (b.get("columns") or []) + [b.get("blocks") or []]:
                    fix(inner)
        fix(report["blocks"])
        report["images"] = 0
        return report

    def install(self, manifest: dict, files: dict[str, bytes]) -> int:
        """Freezes an approved version and makes the tool's user, home and way online."""
        name = manifest["name"]
        with self.lock:
            entry = self.registry.get(name)
            version = (entry["version"] + 1) if entry else 1
            vdir = self.version_dir(name, version)
            if vdir.exists():
                shutil.rmtree(vdir)
            vdir.mkdir(parents=True)
            for rel, data in files.items():
                if rel.endswith("\0x"):
                    continue
                target = vdir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                os.chmod(target, 0o750 if (rel + "\0x") in files else 0o640)
            for d in [self.base / name, vdir.parent, vdir, *[p for p in vdir.rglob("*") if p.is_dir()]]:
                os.chmod(d, 0o2750)   # the tool's group reads; the agent sees nothing
            (vdir.parent.parent / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
            if entry is None:
                entry = {"enabled": False, "values": {}, "created": time.time(), "runs": [],
                         "port": self._free_port()}
            entry.update({"version": version, "manifest": manifest, "updated": time.time()})
            # Settings the new version no longer has are dropped; new ones start empty.
            names = {s["name"] for s in manifest["settings"]}
            entry["values"] = {k: v for k, v in entry.get("values", {}).items() if k in names}
            secrets = {k: v for k, v in self._secrets(name).items() if k in names}
            self._save_secrets(name, secrets)
            if entry["enabled"] and self.missing(name, entry, secrets):
                entry["enabled"] = False
            self.registry[name] = entry
            self._save()
            self._make_user(name)
            self.apply_firewall()
            self._restart_proxy(name)
        self.notify()
        return version

    def _free_port(self) -> int:
        used = {e.get("port") for e in self.registry.values()}
        for port in range(self.cfg.made_tools_port, self.cfg.made_tools_port + 100):
            if port not in used:
                return port
        raise ToolError("No free port for another tool's proxy (100 tools at most).")

    def _make_user(self, name: str) -> None:
        home = self.home(name)
        home.mkdir(parents=True, exist_ok=True)
        if no_sudo():
            return
        user, group = self.user(name), self.cfg.made_tools_group
        if subprocess.run(["id", "-u", user], capture_output=True).returncode != 0:
            p = root(["useradd", "--system", "--no-create-home", "--home-dir", str(home), "--shell",
                      "/usr/sbin/nologin", "--gid", group, user], timeout=30)
            if p.returncode != 0:
                raise ToolError(f"Could not make the tool's user: {p.stderr.decode(errors='replace').strip()}")
        root(["chown", f"{user}:{group}", str(home)], timeout=30)
        root(["chmod", "700", str(home)], timeout=30)

    # --- the walls: firewall and proxy --------------------------------------------------------

    def apply_firewall(self) -> bool:
        """Each tool's user may only reach its own proxy (on top of the base rule in setup.sh,
        which keeps every tool user off the internet)."""
        if no_sudo():
            return True
        rules = []
        for name, e in sorted(self.registry.items()):
            try:
                uid = int(subprocess.run(["id", "-u", self.user(name)], capture_output=True, text=True).stdout)
            except ValueError:
                continue
            rules += [f'    meta skuid {uid} oifname "lo" ip daddr 127.0.0.1 tcp dport {e["port"]} accept',
                      f"    meta skuid {uid} meta l4proto tcp reject with tcp reset",
                      f"    meta skuid {uid} reject with icmpx type admin-prohibited"]
        text = ("# UVMC: each tool's user may only reach its own proxy. Written by the supervisor.\n"
                "table inet uvmc_tools\ndelete table inet uvmc_tools\ntable inet uvmc_tools {\n"
                "  chain output {\n    type filter hook output priority 1; policy accept;\n"
                + "\n".join(rules) + "\n  }\n}\n")
        p = root(["tee", "/etc/uvmc/tools.nft"], stdin=text.encode(), timeout=15)
        if p.returncode == 0:
            p = root(["nft", "-f", "/etc/uvmc/tools.nft"], timeout=15)
        return p.returncode == 0

    def _restart_proxy(self, name: str) -> None:
        old = self.proxies.pop(name, None)
        if old is not None:
            old.stop()
        e = self.registry.get(name)
        if e is None:
            return
        sites = SiteList(e["manifest"]["sites"], Path("/nonexistent/uvmc-tool-sites"))
        try:
            self.proxies[name] = Proxy(e["port"], [80, 443], sites, lambda host, port: False,
                                       lambda info, n=name: self._on_net(n, info)).start()
        except OSError as err:
            print(f"Warning: the proxy for tool {name} could not start: {err}")

    def _on_net(self, name: str, info: dict) -> None:
        run = self.running.get(name)
        if run is not None:
            run["net"].append(info)

    def start(self) -> None:
        """At supervisor start: firewall, proxies, and the schedule."""
        self.apply_firewall()
        for name in list(self.registry):
            self._restart_proxy(name)
        threading.Thread(target=self._schedule_loop, name="uvmc-made-tools", daemon=True).start()

    # --- settings and switches ---------------------------------------------------------------

    def missing(self, name: str, entry: dict | None = None, secrets: dict | None = None) -> list[str]:
        e = entry or self.registry[name]
        secrets = self._secrets(name) if secrets is None else secrets
        out = []
        for st in e["manifest"]["settings"]:
            have = secrets.get(st["name"]) if st["type"] == "secret" else e["values"].get(st["name"], st["default"])
            if st["required"] and not str(have or "").strip():
                out.append(st["label"])
        return out

    def get(self, name: str) -> dict:
        e = self.registry.get(name)
        if e is None:
            raise KeyError(f"There's no tool called {name}.")
        return e

    def save_settings(self, name: str, values: dict) -> None:
        with self.lock:
            e = self.get(name)
            secrets = self._secrets(name)
            for st in e["manifest"]["settings"]:
                if st["name"] not in values:
                    continue
                v = values[st["name"]]
                if st["type"] == "secret":
                    v = str(v or "")
                    if v == "":
                        continue                    # empty: keep the stored one
                    if v == "\0clear":
                        secrets.pop(st["name"], None)
                    else:
                        secrets[st["name"]] = v[:10000]
                elif st["type"] == "yes/no":
                    e["values"][st["name"]] = "yes" if v in (True, "yes", "true", "1", 1) else "no"
                elif st["type"] == "number":
                    v = str(v).strip()
                    if v and not re.match(r"^-?\d+(\.\d+)?$", v):
                        raise ToolError(f"{st['label']}: a number.")
                    e["values"][st["name"]] = v
                else:
                    e["values"][st["name"]] = str(v)[:20000]
            self._save_secrets(name, secrets)
            self._save()
        self.notify()

    def set_enabled(self, name: str, on: bool) -> None:
        with self.lock:
            e = self.get(name)
            if on:
                missing = self.missing(name)
                if missing:
                    raise ToolError("Fill in its settings first: " + ", ".join(missing) + ".")
            e["enabled"] = bool(on)
            self._save()
        self.notify()

    def set_notify(self, name: str, mode: str) -> None:
        """When the panel pops up a browser notification after a run: "never"; "asks" (when the
        run fails, its report's status is bad, or the report asks with "notify"); "always"."""
        if mode not in NOTIFY_MODES:
            raise ToolError("notify: never, asks or always.")
        with self.lock:
            self.get(name)["notify"] = mode
            self._save()
        self.notify()

    def remove(self, name: str) -> None:
        with self.lock:
            self.get(name)
            if name in self.running:
                raise ToolError("It's running. Wait for this run to end, or stop it.")
            old = self.proxies.pop(name, None)
            if old is not None:
                old.stop()
            self.registry.pop(name)
            self._save()
            try:
                (self.secrets_dir / f"{name}.json").unlink()
            except OSError:
                pass
            if no_sudo():
                shutil.rmtree(self.base / name, ignore_errors=True)
            else:
                root(["rm", "-rf", "--", str(self.base / name)], timeout=120)
                root(["userdel", self.user(name)], timeout=30)
            self.apply_firewall()
        self.notify()

    # --- runs ------------------------------------------------------------------------------

    def command(self, name: str, e: dict, env: dict) -> list[str]:
        m = e["manifest"]
        inner = ["timeout", "-k", "5", str(m["timeout_seconds"]), "bash", "-c", 'rm -f -- "$UVMC_REPORT"; cd -- "$1" && exec bash -c "$2"',
                 "uvmc-tool", str(self.version_dir(name, e["version"])), m["command"]]
        if no_sudo():
            return inner
        keep = ",".join(k for k in env if k != "HOME")
        user, group = self.user(name), self.cfg.made_tools_group
        # HOME on the command line (sudo may reset it); settings only in the environment,
        # where other users can't read them.
        inner = ["env", f"HOME={self.home(name)}", f"USER={user}", f"LOGNAME={user}", *inner]
        if os.path.isdir("/run/systemd/system"):
            return ["sudo", "-n", f"--preserve-env={keep}", "systemd-run", "--scope", "--quiet",
                    f"--slice={SLICE}", "--", "setpriv", f"--reuid={user}", f"--regid={group}",
                    "--clear-groups", "--", *inner]
        return ["sudo", "-n", f"--preserve-env={keep}", "-u", user, "-g", group, "--", *inner]

    def env(self, name: str, e: dict) -> dict:
        proxy = f"http://127.0.0.1:{e['port']}"
        env = {"HOME": str(self.home(name)), "UVMC_TOOL": name, "UVMC_DATA": str(self.home(name)),
               "UVMC_REPORT": str(self.home(name) / REPORT_FILE),
               "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "http_proxy": proxy, "https_proxy": proxy,
               "NO_PROXY": "localhost,127.0.0.1,::1", "no_proxy": "localhost,127.0.0.1,::1"}
        secrets = self._secrets(name)
        for st in e["manifest"]["settings"]:
            v = secrets.get(st["name"], "") if st["type"] == "secret" else e["values"].get(st["name"], st["default"])
            env[f"UVMC_SETTING_{st['name'].upper()}"] = str(v or "")
        return env

    def run(self, name: str, why: str = "you") -> dict:
        """Starts a run in the background. why: "you" or "schedule"."""
        with self.lock:
            e = self.get(name)
            if name in self.running:
                raise ToolError("It's already running.")
            missing = self.missing(name)
            if missing:
                raise ToolError("Fill in its settings first: " + ", ".join(missing) + ".")
            if name not in self.proxies:
                self._restart_proxy(name)
            run = {"id": time.strftime("%Y%m%d-%H%M%S"), "started": time.time(), "why": why,
                   "version": e["version"], "net": []}
            self.running[name] = run
        threading.Thread(target=self._run, args=(name, run), daemon=True).start()
        self.notify()
        return {"id": run["id"]}

    def stop(self, name: str) -> None:
        run = self.running.get(name)
        if run is None or run.get("proc") is None:
            raise ToolError("It isn't running.")
        run["stopped"] = True
        try:
            if no_sudo():
                run["proc"].kill()
            else:
                root(["pkill", "-KILL", "-u", self.user(name)], timeout=15)
        except OSError:
            pass

    def _run(self, name: str, run: dict) -> None:
        e = self.registry[name]
        env = self.env(name, e)
        full_env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", **env}
        log_dir = self.logs / name
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{run['id']}.log"
        code, note = None, ""
        try:
            with open(path, "wb") as out:
                proc = subprocess.Popen(self.command(name, e, env), stdout=out, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, env=full_env,
                                        cwd="/" if not no_sudo() else str(self.version_dir(name, e["version"])))
                run["proc"] = proc
                try:
                    code = proc.wait(timeout=e["manifest"]["timeout_seconds"] + 30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    code = proc.wait()
            if code == 124:
                note = f"Stopped after its time limit ({e['manifest']['timeout_seconds']} s)."
            elif run.get("stopped"):
                note = "Stopped by you."
        except OSError as err:
            note = f"Could not start: {err}"
        size = path.stat().st_size if path.exists() else 0
        if size > 1_000_000:   # keep the end, where the error usually is
            data = path.read_bytes()[-1_000_000:]
            path.write_bytes(b"(the beginning was cut off)\n" + data)
        report = self._take_report(name, run["id"], log_dir)
        record = {"id": run["id"], "started": run["started"], "ended": time.time(), "why": run["why"],
                  "version": run["version"], "exit": code, "ok": code == 0 and not note, "note": note,
                  "report": report, "seen": False,
                  "net": [{"host": n.get("host"), "verdict": n.get("verdict"), "bytes_out": n.get("bytes_out"),
                           "bytes_in": n.get("bytes_in")} for n in run["net"][-50:]]}
        with self.lock:
            self.running.pop(name, None)
            e = self.registry.get(name)
            if e is not None:
                e["runs"] = (e.get("runs", []) + [record])[-self.cfg.made_tools_keep_runs:]
                e["last_start"] = run["started"]
                self._save()
                keep = {r["id"] for r in e["runs"]}
                for old in log_dir.iterdir():
                    if old.name.split(".")[0] not in keep:
                        shutil.rmtree(old, ignore_errors=True) if old.is_dir() else old.unlink(missing_ok=True)
        self.notify()
        try:
            self.on_run(name, record)
        except Exception as err:   # the card is a nicety; the run is recorded either way
            print(f"Warning: could not announce the run of {name}: {err}")

    # --- reports ---------------------------------------------------------------------------

    def _read_as_tool(self, name: str, rel: str, limit: int) -> bytes | None:
        """A file from the tool's HOME, read as the tool's own user: a link it plants can't
        make the supervisor read anything the tool couldn't read itself."""
        path = self.home(name) / rel
        if no_sudo():
            try:
                if path.is_symlink() or not path.is_file():
                    return None
                with open(path, "rb") as f:
                    return f.read(limit + 1)
            except OSError:
                return None
        p = root(["setpriv", f"--reuid={self.user(name)}", f"--regid={self.cfg.made_tools_group}", "--clear-groups",
                  "timeout", "5", "head", "-c", str(limit + 1), "--", str(path)], timeout=15)
        return p.stdout if p.returncode == 0 else None

    def _take_report(self, name: str, run_id: str, log_dir: Path) -> dict | None:
        """The run's report, checked, saved next to its log with its images. A summary for the record."""
        data = self._read_as_tool(name, REPORT_FILE, reports.MAX_REPORT)
        if data is None:
            return None
        report, problems = reports.parse(data)
        if report is not None:
            reports.attach_images(report, lambda rel: self._read_as_tool(name, rel, reports.MAX_IMAGE),
                                  log_dir / f"{run_id}.images", problems)
        saved = {"report": report, "problems": problems[:20]}
        (log_dir / f"{run_id}.report.json").write_text(json.dumps(saved), encoding="utf-8")
        return {"title": report["title"] if report else "", "status": report["status"] if report else None,
                "problems": len(problems), "stats": reports.first_stats(report)[:4] if report else []}

    def result(self, name: str, run_id: str) -> dict:
        """One run as the panel's card shows it: its record, its report, or what it printed."""
        e = self.get(name)
        record = next((r for r in e.get("runs", []) if r["id"] == run_id), None)
        if record is None:
            raise KeyError("No such run.")
        out = {"name": name, "title": e["manifest"]["title"], "run": record, "report": None, "problems": [],
               "notify": e.get("notify", "asks")}
        if record.get("report"):
            try:
                out.update(json.loads((self.logs / name / f"{run_id}.report.json").read_text(encoding="utf-8")))
            except (OSError, ValueError):
                out["problems"] = ["The report is gone."]
        if out["report"] is None:
            try:
                text = self.log(name, run_id)
            except KeyError:
                text = ""
            out["text"] = text[-20_000:]
        return out

    def image(self, name: str, run_id: str, n: int) -> tuple[bytes, str]:
        self.get(name)
        if not re.match(r"^[0-9]{8}-[0-9]{6}$", run_id):
            raise KeyError("No such run.")
        try:
            data = (self.logs / name / f"{run_id}.images" / str(int(n))).read_bytes()
        except (OSError, ValueError):
            raise KeyError("No such image.") from None
        kind = reports.image_type(data)
        if kind is None:
            raise KeyError("No such image.")
        return data, kind

    def mark_seen(self, name: str, run_id: str | None = None) -> None:
        """You looked at a run in Reports: it and the tool's earlier ones stop counting as new."""
        with self.lock:
            e = self.get(name)
            for r in e.get("runs", []):
                if r.get("seen") is False:
                    r["seen"] = True
                if r["id"] == run_id:
                    break
            self._save()
        self.notify()   # the counts in every open panel

    def code(self, name: str) -> tuple[int, dict[str, tuple[bytes, bool]]]:
        """The approved version's files (its code and manifest; not its data or secrets):
        version, {relative path: (data, executable)}."""
        e = self.get(name)
        vdir = self.version_dir(name, e["version"])
        files = {}
        for p in sorted(vdir.rglob("*")):
            if p.is_file() and not p.is_symlink():
                files[str(p.relative_to(vdir))] = (p.read_bytes(), bool(p.stat().st_mode & 0o100))
        return e["version"], files

    def log(self, name: str, run_id: str) -> str:
        self.get(name)
        if not re.match(r"^[0-9]{8}-[0-9]{6}$", run_id):
            raise KeyError("No such run.")
        try:
            return (self.logs / name / f"{run_id}.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            raise KeyError("That run's log is gone.") from None

    def file(self, name: str, rel: str) -> dict:
        e = self.get(name)
        vdir = self.version_dir(name, e["version"]).resolve()
        path = (vdir / rel).resolve()
        if vdir not in path.parents or not path.is_file():
            raise KeyError("No such file in this tool.")
        data = path.read_bytes()
        return {"path": rel, "size": len(data), "binary": not _is_text(data),
                "text": data[:MAX_SHOWN].decode("utf-8", "replace") if _is_text(data) else ""}

    def _schedule_loop(self, every: float = 20) -> None:
        while True:
            time.sleep(every)
            self.run_due()

    def run_due(self, now: float | None = None) -> list[str]:
        now = now or time.time()
        started = []
        for name, e in list(self.registry.items()):
            if not e.get("enabled") or name in self.running:
                continue
            due = next_run(e["manifest"]["schedule"], e.get("last_start"), now)
            if due is not None and due <= now:
                try:
                    self.run(name, "schedule")
                    started.append(name)
                except ToolError as err:
                    e["enabled"] = False   # e.g. a setting went missing: stop trying
                    e.setdefault("runs", []).append({"id": time.strftime("%Y%m%d-%H%M%S"), "started": now,
                                                     "ended": now, "why": "schedule", "ok": False,
                                                     "note": f"Not run, and switched off: {err}"})
                    self._save()
                    self.notify()
        return started

    # --- views -----------------------------------------------------------------------------

    def view(self, name: str | None = None) -> list[dict] | dict:
        out = []
        now = time.time()
        for n, e in sorted(self.registry.items()):
            if name and n != name:
                continue
            m = e["manifest"]
            secrets = self._secrets(n)
            settings = []
            for st in m["settings"]:
                item = dict(st)
                if st["type"] == "secret":
                    item["set"] = bool(secrets.get(st["name"]))
                else:
                    item["value"] = e["values"].get(st["name"], st["default"])
                settings.append(item)
            runs = e.get("runs", [])
            out.append({"name": n, "title": m["title"], "description": m["description"], "setup": m["setup"],
                        "version": e["version"], "enabled": e["enabled"], "command": m["command"],
                        "schedule": m["schedule"], "schedule_text": schedule_text(m["schedule"]),
                        "timeout_seconds": m["timeout_seconds"], "sites": m["sites"], "settings": settings,
                        "missing": self.missing(n), "running": n in self.running,
                        "running_since": self.running[n]["started"] if n in self.running else None,
                        "last_run": runs[-1] if runs else None, "runs": runs[::-1] if name else None,
                        "next_run": next_run(m["schedule"], e.get("last_start"), now) if e["enabled"] else None,
                        "updated": e.get("updated"), "notify": e.get("notify", "asks"),
                        "unseen": sum(1 for r in runs if r.get("seen") is False),   # new in Reports
                        "files": sorted(str(p.relative_to(self.version_dir(n, e["version"])))
                                        for p in self.version_dir(n, e["version"]).rglob("*") if p.is_file())
                        if name else None})
        if name:
            if not out:
                raise KeyError(f"There's no tool called {name}.")
            return out[0]
        return out

    def summary_for_agent(self) -> str:
        if not self.registry:
            return ""
        parts = []
        for n, e in sorted(self.registry.items()):
            m = e["manifest"]
            parts.append(f"{n} (version {e['version']}, {'on' if e['enabled'] else 'off'}, {schedule_text(m['schedule'])})")
        return ", ".join(parts)


GET_CODE_SPEC = {
    "name": "get_tool_code",
    "description": (
        "Copy the code of a tool the user approved (its files and uvmc-tool.json, not its data or the "
        "secrets in its settings) into your work folder, to read it or change it. The installed tool "
        "stays as it is: change your copy, test it, and propose it again with propose_tool, which shows "
        "the user the changes. Returns the folder it was copied to."),
    "input_schema": {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "The tool's name, e.g. bustehrad-weather."}},
        "required": ["name"],
    },
}

PROPOSE_SPEC = {
    "name": "propose_tool",
    "description": (
        "Propose a tool you built for the user to approve: a small program that keeps working after "
        "this conversation (on a schedule, or when the user runs it from the panel's Tools tab), e.g. "
        "a daily report or a watcher. The user sees its description, settings, sites and full code "
        "(or the changes, for a new version) and approves or declines. Once approved it runs as its own "
        "user from a frozen copy, reaching only the sites it declares. Build and test it in your work "
        "folder first. When its job is to tell the user something, give it a report (see below): "
        "the user then sees each run as a card in the conversation, with tables and charts, "
        "instead of reading a log.\n\n" + FORMAT_HELP),
    "input_schema": {
        "type": "object",
        "properties": {
            "folder": {"type": "string", "description": "The tool's folder in your work folder, e.g. tools/inbox-digest."},
            "reason": {"type": "string", "description": "Why the user wants it and how you tested it, for the approval card."},
        },
        "required": ["folder", "reason"],
    },
}
