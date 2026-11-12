"""Where tool commands actually run.

Everything the agent does goes through one Executor. In the VM, each command:
- runs as the agent user (no root),
- inside the "uvmc-agent.slice" resource box (memory, CPU and process limits),
- with the supervisor's proxy as its only way onto the internet,
- in a private view of its own (design.md 7p): a /proc that shows only its user's processes
  and its own /tmp, /var/tmp and /dev/shm, so conversations, jobs and tools can't watch each
  other's commands or read each other's temporary files,
- in its user's own network (design.md 7t, netns.py): only its own loopback, where its own
  servers and its proxy are; no other user's servers, no other program of the VM, no DNS.

If the supervisor ever moves to the host, a MultipassExecutor
(`multipass exec uvmc -- ...`) can replace these without touching tools or the loop.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import browser
from .config import Config

SLICE = "uvmc-agent.slice"

# The private view (design.md 7p), set up as root in a mount namespace of the command's own,
# just before it drops to its user. Mounts made later outside (shared folders) still reach it
# (propagation slave); its own never leave it. /proc is a new instance with hidepid=invisible:
# the user sees only its own processes, while the supervisor and the system keep the normal
# /proc. Each temporary folder becomes the user's own folder in it (mode 700, on the same size-
# limited memory disk), so files there are its alone, whatever path a program writes to.
# Last, a conversation's or job's command enters its user's own network (netns.py): $3 names
# it, "-" keeps the VM's (tools the agent builds).
# If any step fails, the command doesn't run (exit 126): never without its view.
PRIVATE_DIR = ".uvmc-tmp"
VIEW_SCRIPT = r"""
u=$1; g=$2; n=$3; shift 3
fail() { echo "UVMC: this command could not get its private view ($1), so it didn't run." >&2; exit 126; }
mount -t proc -o nosuid,nodev,noexec,hidepid=invisible proc /proc || fail /proc
for d in /tmp /var/tmp /dev/shm; do
  [ -d "$d" ] || continue
  install -d -m 711 -o root -g root "$d/%(dir)s" || fail "$d"
  install -d -m 700 -o "$u" -g "$g" "$d/%(dir)s/$u" || fail "$d"
  mount --bind "$d/%(dir)s/$u" "$d" || fail "$d"
done
if [ "$n" != - ]; then
  mountpoint -q "/run/netns/$n" || fail "its network"
  exec nsenter --net="/run/netns/$n" -- "$@"
fi
exec "$@"
""" % {"dir": PRIVATE_DIR}


def netns_name(user: str) -> str:
    """The name of a user's own network namespace (/run/netns/<name>, netns.py)."""
    return f"uvmc-{user}"


def private_view(user: str, group: str, own_network: bool = True) -> list[str]:
    """The start of a root command that gives what follows (setpriv ...) its private view,
    in its user's own network unless own_network is False (tools the agent builds)."""
    return ["unshare", "--mount", "--propagation", "slave", "--", "sh", "-c", VIEW_SCRIPT, "uvmc-view", user, group,
            netns_name(user) if own_network else "-"]


def no_sudo() -> bool:
    return os.environ.get("UVMC_NO_SUDO") == "1"


def proxy_env(cfg: Config, port: int | None = None) -> list[str]:
    url = f"http://127.0.0.1:{port or cfg.proxy_port}"
    return [f"{k}={url}" for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")] + [
        "NO_PROXY=localhost,127.0.0.1,::1", "no_proxy=localhost,127.0.0.1,::1"]


