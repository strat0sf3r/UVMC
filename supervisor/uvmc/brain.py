"""Claude Code on your Claude plan as the brain, with the supervisor as its only hands.

How it fits together:

    panel chat ──> Controller ──> BrainRunner ──stdin (stream-json)──> claude -p   (user uvmc-brain)
                       ^                                                  │
                       │  rules, approvals, logs, runs as "agent"         │ its only tools:
                       └──────────── Bridge (unix socket) <── mcp_server.py ┘

- Claude Code runs as its own user, uvmc-brain, with all its built-in tools switched off
  (--tools ""), so it can't run commands or touch files itself. Its only tools come from
  our MCP server, which forwards each call to the supervisor; the supervisor treats them
  exactly like the API models' tool calls: rules, approval cards, logs, run as "agent".
- The login token is the key "claude-subscription" (from `claude setup-token`), readable
  only by the supervisor. It goes into the brain process's environment, never onto a
  command line or into a file the agent can read. What the agent runs can't read another
  user's process environment.
- The brain's network goes through a second proxy that only allows Anthropic's sites,
  and the firewall gives that user nothing else.
- The conversation shows up in the panel like any other: replies, tool rows, approvals.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import struct
import subprocess
import threading
import time
import uuid
from collections import deque
from typing import Callable

from .config import Config
from .executor import no_sudo

PREFIX = "mcp__uvmc__"
BRAIN_NOTE = """
## How you act here
You are the brain of UVMC, signed in with the user's Claude plan. You have no built-in
tools on purpose: everything you do on the machine goes through the uvmc tools
(run_command, read_file, write_file, ...). The supervisor checks each call, may ask the
user, and runs it as this conversation's own user in its work folder. Don't look for other ways to act.
"""

# --- the conversation with one Claude Code process ------------------------------------------


class BrainRunner:
    """One `claude -p` process in stream-json mode; one conversation, many turns."""

    def __init__(self, cfg: Config, provider, system: str, signature: tuple, conv_key: str = ""):
        self.cfg, self.provider, self.system, self.signature = cfg, provider, system, signature
        self.conv_key = conv_key   # names its conversation in each tool call (several can run)
        self.token = provider.token
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue = queue.Queue()
        self.stderr: deque = deque(maxlen=40)
        self.session_id: str | None = None
        self.pending: list[tuple[str, str, dict]] = []   # tool_use blocks waiting for their MCP call
        self.cond = threading.Condition()
        self.idle_since: float | None = None   # when its last turn ended; None while working

    # --- process ----------------------------------------------------------------

    def command(self) -> list[str]:
        cfg = self.cfg
        mcp = {"mcpServers": {"uvmc": {"type": "stdio", "command": cfg.mcp_python,
                                       "args": ["-I", cfg.mcp_script, cfg.bridge_socket, self.conv_key]}}}
        claude = [
            cfg.claude_bin, "-p",
            "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--model", self.provider.profile.model,
            "--tools", "",                              # none of its own tools
            "--strict-mcp-config", "--mcp-config", json.dumps(mcp),
            "--allowedTools", "mcp__uvmc",              # ours run without its prompts: we ask you ourselves
            "--permission-mode", "dontAsk",             # anything else is refused
            "--setting-sources", "user",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--append-system-prompt", (self.system + "\n" + BRAIN_NOTE).strip(),
        ]
        if no_sudo():
            return claude
        keep = ",".join(self.env())
        user, home = self.cfg.brain_user, self.cfg.brain_home
        if os.path.isdir("/run/systemd/system"):
            # In its own resource box (uvmc-brain.slice: a memory cap), as its own user.
            return (["sudo", "-n", f"--preserve-env={keep}", "systemd-run", "--scope", "--quiet",
                     "--slice=uvmc-brain.slice", "--",
                     "setpriv", f"--reuid={user}", f"--regid={user}", "--init-groups", "--",
                     "env", f"HOME={home}", f"USER={user}", f"LOGNAME={user}"] + claude)
        return ["sudo", "-n", f"--preserve-env={keep}", "-u", user, "-H", "--"] + claude

    def env(self) -> dict:
        proxy = f"http://127.0.0.1:{self.cfg.brain_proxy_port}"
        return {
            "CLAUDE_CODE_OAUTH_TOKEN": self.token,
            "HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "https_proxy": proxy, "http_proxy": proxy,
            "NO_PROXY": "localhost,127.0.0.1,::1", "no_proxy": "localhost,127.0.0.1,::1",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "MCP_TIMEOUT": "30000",
            "MCP_TOOL_TIMEOUT": "7200000",              # a tool call may wait for you, or run long
        }

    def start(self) -> None:
        env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
        if no_sudo():  # tests and development: run directly, as yourself
            env["HOME"] = os.environ.get("HOME", "/tmp")
        env.update(self.env())
        cwd = self.cfg.brain_dir if os.path.isdir(self.cfg.brain_dir) else "/"
        self.proc = subprocess.Popen(self.command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=env, cwd=cwd, bufsize=0)
        threading.Thread(target=self._read_out, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read_out(self) -> None:
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                self.lines.put(json.loads(line))
            except ValueError:
                self.stderr.append(line)
        self.lines.put(None)  # it exited

    def _read_err(self) -> None:
        for raw in self.proc.stderr:
            self.stderr.append(raw.decode("utf-8", "replace").rstrip())

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def send(self, obj: dict) -> None:
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        self.proc.stdin.flush()

    def interrupt(self) -> None:
        try:
            self.send({"type": "control_request", "request_id": f"req_{uuid.uuid4().hex[:10]}",
                       "request": {"subtype": "interrupt"}})
        except OSError:
            pass

    def close(self) -> None:
        if not self.proc:
            return
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.terminate()  # sudo passes it on to Claude Code
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    # --- linking MCP calls to the tool_use blocks the panel shows ------------------------

    def expect(self, call_id: str, name: str, args: dict) -> None:
        with self.cond:
            self.pending.append((call_id, name, args))
            self.cond.notify_all()

    def match(self, name: str, args: dict, wait: float = 3.0) -> str:
        """The tool_use id for an incoming MCP call (they arrive separately, in either order)."""
        deadline = time.time() + wait
        with self.cond:
            while True:
                for i, (cid, n, a) in enumerate(self.pending):
                    if n == name and a == args:
                        del self.pending[i]
                        return cid
                left = deadline - time.time()
                if left <= 0:
                    return f"mcp_{uuid.uuid4().hex[:12]}"
                self.cond.wait(left)

    def leftover(self) -> list[tuple[str, str, dict]]:
        with self.cond:
            out, self.pending = self.pending, []
            return out


# --- your plan's usage ---------------------------------------------------------------------


def plan_usage(info: dict) -> dict:
    """Claude Code's rate_limit_event, tidied for the panel.

    Its shape isn't documented by Anthropic; this follows what Claude Code sends today and
    ignores anything unexpected: status ("allowed", "allowed_warning", "rejected"), and per
    usage window ("five_hour", "seven_day", ...) how much is used (0-1) and when it resets.
    """
    def when(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v / 1000 if v > 1e12 else v      # seen both in seconds and in milliseconds

    def share(v):
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return None

    windows = {}
    unified = info.get("unifiedWindows") or info.get("unified_windows")
    if isinstance(unified, dict):
        for name, w in unified.items():
            if isinstance(w, dict):
                windows[str(name)] = {"used": share(w.get("utilization")),
                                      "resets_at": when(w.get("resetsAt") or w.get("resets_at"))}
    kind = info.get("rateLimitType") or info.get("rate_limit_type")
    if kind and str(kind) not in windows:
        windows[str(kind)] = {"used": share(info.get("utilization")),
                              "resets_at": when(info.get("resetsAt") or info.get("resets_at"))}
    return {"status": str(info.get("status") or ""), "limit": str(kind or ""), "windows": windows,
            "overage": bool(info.get("isUsingOverage") or info.get("is_using_overage"))}


# --- one task ----------------------------------------------------------------------------


def transcript(messages: list[dict], limit: int = 20000) -> str:
    """Earlier messages, for a fresh Claude Code process (e.g. after switching models)."""
    parts = []
    for m in messages:
        if m.get("role") == "user":
            parts.append(f"User: {m.get('text', '')}")
        elif m.get("role") == "assistant" and m.get("text"):
            parts.append(f"Assistant: {m['text']}")
    text = "\n\n".join(parts)
    if not text:
        return ""
    if len(text) > limit:
        text = "...\n" + text[-limit:]
    return f"(Earlier in this conversation:)\n{text}\n\n(Now the user says:)\n"


def run_task(session, task: str) -> str:
    """Session.run_task for a Claude Code brain."""
    s, prov = session, session.provider
    s.messages.append({"role": "user", "text": task})
    s.log.event("task", text=task, model=prov.profile.name, **s._task_by())
    s.last_text = ""
    system = s.system if s.system is not None else s.cfg.house_rules
    tools = s.tools.definitions(s.tool_names) + s.extra_tools
    signature = (prov.profile.name, prov.profile.model, prov.token,
                 json.dumps(tools, sort_keys=True), system)
    runner: BrainRunner | None = s.brain
    message = task
    if runner is None or not runner.alive() or runner.signature != signature:
        if runner is not None:
            runner.close()
        runner = s.brain = BrainRunner(s.cfg, prov, system, signature, getattr(s, "bridge_key", ""))
        try:
            runner.start()
        except OSError as e:
            s.brain = None
            s.log.event("error", text=f"Claude Code could not start: {e}")
            return s._end("Claude Code could not start", 0)
        message = transcript(s.messages[:-1]) + task
    runner.idle_since = None
    try:
        return _turn(s, runner, message)
    finally:
        runner.idle_since = time.time()


def _turn(s, runner: BrainRunner, message: str) -> str:
    try:
        runner.send({"type": "user", "message": {"role": "user", "content": message}})
    except OSError:
        return _died(s, runner, 0)
    used, step, seen, texts = 0, 0, set(), []
    interrupted_at = None
    while True:
        if s.cancel.is_set() and interrupted_at is None:
            interrupted_at = time.time()
            runner.interrupt()
        if interrupted_at and time.time() - interrupted_at > 15:
            runner.close()  # it didn't stop; the next task starts a fresh one
            s.brain = None
            _close_leftovers(s, runner, "Stopped by the user.")
            return s._end("stopped by you", used)
        try:
            ev = runner.lines.get(timeout=0.5)
        except queue.Empty:
            continue
        if ev is None:
            if interrupted_at:
                s.brain = None
                _close_leftovers(s, runner, "Stopped by the user.")
                return s._end("stopped by you", used)
            return _died(s, runner, used)
        kind = ev.get("type")
        if kind == "system":
            sub = ev.get("subtype")
            if sub == "init":
                runner.session_id = ev.get("session_id")
                for srv in ev.get("mcp_servers") or []:
                    if srv.get("name") == "uvmc" and srv.get("status") not in ("connected", "pending"):
                        s.log.event("error", text=f"Claude Code could not load the UVMC tools ({srv.get('status')}).")
            elif sub == "api_retry":
                s.log.event("notice", text=f"Claude Code is retrying ({ev.get('error', 'error')}, "
                                           f"attempt {ev.get('attempt')}).")
        elif kind == "rate_limit_event":
            info = ev.get("rate_limit_info")
            if isinstance(info, dict):
                s.log.event("plan_usage", model=runner.provider.profile.name, **plan_usage(info))
        elif kind == "assistant" and not ev.get("parent_tool_use_id"):
            msg = ev.get("message") or {}
            blocks, calls, text = [], 0, ""
            for b in msg.get("content") or []:
                if b.get("type") == "text" and b.get("text", "").strip():
                    text += b["text"]
                    blocks.append({"type": "text", "text": b["text"]})
                elif b.get("type") == "tool_use":
                    name = b.get("name", "")
                    name = name[len(PREFIX):] if name.startswith(PREFIX) else name
                    args = b.get("input") if isinstance(b.get("input"), dict) else {}
                    runner.expect(b.get("id", ""), name, args)
                    blocks.append({"type": "tool_use", "id": b.get("id", ""), "name": name, "input": args})
                    calls += 1
            if not blocks:
                continue
            usage = {}
            mid = msg.get("id")
            if mid not in seen:  # one model reply can arrive as several messages; count it once
                seen.add(mid)
                u = msg.get("usage") or {}
                usage = {"in": int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
                         + int(u.get("cache_creation_input_tokens") or 0),
                         "out": int(u.get("output_tokens") or 0)}
                used += usage["in"] + usage["out"]
                step += 1
            s.log.event("reply", step=step, stop="tool_use" if calls else "end", model=runner.provider.profile.name,
                        usage=usage or {"in": 0, "out": 0}, content=blocks)
            if text:
                texts.append(text)
                s.last_text = text
        elif kind == "result":
            _close_leftovers(s, runner, "Not run.")
            if texts:
                s.messages.append({"role": "assistant", "text": "\n\n".join(texts)})
            if interrupted_at:
                return s._end("stopped by you", used)
            if ev.get("is_error") or str(ev.get("subtype", "success")) != "success":
                detail = str(ev.get("result") or ev.get("subtype") or "error")
                s.log.event("error", text=f"Claude Code: {detail}")
                return s._end("ended with an error", used)
            return s._end("done", used)


def _close_leftovers(s, runner: BrainRunner, text: str) -> None:
    for cid, name, _ in runner.leftover():
        s.log.event("result", id=cid, tool=name, is_error=True, text=text)


def _died(s, runner: BrainRunner, used: int) -> str:
    s.brain = None
    tail = "\n".join(list(runner.stderr)[-8:]).strip()
    hint = ""
    if "not found" in tail or "No such file" in tail:
        hint = " Is Claude Code installed in the VM? Run update."
    s.log.event("error", text=f"Claude Code stopped unexpectedly.{hint}" + (f"\n{tail}" if tail else ""))
    _close_leftovers(s, runner, "Claude Code stopped.")
    return s._end("Claude Code stopped", used)


# --- the bridge: where the MCP server's calls come in ---------------------------------------


class Bridge:
    """Unix socket server for mcp_server.py. Only the brain user may connect."""

    def __init__(self, path: str, handler: Callable[[dict], dict], allowed_uids: set[int]):
        self.path, self.handler, self.allowed = path, handler, allowed_uids
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        os.chmod(path, 0o666)   # anyone may knock; the peer check below decides
        self.sock.listen(16)

    def start(self) -> "Bridge":
        threading.Thread(target=self._serve, name="uvmc-bridge", daemon=True).start()
        return self

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn: socket.socket) -> None:
        with conn:
            try:
                creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                _, uid, _ = struct.unpack("3i", creds)
                if uid not in self.allowed:
                    conn.sendall(json.dumps({"text": "Not allowed.", "is_error": True}).encode() + b"\n")
                    return
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    data += chunk
                    if len(data) > 5_000_000:
                        return
                answer = self.handler(json.loads(data))
            except (OSError, ValueError) as e:
                answer = {"text": f"Bad request: {e}", "is_error": True}
            try:
                conn.sendall(json.dumps(answer).encode() + b"\n")
            except OSError:
                pass

    def stop(self) -> None:
        self.sock.close()
