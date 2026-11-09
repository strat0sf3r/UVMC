"""Where tool commands actually run.

Everything the agent does goes through one Executor. In the VM, each command:
- runs as the agent user (no root),
- inside the "uvmc-agent.slice" resource box (memory, CPU and process limits),
- with the supervisor's proxy as its only way onto the internet.

If the supervisor ever moves to the host, a MultipassExecutor
(`multipass exec uvmc -- ...`) can replace these without touching tools or the loop.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .config import Config

SLICE = "uvmc-agent.slice"


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
        return ["env", *proxy_env(self.cfg, self.proxy_port), "timeout", "-k", "5", str(timeout),
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
        return ["sudo", "-n", "systemd-run", "--scope", "--quiet", f"--slice={self.slice}", "--",
                "setpriv", f"--reuid={user}", f"--regid={self.cfg.user}", "--init-groups", "--reset-env", "--",
                *self.inner(script, args, timeout)]


class SudoExecutor(Executor):
    """Fallback without systemd: agent user, no resource box."""

    def command(self, script, args, timeout):
        return ["sudo", "-n", "-u", self.user, "-H", "--", *self.inner(script, args, timeout)]


class LocalExecutor(Executor):
    """Tests only: runs as the current user."""


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
