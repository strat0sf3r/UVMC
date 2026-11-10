"""Which parts of UVMC have a newer version out (the panel's Settings > Updates).

Once a day (and on "Check now") the supervisor compares what is installed with the newest
release of each part. It only reads version numbers; nothing is installed. Updating stays a
choice: the pins are raised in the repo and pushed with `update` (docs/guide.md, "Keeping UVMC
up to date"). The checks run as the supervisor's own user, never an agent's, and reach only
pypi.org, registry.npmjs.org, api.github.com and changelogs.ubuntu.com. `[updates] check = false`
in config.toml switches them off.

Multipass is on the PC, so the VM only knows the version `update.cmd` last told it (written to
the state folder by setup.sh). To be replaced by a direct check once UVMC has a PC-side app
(design.md, 7m "Later").
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import urllib.request
from importlib import metadata
from pathlib import Path
from typing import Callable

from . import browser

HERE = Path(__file__).resolve().parents[1]   # /opt/uvmc in the VM, supervisor/ in the repo
REQUIREMENTS = HERE / "requirements.txt"
BROWSER_REQUIREMENTS = HERE / "browser-requirements.txt"
PYPI = "https://pypi.org/pypi/{}/json"
NPM_CLAUDE = "https://registry.npmjs.org/@anthropic-ai/claude-code/latest"
GITHUB_MULTIPASS = "https://api.github.com/repos/canonical/multipass/releases/latest"
UBUNTU_LTS = "https://changelogs.ubuntu.com/meta-release-lts"
UPDATE_HOW = "then run the tests and windows\\update.cmd (unix/update.sh on a Mac or Linux PC)."

Fetch = Callable[[str], str]


def fetch_url(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "uvmc-update-check",
                                               "Accept": "application/json, text/plain"})
    with urllib.request.urlopen(req, timeout=20) as r:   # noqa: S310 (fixed https addresses)
        return r.read(2_000_000).decode("utf-8", "replace")


def parse_version(v: str) -> tuple:
    """"v1.17.0", "1.16.4+win", "2.1.319 (Claude Code)" -> (1, 17, 0) ..."""
    m = re.search(r"\d+(?:\.\d+)*", v or "")
    return tuple(int(x) for x in m.group(0).split(".")) if m else ()


def clean(v: str) -> str:
    m = re.search(r"\d+(?:\.\d+)*", v or "")
    return m.group(0) if m else (v or "")


def is_newer(latest: str, installed: str) -> bool:
    a, b = parse_version(latest), parse_version(installed)
    return bool(a and b) and a > b


def pins(path: Path) -> list[tuple[str, str]]:
    """name==version lines of a requirements file."""
    out = []
    try:
        for line in path.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if "==" in line:
                name, ver = line.split("==", 1)
                out.append((name.strip(), ver.strip()))
    except OSError:
        pass
    return out


def installed_package(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return ""


def newest_lts(text: str) -> tuple[str, str]:
    """(version, name) of the newest supported LTS in Ubuntu's meta-release-lts file."""
    best = ("", "")
    for block in text.split("\n\n"):
        f = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if f.get("Supported") == "1" and "Version" in f:
            v = f["Version"].split()[0]
            if parse_version(v) > parse_version(best[0]):
                best = (v, f.get("Name", ""))
    return best


def os_release(path: str = "/etc/os-release") -> str:
    try:
        for line in Path(path).read_text().splitlines():
            if line.startswith("VERSION_ID="):
                return line.split("=", 1)[1].strip('"')
    except OSError:
        pass
    return ""