def root(cmd: list[str], timeout: int = 60, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    """Runs a command as root (the supervisor's user has sudo; the agent does not)."""
    full = cmd if no_sudo() else ["sudo", "-n", *cmd]
    return subprocess.run(full, input=stdin, capture_output=True, timeout=timeout)


class Executor:
    """Runs a bash script as the agent, starting in the working folder."""

    def __init__(self, cfg: Config, slice_name: str = SLICE, user: str | None = None,
                 workdir: str | None = None, proxy_port: int | None = None):
        self.cfg = cfg
        self.slice = slice_name   # a conversation's own box inside the agent's (for Stop)
        self.user = user or cfg.user          # a conversation's own user (agent1, agent2, ...)
        self.workdir = workdir or cfg.workdir  # and its own copy of the work folder
        self.proxy_port = proxy_port           # and its own proxy

    def inner(self, script: str, args: tuple[str, ...], timeout: int) -> list[str]:
        body = f'cd "{self.workdir}" && {script}'
        return ["env", *proxy_env(self.cfg, self.proxy_port),
                *(f"{k}={v}" for k, v in browser.env().items()), "timeout", "-k", "5", str(timeout),
                "bash", "-c", body, "uvmc", *args]

    def command(self, script: str, args: tuple[str, ...], timeout: int) -> list[str]:
        return self.inner(script, args, timeout)

    def run(self, script: str, *args: str, stdin: str | None = None,
            timeout: int | None = None) -> subprocess.CompletedProcess:
        timeout = timeout or self.cfg.command_timeout_s
        return subprocess.run(
            self.command(script, args, timeout),
            input=stdin.encode() if stdin is not None else None,
            capture_output=True, timeout=timeout + 15,
        )


class SystemdExecutor(Executor):
    """Normal mode in the VM: agent user, inside the resource box."""

    def command(self, script, args, timeout):
        user = self.user
        # --description: systemd would otherwise log the whole command line (the agent's script)
        # in the journal when the scope starts.
        return ["sudo", "-n", "systemd-run", "--scope", "--quiet", "--description=uvmc", f"--slice={self.slice}", "--",
                *private_view(user, self.cfg.user),
                "setpriv", f"--reuid={user}", f"--regid={self.cfg.user}", "--init-groups", "--reset-env", "--",
                *self.inner(script, args, timeout)]


class SudoExecutor(Executor):
    """Fallback without systemd: agent user and its private view, no resource box."""

    def command(self, script, args, timeout):
        return ["sudo", "-n", *private_view(self.user, self.cfg.user),
                "setpriv", f"--reuid={self.user}", f"--regid={self.cfg.user}", "--init-groups", "--reset-env", "--",
                "env", f"HOME={os.path.join(self.cfg.homes_root, self.user)}", f"USER={self.user}",
                *self.inner(script, args, timeout)]


class LocalExecutor(Executor):
    """Tests only: runs as the current user."""


class SessionExecutor(Executor):
    """An agent's commands inside a service's account (services.py, install sessions): each runs
    as a transient systemd service with the session's properties (the service's user, sandbox
    and box, its app folder writable, no secrets, its data folder hidden), named
    <unit_prefix>-<n> so Stop can end them. props() is read at every command, so a profile
    change applies at once. Outside the VM (tests) it runs like LocalExecutor."""

    def __init__(self, cfg: Config, slice_name: str, props, unit_prefix: str, **who):
        super().__init__(cfg, slice_name, **who)
        self.props = props
        self.unit_prefix = unit_prefix
        self.n = 0

    def command(self, script, args, timeout):
        if no_sudo():
            return self.inner(script, args, timeout)
        self.n += 1
        flags = [x for p in self.props() for x in ("-p", p)]
        return ["sudo", "-n", "systemd-run", "--quiet", "--wait", "--collect", "--pipe", "--service-type=exec",
                f"--unit={self.unit_prefix}-{os.getpid()}-{self.n}", *flags, "-p", f"RuntimeMaxSec={timeout + 30}",
                "--", *self.inner(script, args, timeout)]


def conversation_slice(conv_id: str) -> str:
    """A conversation's box: a child of the agent's (systemd nests "a-b.slice" in "a.slice"),
    so the agent's limits cover every conversation together, and Stop can end one alone."""
    return f"{SLICE[:-len('.slice')]}-{conv_id}.slice"


def make_executor(cfg: Config, slice_name: str = SLICE, **who) -> Executor:
    if no_sudo():
        return LocalExecutor(cfg, slice_name, **who)
    if Path("/run/systemd/system").exists():
        return SystemdExecutor(cfg, slice_name, **who)
    return SudoExecutor(cfg, slice_name, **who)


def kill_slice(slice_name: str) -> None:
    """Stops everything one conversation runs (its own box), leaving the others alone."""
    if no_sudo() or not Path("/run/systemd/system").exists():
        return
    root(["systemctl", "kill", "--signal=KILL", slice_name], timeout=15)


def slice_of_pid(pid: int | None) -> str | None:
    """The conversation box a process runs in, if any (from /proc/<pid>/cgroup)."""
    if not pid:
        return None
    try:
        text = Path(f"/proc/{int(pid)}/cgroup").read_text()
    except (OSError, ValueError):
        return None
    prefix = SLICE[:-len(".slice")] + "-"
    for part in text.replace("\n", "/").split("/"):
        if part.startswith(prefix) and part.endswith(".slice"):
            return part
    return None


def clean_user(cfg: Config, user: str) -> list[str]:
    """When a conversation or a job ends: stops whatever its user still runs, empties its home
    folder (keeping the ~/shared link setup makes) and removes its files in /tmp, /var/tmp and
    /dev/shm (its private folders there, PRIVATE_DIR/<user>, and anything older it left in the
    shared ones), so the next conversation with this user starts clean. Returns what couldn't be
    removed (empty when all went well)."""
    problems = []
    if not no_sudo():
        root(["pkill", "-KILL", "-u", user], timeout=15)
    home = os.path.join(cfg.homes_root, user)
    if user and "/" not in user and os.path.isdir(home):
        keep = ["-not", "-path", f"{home}/shared"]
        root(["find", home, "-xdev", "-mindepth", "1", *keep, "-delete"], timeout=120)
        if _left(["find", home, "-xdev", "-mindepth", "1", *keep]):
            problems.append(f"{home} could not be emptied")
    tmp = cfg.tmp_dirs()
    if tmp:
        try:
            uid = os.getuid() if no_sudo() else __import__("pwd").getpwnam(user).pw_uid
        except KeyError:
            return problems
        for d in tmp:
            if not os.path.isabs(d) or not os.path.isdir(d):
                continue
            root(["find", d, "-xdev", "-mindepth", "1", "-uid", str(uid), "-delete"], timeout=120)
            if _left(["find", d, "-xdev", "-mindepth", "1", "-uid", str(uid)]):
                problems.append(f"some of {user}'s files are still in {d}")
    return problems


def _left(find: list[str]) -> bool:
    """Whether a find (as root) still lists something."""
    p = root(find + ["-print", "-quit"], timeout=60)
    return bool(p.stdout.strip())


def agent_users(cfg: Config) -> list[str]:
    """The agent user, the conversations' users (agent1, agent2, ...) and the workers' after them."""
    return [cfg.user] + [f"{cfg.conv_user_prefix}{i}" for i in range(1, cfg.conv_max_open + cfg.delegate_slots + 1)]


def kill_all_agent_processes(cfg: Config) -> None:
    """Stops everything the agent is running, in every conversation, including background programs."""
    if no_sudo():
        return
    if Path("/run/systemd/system").exists():
        root(["systemctl", "kill", "--signal=KILL", SLICE], timeout=15)
    for user in agent_users(cfg):
        root(["pkill", "-KILL", "-u", user], timeout=15)


def oom_kills(path: str) -> int | None:
    """How many processes the system has stopped in a box because it was out of memory
    (memory.events, counted for the box and everything in it). path is under uvmc.slice,
    e.g. "uvmc-brain.slice" or "uvmc-agent.slice/uvmc-agent-<id>.slice". None outside the VM."""
    try:
        for line in (Path("/sys/fs/cgroup/uvmc.slice") / path / "memory.events").read_text().splitlines():
            k, v = line.split()
            if k == "oom_kill":
                return int(v)
    except (OSError, ValueError):
        pass
    return None


def slice_usage(name: str = SLICE) -> dict:
    """Current use and limits of a resource box: the agent's by default (empty outside the VM)."""
    base = Path("/sys/fs/cgroup/uvmc.slice") / name

    def read(name):
        try:
            v = (base / name).read_text().split()[0]
            return None if v == "max" else int(v)
        except (OSError, ValueError, IndexError):
            return None

    cpu_usec = None
    try:
        for line in (base / "cpu.stat").read_text().splitlines():
            k, v = line.split()
            if k == "usage_usec":
                cpu_usec = int(v)
    except (OSError, ValueError):
        pass
    return {"memory": read("memory.current"), "memory_max": read("memory.max"),
            "processes": read("pids.current"), "processes_max": read("pids.max"), "cpu_usec": cpu_usec}
