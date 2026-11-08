"""The tool layer: the only way the agent can act.

Every tool runs through an Executor as the workspace user (normally "agent", without
sudo), so the agent can never touch the supervisor, its config or the key.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from .config import Config
from .executor import Executor, make_executor, root

# Tool definitions sent to Claude. Tags: read-only / reversible / irreversible.
SPECS = {
    "run_command": {
        "tag": "reversible",
        "description": (
            "Run a bash command as the agent user and return its exit code and combined output. "
            "Each call starts fresh in the working folder; use 'cd dir && ...' within one command. "
            "Long output is shortened to its start and end."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The bash command to run."},
                "timeout_s": {"type": "integer", "description": "Seconds before it is stopped (default 120)."},
            },
            "required": ["command"],
        },
    },
    "read_file": {
        "tag": "read-only",
        "description": "Read a text file. Relative paths start in the working folder. "
                       "Use offset and limit (in lines) for large files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "description": "First line, counting from 1."},
                "limit": {"type": "integer", "description": "Number of lines."},
            },
            "required": ["path"],
        },
    },
    "write_file": {
        "tag": "reversible",
        "description": "Create or overwrite a text file with the given content. "
                       "Missing folders are created. Relative paths start in the working folder.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    "list_dir": {
        "tag": "read-only",
        "description": "List a folder with sizes and dates. Defaults to the working folder.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
        },
    },
    "install_system_package": {
        "tag": "irreversible",
        "description": (
            "Install Ubuntu packages with apt (you have no root yourself). The user approves each "
            "request, so say why in 'reason'. Prefer user-level installs (pip install --user, "
            "npm in a project folder) when they are enough."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "packages": {"type": "array", "items": {"type": "string"}, "description": "Ubuntu package names."},
                "reason": {"type": "string", "description": "Why you need them, in one sentence."},
            },
            "required": ["packages", "reason"],
        },
    },
}

PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,99}$")


@dataclass
class Result:
    text: str
    is_error: bool = False


class Tools:
    def __init__(self, cfg: Config, executor: Executor | None = None):
        self.cfg = cfg
        self.executor = executor or make_executor(cfg)

    def definitions(self, names: list[str] | None = None) -> list[dict]:
        wanted = self.cfg.tools_enabled if names is None else [n for n in names if n in self.cfg.tools_enabled]
        return [
            {"name": name, "description": SPECS[name]["description"], "input_schema": SPECS[name]["input_schema"]}
            for name in wanted
            if name in SPECS
        ]

    def run(self, name: str, args: dict) -> Result:
        handler = getattr(self, f"_{name}", None)
        if name not in SPECS or handler is None:
            return Result(f"Unknown tool: {name}", True)
        try:
            return handler(**args)
        except TypeError as e:
            return Result(f"Bad arguments for {name}: {e}", True)

    # --- helpers -------------------------------------------------------------

    def _as_agent(self, script: str, *args: str, stdin: str | None = None,
                  timeout: int | None = None) -> subprocess.CompletedProcess:
        return self.executor.run(script, *args, stdin=stdin, timeout=timeout)

    def _shorten(self, text: str) -> str:
        limit = self.cfg.max_output_chars
        if len(text) <= limit:
            return text
        half = limit // 2
        omitted = text[half:-half]
        return (f"{text[:half]}\n\n[... {omitted.count(chr(10)) + 1} lines "
                f"({len(omitted)} characters) omitted ...]\n\n{text[-half:]}")

    @staticmethod
    def _decode(data: bytes) -> str:
        return data.decode("utf-8", errors="replace")

    # --- tools ---------------------------------------------------------------

    def _run_command(self, command: str, timeout_s: int | None = None) -> Result:
        timeout = max(1, min(int(timeout_s or self.cfg.command_timeout_s), self.cfg.max_timeout_s))
        try:
            p = self._as_agent('exec bash -lc "$1" 2>&1', command, timeout=timeout)
        except subprocess.TimeoutExpired:
            return Result(f"Command did not finish within {timeout} s and was stopped.", True)
        out = self._shorten(self._decode(p.stdout) + self._decode(p.stderr))
        if p.returncode == 124:
            return Result(f"Timed out after {timeout} s and was stopped. Output so far:\n{out}", True)
        return Result(f"exit code {p.returncode}\n{out}".rstrip(), p.returncode != 0)

    def _read_file(self, path: str, offset: int | None = None, limit: int | None = None) -> Result:
        p = self._as_agent('cat -- "$1"', path)
        if p.returncode != 0:
            return Result(self._decode(p.stderr).strip() or f"Could not read {path}", True)
        lines = self._decode(p.stdout).splitlines()
        start = max(1, int(offset or 1))
        end = start - 1 + int(limit) if limit else len(lines)
        chunk = lines[start - 1:end]
        header = f"{path}: lines {start}-{start + len(chunk) - 1} of {len(lines)}\n" if (offset or limit) else ""
        return Result(header + self._shorten("\n".join(chunk)))

    def _write_file(self, path: str, content: str) -> Result:
        p = self._as_agent('mkdir -p -- "$(dirname -- "$1")" && cat > "$1"', path, stdin=content)
        if p.returncode != 0:
            return Result(self._decode(p.stderr).strip() or f"Could not write {path}", True)
        return Result(f"Wrote {len(content.encode())} bytes to {path}")

    def _list_dir(self, path: str = ".") -> Result:
        p = self._as_agent('ls -la -- "$1"', path or ".")
        if p.returncode != 0:
            return Result(self._decode(p.stderr).strip() or f"Could not list {path}", True)
        return Result(self._shorten(self._decode(p.stdout)))

    def _install_system_package(self, packages: list, reason: str = "") -> Result:
        names = [str(p).strip() for p in packages if str(p).strip()]
        if not names:
            return Result("No packages given.", True)
        bad = [n for n in names if not PACKAGE_NAME.match(n)]
        if bad:
            return Result(f"Not valid package names: {', '.join(bad)}", True)
        try:
            p = root(["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y",
                      "--no-install-recommends", "--", *names], timeout=900)
        except subprocess.TimeoutExpired:
            return Result("The install did not finish within 15 minutes.", True)
        out = self._shorten(self._decode(p.stdout) + self._decode(p.stderr))
        if p.returncode != 0:
            return Result(f"apt-get failed (exit {p.returncode}):\n{out}", True)
        return Result(f"Installed: {', '.join(names)}\n{out}")