def apt_upgradable() -> int | None:
    """How many Ubuntu packages have an update waiting (from apt's own daily list), or None."""
    try:
        p = subprocess.run(["apt", "list", "--upgradable"], capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL, env={"LANG": "C", "PATH": "/usr/bin:/bin"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    return sum(1 for line in p.stdout.splitlines() if "/" in line and "[upgradable" in line)


class Updates:
    """The check, its saved result, and the notice for versions not seen before."""

    def __init__(self, state_dir: Path, *, fetch: Fetch = fetch_url,
                 claude_version: Callable[[], str] = lambda: "",
                 apt: Callable[[], int | None] = apt_upgradable,
                 reboot_file: Path = Path("/var/run/reboot-required"),
                 notify: Callable[[list[dict]], None] = lambda items: None,
                 requirements: Path = REQUIREMENTS, browser_requirements: Path = BROWSER_REQUIREMENTS,
                 browser_ready: Path | None = None, os_version: Callable[[], str] = os_release):
        self.file = Path(state_dir) / "updates.json"
        self.pc_multipass = Path(state_dir) / "pc-multipass-version"
        self.fetch, self.claude_version, self.apt = fetch, claude_version, apt
        self.reboot_file, self.notify = reboot_file, notify
        self.requirements, self.browser_requirements = requirements, browser_requirements
        self.browser_ready, self.os_version = browser_ready, os_version
        self.lock = threading.Lock()
        self.checking = False
        self.result = self._load()

    # --- saved result ---------------------------------------------------------------------

    def _load(self) -> dict:
        try:
            d = json.loads(self.file.read_text())
            if isinstance(d, dict):
                return d
        except (OSError, ValueError):
            pass
        return {"checked": 0, "items": [], "seen": []}

    def _save(self) -> None:
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.result, indent=1))
            tmp.replace(self.file)
        except OSError:
            pass

    def snapshot(self) -> dict:
        with self.lock:
            return self.snapshot_unlocked()

    # --- the parts --------------------------------------------------------------------------

    def _latest(self, url: str, pick: Callable[[str], str]) -> tuple[str, str]:
        """(version, error)."""
        try:
            return pick(self.fetch(url)), ""
        except Exception as e:  # offline, a site down, an answer we don't understand
            return "", f"Couldn't check: {str(e)[:120] or type(e).__name__}"

    def _item(self, id_, name, installed, latest, error, how, notify=True) -> dict:
        return {"id": id_, "name": name, "installed": installed, "latest": latest,
                "behind": bool(latest and installed and is_newer(latest, installed)),
                "error": error, "how": how, "notify": notify}

    def _python(self) -> list[dict]:
        out = []
        for name, pinned in pins(self.requirements):
            latest, err = self._latest(PYPI.format(name), lambda t: json.loads(t)["info"]["version"])
            have = installed_package(name) or pinned
            out.append(self._item(f"py:{name}", f"{name} (Python, the supervisor)", have, latest, err,
                                  f"Change {name}=={pinned} to {name}=={latest or '<newest>'} in "
                                  f"supervisor/requirements.txt, {UPDATE_HOW}"))
        return out

    def _browser(self) -> list[dict]:
        p = pins(self.browser_requirements)
        if not p:
            return []
        name, pinned = p[0]
        latest, err = self._latest(PYPI.format(name), lambda t: json.loads(t)["info"]["version"])
        have = browser.version(self.browser_ready) or pinned
        return [self._item("browser", "Playwright and Chromium (the headless browser)", have, latest, err,
                           f"Change {name}=={pinned} to {name}=={latest or '<newest>'} in "
                           f"supervisor/browser-requirements.txt, {UPDATE_HOW} The browser that goes "
                           "with it is installed automatically.")]

    def _claude(self) -> list[dict]:
        have = clean(self.claude_version())
        if not have:
            return []
        latest, err = self._latest(NPM_CLAUDE, lambda t: json.loads(t)["version"])
        return [self._item("claude", "Claude Code (for the \"my plan\" models)", have, latest, err,
                           "Settings > Your Claude plan > Update Claude Code, or update-claude on your PC.")]

    def _multipass(self) -> list[dict]:
        try:
            have = clean(self.pc_multipass.read_text().strip())
        except OSError:
            have = ""
        latest, err = self._latest(GITHUB_MULTIPASS, lambda t: json.loads(t)["tag_name"])
        latest = clean(latest)
        how = ("Multipass is on your PC. UVMC is pinned to the version it was tested with "
               "(windows\\multipass.ps1) and moves to a newer one once it's tested. If your PC's is "
               "older than that, run install.cmd: it upgrades Multipass in place and keeps the machine. "
               "The version shown is the one your PC had at the last update.")
        if not have:
            how = "Run windows\\update.cmd once so the VM learns your PC's Multipass version. " + how
        return [self._item("multipass", "Multipass (on your PC)", have, latest, err, how)]

    def _ubuntu(self) -> list[dict]:
        out = []
        count = self.apt()
        restart = self.reboot_file.exists()
        if count is not None or restart:
            text = f"{count} waiting" if count else "none waiting"
            out.append({"id": "apt", "name": "Ubuntu's packages in the VM", "installed": text, "latest": "",
                        "behind": restart, "error": "", "notify": False,
                        "restart": restart,
                        "how": ("Security updates install by themselves every day. "
                                + ("They need a restart of the VM: windows\\stop.cmd, then open the panel "
                                   "again (it starts it). " if restart else "")
                                + "The rest: in a terminal on your PC, multipass exec uvmc -- sudo apt-get upgrade -y.")})
        have = self.os_version()
        if have:
            latest, err = self._latest(UBUNTU_LTS, lambda t: newest_lts(t)[0])
            out.append(self._item("ubuntu", "Ubuntu release in the VM", have, latest, err,
                                  "A new Ubuntu release needs a tested move (design.md, step 7f); "
                                  f"UVMC stays on {have} until then. It is supported for years, with "
                                  "security updates."))
        return out

    # --- the check -------------------------------------------------------------------------

    def check(self) -> dict:
        with self.lock:
            if self.checking:
                return self.snapshot_unlocked()
            self.checking = True
        new: list[dict] = []
        try:
            items = []
            for part in (self._python, self._browser, self._claude, self._multipass, self._ubuntu):
                try:
                    items += part()
                except Exception as e:  # one part failing must not hide the others
                    items.append({"id": part.__name__.strip("_"), "name": part.__name__.strip("_"),
                                  "installed": "", "latest": "", "behind": False,
                                  "error": f"Couldn't check: {e}", "how": "", "notify": False})
            with self.lock:
                seen = set(self.result.get("seen", []))
                new = [i for i in items if i["behind"] and i.get("notify") and f"{i['id']}={i['latest']}" not in seen]
                seen |= {f"{i['id']}={i['latest']}" for i in new}
                self.result = {"checked": time.time(), "items": items, "seen": sorted(seen)}
                self._save()
        finally:
            with self.lock:
                self.checking = False
        if new:
            try:
                self.notify(new)
            except Exception:
                pass
        return self.snapshot()

    def snapshot_unlocked(self) -> dict:
        items = list(self.result.get("items", []))
        return {"checked": self.result.get("checked", 0), "checking": self.checking, "items": items,
                "behind": sum(1 for i in items if i.get("behind"))}

    def loop(self, every_hours: float, first_after: float = 60) -> None:
        """Checks once a day (every_hours), the first time shortly after the supervisor starts,
        or later if the last check is still fresh."""
        last = self.result.get("checked", 0) or 0
        wait = max(first_after, last + every_hours * 3600 - time.time())
        while True:
            time.sleep(wait)
            self.check()
            wait = every_hours * 3600
