"""The supervisor as a background service: one agent session, an HTTP API, and the panel.

The panel (static/index.html) is only a client of this API. Anything that controls
the VM itself (start, stop, snapshot, reset) lives on the host, never here.
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse, Response,
                               RedirectResponse, StreamingResponse)

from . import __version__, config
from .agent import Session
from . import briefing as briefing_mod
from . import mcp_http
from .brain import Bridge
from .audit import ExecFilter, ExecWatcher
from .checkpoints import Checkpoints
from .executor import (agent_users, conversation_slice, kill_all_agent_processes, kill_slice, make_executor,
                       no_sudo, oom_kills, root, slice_of_pid, slice_usage)
from .executor import SLICE as SLICE_AGENT
from .log import ChildLog, RunLog, read_log
from .made_tools import PROPOSE_SPEC, ToolBox, ToolError
from .workcopies import MERGE_DIR, SyncError, WorkCopies
from .transcript import transcript
from .netproxy import Proxy, SiteList
from .providers import Profile, ProviderError, ToolCall, make as make_provider
from .settings import Settings, SettingsError
from .signin import KEY_NAME as PLAN_KEY, SignIn, SignInError
from .shares import SPEC as SHARE_SPEC, ShareError, Shares
from .tools import Tools

STATIC = Path(__file__).parent / "static"
INSTALL_CLAUDE = Path("/usr/local/sbin/uvmc-install-claude")
TOKEN_FILE = Path(os.environ.get("UVMC_PANEL_TOKEN_FILE", "/etc/uvmc/panel_token"))


def _files_set(top: str, paths: list[str]) -> set[str]:
    """Which of these paths exist in a folder (through root if the supervisor can't look)."""
    out = set()
    for p in paths:
        full = os.path.join(top, p)
        try:
            if os.path.isfile(full):
                out.add(p)
                continue
        except OSError:
            pass
        if not no_sudo() and root(["test", "-f", full], timeout=15).returncode == 0:
            out.add(p)
    return out



RESOLVE_NOTE = ("You can't put files into the main folder yourself: the user reviews your proposal and brings it in. If they then ask for changes, change your file again and tell them to review your proposal; don't say you couldn't push or bring it in.")

class Busy(Exception):
    pass


class Unsaved(Exception):
    pass


class Conversation:
    """One conversation in the panel: its own model, timeline, approvals, Stop and box, its own
    Linux user (agent1, agent2, ...), its own copy of the work folder and its own proxy.

    Several can be open and work at the same time. What they share: the machine's limits, the
    allowed sites, and the main work folder, which changes only when you bring a
    conversation's changes in (workcopies.py)."""

    def __init__(self, cfg: config.Config, model: str, origin: str = "panel", slot: int = 1,
                 copy_dir: str = "", cid: str | None = None):
        self.id = cid or uuid.uuid4().hex[:8]
        self.slot = slot                        # its user and proxy port
        self.user = f"{cfg.conv_user_prefix}{slot}" if not no_sudo() else cfg.user
        self.copy_dir = copy_dir
        self.port = cfg.conv_proxy_port + slot if not no_sudo() else 0
        self.proxy: Proxy | None = None
        self.task_sites: dict[str, bool] = {}   # "allow for this task"
        self.key = secrets.token_hex(16)        # Claude Code's tool calls name their conversation with this
        self.origin = origin                    # panel | api | claude-app
        self.model = model
        self.worker_model: str | None = None    # set: the model coordinates and this profile works
        self.worker_session: Session | None = None
        self.cancel = threading.Event()
        self.state = "idle"                     # idle | running | waiting | stopping
        self.thread: threading.Thread | None = None
        self.no_ask = False                     # a script asked that approvals be refused, not asked
        self.title = ""
        self.created = self.last_active = time.time()
        self.slice = conversation_slice(self.id)
        self.bridge_lock = threading.Lock()     # Claude Code's tool calls, one at a time (per conversation)
        self.worker_key = secrets.token_hex(16)  # the same, for a Claude Code worker
        self.worker_bridge_lock = threading.Lock()  # its own: the coordinator's delegate call holds the other
        self.tools = Tools(cfg, make_executor(cfg, self.slice, user=self.user, workdir=copy_dir))
        self.log: RunLog
        self.session: Session

    @property
    def busy(self) -> bool:
        return self.state != "idle"


class Controller:
    """Owns the conversations, the approvals, the network, the activity and the undo points."""

    def __init__(self, cfg: config.Config, provider_factory: Callable[[Profile], object]):
        self.cfg = cfg
        self.settings = Settings(cfg)             # panel settings on top of config.toml
        # Signing in with your Claude plan from the panel (Claude Code's own setup-token)
        self.signin = SignIn(cfg.claude_bin, str(cfg.state_dir / "signin"),
                             save=lambda token: self.settings.set_key(PLAN_KEY, token))
        self.provider_factory = provider_factory  # called per task, so a new key or model is picked up
        self.tools = Tools(cfg)                   # for the panel's own looks (files): no conversation
        self.lock = threading.Lock()
        self.subscribers: set[queue.Queue] = set()
        self.pending: dict[str, dict] = {}        # approvals waiting, from every conversation
        # network
        self.sites = SiteList(cfg.net_allow, cfg.state_dir / "allowed_sites.txt")
        self.site_lock = threading.Lock()
        self.site_waits: dict[str, dict] = {}
        self.net_recent: deque = deque(maxlen=500)
        # activity
        self.exec_filter = ExecFilter(cfg.workdir)
        self.exec_recent: deque = deque(maxlen=1500)
        # undo
        self.checkpoints = Checkpoints(cfg)
        self.checkpoint_lock = threading.Lock()
        # folders from your computer
        self.shares = Shares(agent_home=os.path.dirname(cfg.workdir.rstrip("/")) or "/home/agent")
        self.proxy: Proxy | None = None
        self.brain_proxy: Proxy | None = None     # Claude Code's own way online: Anthropic only
        self.bridge: Bridge | None = None         # where Claude Code's tool calls come in
        self.claude_updating = False
        self.mcp_seen: dict = self._load_json("mcp_seen.json") or {}   # Claude apps on your PC that connected: name -> info
        self.mcp_conv: str | None = None          # the conversation the Claude apps used last
        self.plan_usage = self._load_plan_usage()   # the latest from Claude Code, for the header
        self.watcher: ExecWatcher | None = None
        self.made = ToolBox(cfg, notify=lambda: self._broadcast({"kind": "made_tools"}))   # tools the agent built
        self.copies = WorkCopies(cfg)             # each conversation's own copy of the work folder
        self.copy_lock = threading.Lock()
        self.pool_missing = self._pool_missing()  # setup makes the conversations' users
        self.oom_seen: dict[str, int] = {}        # memory boxes: processes stopped for lack of memory, so far
        self.net_on = False
        self.convs: dict[str, Conversation] = {}  # open conversations, oldest first
        self.current: str = ""                    # the default for calls that name none
        self._recover()
        if not self.convs:
            self.new_conversation()

    # --- the conversation a call is about ---------------------------------------------------
    # Without a conversation id, calls go to the current one (the last one started or picked).
    # These keep the one-conversation API (and the tests) working.

    def conv(self, cid: str | None = None) -> Conversation:
        c = self.convs.get(cid or self.current)
        if c is None:
            raise KeyError("That conversation is closed.")
        return c

    @property
    def session(self) -> Session:
        return self.conv().session

    @property
    def log(self) -> RunLog:
        return self.conv().log

    @property
    def state(self) -> str:
        return self.conv().state

    @state.setter
    def state(self, value: str) -> None:
        self.conv().state = value

    @property
    def model(self) -> str:
        return self.conv().model

    @model.setter
    def model(self, value: str) -> None:
        self.conv().model = value

    @property
    def worker_model(self) -> str | None:
        return self.conv().worker_model

    @worker_model.setter
    def worker_model(self, value: str | None) -> None:
        self.conv().worker_model = value

    @property
    def cancel(self) -> threading.Event:
        return self.conv().cancel

    @property
    def no_ask(self) -> bool:
        return self.conv().no_ask

    @no_ask.setter
    def no_ask(self, value: bool) -> None:
        self.conv().no_ask = value

    def running(self) -> list[Conversation]:
        return [c for c in self.convs.values() if c.busy]

    def anything_running(self) -> bool:
        return any(c.busy for c in self.convs.values())

    # --- background services ---------------------------------------------------------------

    def start_background(self, proxy: bool = True, audit: bool = True, brain: bool = True,
                         made_tools: bool = True) -> None:
        """The proxy (the agent's only way online), the activity watcher, Claude Code's bridge,
        and the tools the agent built (their proxies and schedule)."""
        if made_tools:
            try:
                self.made.start()
            except Exception as e:  # the tools must never keep the supervisor from starting
                print(f"Warning: the agent's tools could not start: {e}")
        if proxy:
            # The agent user's proxy: nothing works as "agent" any more but the panel's file views,
            # so it refuses new sites. Each conversation has its own (see _start_proxy).
            self.proxy = Proxy(self.cfg.proxy_port, self.cfg.net_ports, self.sites,
                               lambda host, port: False, lambda info: self.net_recent.append({**info, "t": time.time()})).start()
            self.net_on = True
            for c in list(self.convs.values()):
                self._start_proxy(c)
        if audit:
            self.watcher = ExecWatcher(self._on_exec).start()
        if brain:
            self.start_brain_support()
        if audit:   # outside the VM (tests) there are no boxes to watch
            threading.Thread(target=self._memory_watch, name="uvmc-memory", daemon=True).start()

    def start_brain_support(self) -> None:
        """For Claude Code on your plan. Failures only disable that mode."""
        try:
            sites = SiteList(self.cfg.brain_sites, Path("/nonexistent/uvmc-brain-sites"))
            self.brain_proxy = Proxy(self.cfg.brain_proxy_port, [443], sites, lambda host, port: False,
                                     self._on_brain_net).start()
        except OSError as e:
            print(f"Warning: Claude Code's proxy could not start: {e}")
        try:
            if no_sudo():
                uids = {os.getuid()}
            else:
                import pwd
                uids = {pwd.getpwnam(self.cfg.brain_user).pw_uid}
            self.bridge = Bridge(self.cfg.bridge_socket, self._bridge, uids).start()
        except (OSError, KeyError) as e:
            print(f"Warning: Claude Code's tool bridge could not start: {e}")
        if self.cfg.brain_idle_minutes > 0:
            threading.Thread(target=self._brain_idle_watch, name="uvmc-brain-idle", daemon=True).start()

    def _brain_idle_watch(self, every: float = 30) -> None:
        while True:
            time.sleep(every)
            self.close_idle_brain()

    def close_idle_brain(self) -> bool:
        """Frees Claude Code's memory in conversations that have been unused for a while."""
        limit = self.cfg.brain_idle_minutes * 60
        closed = []
        with self.lock:
            for c in self.convs.values():
                runner = c.session.brain
                if (c.busy or runner is None or runner.idle_since is None
                        or time.time() - runner.idle_since < limit):
                    continue
                c.session.close()
                closed.append(c)
        for c in closed:
            c.log.event("notice", text=f"Claude Code was closed after {self.cfg.brain_idle_minutes} unused minutes "
                                       "to free memory. Your next message starts it again with the conversation so far.")
        return bool(closed)

    # --- memory boxes ----------------------------------------------------------------------

    def _memory_watch(self, every: float = 5) -> None:
        self.check_memory()   # the counts so far: only what happens from now on is news
        while True:
            time.sleep(every)
            try:
                self.check_memory()
            except Exception as e:  # never let the watch die
                print(f"Warning: memory watch: {e}")

    def check_memory(self) -> list[dict]:
        """Tells you in the panel when a memory box was full and the system had to stop a
        process in it (its oom_kill count went up). Returns the alerts it posted."""
        def grew(path: str) -> bool:
            n = oom_kills(path)
            if n is None:
                return False
            before = self.oom_seen.get(path)
            self.oom_seen[path] = n
            return before is not None and n > before

        def cap(name: str) -> str:
            m = slice_usage(name).get("memory_max")
            return f"{m // (1024 * 1024)} MB" if m else "its limit"

        with self.lock:
            convs = list(self.convs.values())
        alerts = []

        def alert(targets, box, text):
            for c in targets or convs[:1]:
                c.log.event("memory_full", box=box, text=text)
                alerts.append({"conv": c.id, "box": box})

        if grew("uvmc-brain.slice"):
            using = [c for c in convs if c.session.brain is not None
                     or (c.worker_session is not None and c.worker_session.brain is not None)]
            alert(using, "claude-code",
                  f"Claude Code ran out of memory: its box ({cap('uvmc-brain.slice')} for all \"my plan\" "
                  "conversations and workers together) was full, so the system stopped one of its processes and "
                  "a reply may have broken off. Fewer \"my plan\" conversations at once helps; or raise "
                  "[subscription] memory in config.toml (or give the VM more memory), then run update.")
        agent_box = cap(SLICE_AGENT)
        hit = []
        for c in convs:
            if grew(f"{SLICE_AGENT}/{c.slice}"):
                hit.append(c)
        whole = grew(SLICE_AGENT)
        if hit or whole:
            alert(hit or [c for c in convs if c.busy], "agent",
                  f"A program the agent ran ran out of memory: the agent's box ({agent_box}, shared by all "
                  "conversations) was full, so the system stopped it. Ask the agent to use less memory or work in "
                  "smaller steps; or raise [limits] memory in config.toml (or give the VM more memory), then run update.")
        if grew("uvmc-tools.slice"):
            alert([], "tools",
                  f"A tool the agent built ran out of memory: the tools' box ({cap('uvmc-tools.slice')}) was full, "
                  "so the system stopped it. Its run log is in the Tools tab.")
        return alerts

    # --- Claude Code itself (for the "my plan" models) --------------------------------

    def claude_version(self) -> str:
        try:
            p = subprocess.run([self.cfg.claude_bin, "--version"], capture_output=True, text=True,
                               timeout=30, stdin=subprocess.DEVNULL)
            out = p.stdout.strip()
            return out.splitlines()[0] if p.returncode == 0 and out else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def update_claude(self) -> dict:
        """Installs the latest Claude Code (uvmc-install-claude) while no conversation works."""
        with self.lock:
            if self.anything_running():
                raise Busy("A conversation is working. Update Claude Code when it's done.")
            if self.claude_updating:
                raise Busy("Claude Code is already being updated.")
            self.claude_updating = True
        try:
            for c in list(self.convs.values()):
                c.session.close()   # the next message starts the new version
            p = root([str(INSTALL_CLAUDE)], timeout=900)
            text = (p.stdout + p.stderr).decode("utf-8", "replace").strip()
            ok = p.returncode == 0
        except Exception as e:  # e.g. the install script is missing: an old setup
            text, ok = f"Updating failed: {e}. Run update on your PC first.", False
        finally:
            self.claude_updating = False
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        summary = lines[-1] if ok and lines else text
        self.log.event("notice", text=f"Claude Code update: {summary}")
        return {"ok": ok, "message": summary if ok else text, "version": self.claude_version()}

    def _bridge(self, req: dict) -> dict:
        """A request from Claude Code's MCP server: list the tools, or run one."""
        given = str(req.get("conv", ""))
        c = next((c for c in self.convs.values() if hmac.compare_digest(c.key, given)), None)
        as_worker = False
        if c is None and given:   # a Claude Code worker's calls
            c = next((c for c in self.convs.values() if hmac.compare_digest(c.worker_key, given)), None)
            as_worker = c is not None
        if c is None and not given:   # an MCP server from before conversations had keys
            with_brain = [c for c in self.convs.values() if c.session.brain is not None]
            c = with_brain[0] if len(with_brain) == 1 else None
        if c is None:
            closed = "This conversation was closed" if given else "No task is running"
            return {"text": f"{closed}, so nothing was done.", "is_error": True}
        s = c.worker_session if as_worker else c.session
        if s is None:
            return {"text": "The worker's job has ended, so nothing was done.", "is_error": True}
        if req.get("op") == "list":
            return {"tools": s.tools.definitions(s.tool_names) + s.extra_tools}
        if req.get("op") != "call":
            return {"text": "Unknown request.", "is_error": True}
        name, args = str(req.get("name", "")), req.get("args")
        args = args if isinstance(args, dict) else {}
        runner = s.brain
        if runner is None or not c.busy:
            return {"text": "No task is running, so nothing was done.", "is_error": True}
        call_id = runner.match(name, args)
        if s.cancel.is_set():
            s.log.event("result", id=call_id, tool=name, is_error=True, text="Stopped by the user.")
            return {"text": "Stopped by the user.", "is_error": True}
        with c.worker_bridge_lock if as_worker else c.bridge_lock:  # one at a time, like the other models' calls
            result = s._handle(ToolCall(call_id, name, args))
        return {"text": result["content"], "is_error": result["is_error"]}

    # --- conversations and events ----------------------------------------------------

    def new_conversation(self, model: str | None = None, origin: str = "panel", focus: bool = True) -> Conversation:
        """Opens a conversation. Over the limit, the oldest idle one with nothing left to bring
        into the main folder (not the current one) closes."""
        if model is not None:
            self.cfg.profile(model)  # KeyError if unknown
        with self.lock:
            if len(self.convs) >= self.cfg.conv_max_open:
                idle = [c for c in self.convs.values() if not c.busy and c.id != self.current
                        and not self.unsaved(c)]
                if not idle:
                    raise Busy(f"{len(self.convs)} conversations are open, and each is working or has changes not "
                               "brought into the main folder yet. Close one first.")
                self._close(min(idle, key=lambda c: c.last_active))
            base = self.convs.get(self.current)
            used = {c.slot for c in self.convs.values()}
            slot = next(i for i in range(1, self.cfg.conv_max_open + 1) if i not in used)
            cid = uuid.uuid4().hex[:8]
            c = Conversation(self.cfg, model or (base.model if base else self.cfg.model), origin, slot,
                             str(self.copies.copy_dir(cid)), cid)
            if base is not None and model is None:
                c.worker_model = base.worker_model
            self._new_log(c)
            self.convs[c.id] = c
            if focus or not self.current:
                self.current = c.id
        if self.net_on:
            self._start_proxy(c)
        self._broadcast({"kind": "conversations", "conv": c.id})
        return c

    def _start_proxy(self, c: Conversation) -> None:
        """A conversation's own proxy: every connection it makes is known to be its own."""
        if c.proxy is not None:
            return
        try:
            c.proxy = Proxy(c.port, self.cfg.net_ports, self.sites,
                            lambda host, port, c=c: self.decide_site(c, host, port),
                            lambda info, c=c: self._on_net(c, info)).start()
            c.tools.executor.proxy_port = c.proxy.port
        except OSError as e:
            c.log.event("notice", text=f"This conversation's proxy could not start ({e}), so it has no internet.")

    def _pool_missing(self) -> str:
        """Setup makes the conversations' users; an older setup doesn't have them."""
        if no_sudo():
            return ""
        import pwd
        for i in range(1, self.cfg.conv_max_open + 1):
            try:
                pwd.getpwnam(f"{self.cfg.conv_user_prefix}{i}")
            except KeyError:
                return f"{self.cfg.conv_user_prefix}{i}"
        return ""

    def _save_meta(self, c: Conversation) -> None:
        try:
            d = self.copies.state / c.id
            d.mkdir(parents=True, exist_ok=True)
            (d / "meta.json").write_text(json.dumps({"title": c.title, "slot": c.slot, "origin": c.origin,
                                                     "model": c.model, "created": c.created}), encoding="utf-8")
        except OSError:
            pass

    def _recover(self) -> None:
        """Conversations whose copies outlived a restart come back, so their work can still be
        brought in. Their chat history is in Runs; the model starts afresh."""
        try:
            metas = sorted(self.copies.state.glob("*/meta.json"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for meta in metas[: self.cfg.conv_max_open]:
            cid = meta.parent.name
            try:
                m = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not self.copies.exists(cid) or any(c.slot == m.get("slot") for c in self.convs.values()):
                continue
            model = m.get("model") if m.get("model") in self.cfg.models else self.cfg.model
            c = Conversation(self.cfg, model, m.get("origin", "panel"), int(m.get("slot", 1)),
                             str(self.copies.copy_dir(cid)), cid)
            c.title = m.get("title", "")
            c.created = m.get("created", time.time())
            self._new_log(c)
            c.log.event("notice", text="UVMC restarted. This conversation's copy of the work folder is kept, "
                                       "so you can still bring its changes in; the agent starts afresh.")
            self.convs[c.id] = c
            self.current = c.id

    def _new_log(self, c: Conversation) -> None:
        old = getattr(c, "session", None)
        if old is not None:
            old.close()
        c.log = RunLog(self.cfg.log_dir, listener=lambda r, cid=c.id: self._broadcast({**r, "conv": cid}))
        c.session = Session(self.cfg, None, lambda tool, args, reason, call_id="", c=c: self._approve(c, tool, args, reason, call_id),
                            c.log, out=lambda *_: None, cancel=c.cancel, tools=c.tools)
        c.session.bridge_key = c.key
        c.log.event("session", model=c.model, origin=c.origin)

    def new_session(self) -> Conversation:
        """"New conversation": opens a fresh one next to the others (same model and worker as
        the current one) and makes it current."""
        return self.new_conversation()

    def ensure_copy(self, c: Conversation) -> None:
        """A conversation's copy of the main work folder, made at its first task."""
        if not self.copies.exists(c.id):
            with self.copy_lock:
                self.copies.create(c.id, c.user)
            c.log.event("workcopy", path=c.copy_dir, shared=self.cfg.workdir)
        self._save_meta(c)

    def unsaved(self, c: Conversation) -> int:
        """How many of its changes aren't in the main folder yet."""
        try:
            plan = self.copies.plan(c.id)
        except OSError:
            return 0
        return len(plan["mine"]) + len(plan["merged"]) + len(plan["conflicts"]) + len(plan.get("resolving", []))

    def close_conversation(self, cid: str, discard: bool = False) -> None:
        with self.lock:
            c = self.conv(cid)
            if c.busy:
                raise Busy("This conversation is working. Stop it first.")
            n = self.unsaved(c)
            if n and not discard:
                raise Unsaved(f"This conversation has {n} change{'s' if n > 1 else ''} not brought into the shared "
                              "folder. Bring them in first, or close it anyway and drop them.")
            self._close(c)
            empty = not self.convs
        if empty:   # there is always one to type into
            self.new_conversation()
        self._broadcast({"kind": "conversations", "conv": cid, "closed": True})

    def _close(self, c: Conversation) -> None:
        """With the lock held."""
        c.session.close()
        if c.proxy is not None:
            c.proxy.stop()
        try:
            self.copies.remove(c.id)
        except (SyncError, OSError) as e:
            print(f"Warning: could not remove the copy of conversation {c.id}: {e}")
        self.convs.pop(c.id, None)
        if self.mcp_conv == c.id:
            self.mcp_conv = None
        if self.current == c.id:
            rest = sorted(self.convs.values(), key=lambda x: x.last_active)
            self.current = rest[-1].id if rest else ""

    def focus(self, cid: str) -> None:
        self.conv(cid)
        self.current = cid

    def conversations(self) -> list[dict]:
        out = []
        for c in self.convs.values():
            waiting = sum(1 for p in self.pending.values() if c.id in p["convs"])
            out.append({"id": c.id, "title": c.title, "origin": c.origin, "model": c.model,
                        "model_label": self.cfg.models[c.model].label if c.model in self.cfg.models else c.model,
                        "state": c.state, "waiting": waiting, "session": c.log.name, "slot": c.slot,
                        "created": c.created, "last_active": c.last_active, "current": c.id == self.current})
        return out

    def _load_plan_usage(self) -> dict | None:
        try:
            return json.loads((self.cfg.state_dir / "plan_usage.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _load_json(self, name: str):
        try:
            return json.loads((self.cfg.state_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _save_json(self, name: str, data) -> None:
        try:
            self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
            (self.cfg.state_dir / name).write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass

    def _keep_plan_usage(self, record: dict) -> None:
        self.plan_usage = {k: v for k, v in record.items() if k not in ("seq", "kind", "session", "conv")}
        try:
            self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
            (self.cfg.state_dir / "plan_usage.json").write_text(json.dumps(self.plan_usage), encoding="utf-8")
        except OSError:
            pass

    def _broadcast(self, record: dict) -> None:
        if record.get("kind") == "plan_usage":
            self._keep_plan_usage(record)
        for q in list(self.subscribers):
            try:
                q.put_nowait(record)
            except queue.Full:
                self.subscribers.discard(q)  # a stuck client; it will reconnect

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=5000)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        self.subscribers.discard(q)

    def _set_state(self, c: Conversation, state: str) -> None:
        c.state = state
        c.log.event("status", state=state)

    def _refresh_state(self, c: Conversation) -> None:
        if c.state in ("running", "waiting"):
            want = "waiting" if any(c.id in p["convs"] for p in self.pending.values()) else "running"
            if want != c.state:
                self._set_state(c, want)

    def models(self) -> list[dict]:
        return [{"name": p.name, "label": p.label, "provider": p.provider, "model": p.model,
                 "has_key": config.has_key(p), "key": p.key, "plan": p.provider == "claude-code",
                 "on_pc": "{pc}" in p.base_url}
                for p in self.cfg.models.values()]

    def local_model(self) -> dict:
        """The model running on your PC (windows\\local-model.cmd): found, running, which one."""
        from . import pc
        p = next((p for p in self.cfg.models.values() if pc.PLACEHOLDER in p.base_url), None)
        out = {"profile": p.name if p else None, "label": p.label if p else "", "pc": pc.address(),
               "has_key": bool(p and config.has_key(p)), "running": False, "key_ok": None,
               "model": "", "file": "", "context": 0, "build": "", "error": ""}
        if not p or not out["pc"]:
            return out
        import httpx
        root = pc.resolve(p.base_url).rstrip("/").removesuffix("/v1")
        key = config.api_key(p.key)
        try:
            r = httpx.get(root + "/props", headers={"Authorization": f"Bearer {key}"} if key else {}, timeout=3)
        except httpx.HTTPError as e:
            out["error"] = f"Nothing answers at {root} ({type(e).__name__})."
            return out
        out["running"] = True
        if r.status_code == 401:
            out["key_ok"] = False
            return out
        try:
            d = r.json()
        except ValueError:
            out["error"] = f"Unexpected answer from {root}."
            return out
        out.update(key_ok=True, model=str(d.get("model_alias") or ""), build=str(d.get("build_info") or ""),
                   file=str(d.get("model_path") or "").replace("\\", "/").rsplit("/", 1)[-1],
                   context=int((d.get("default_generation_settings") or {}).get("n_ctx") or 0))
        return out

    def set_model(self, name: str, cid: str | None = None) -> None:
        profile = self.cfg.profile(name)  # KeyError if unknown
        c = self.conv(cid)
        with self.lock:
            if c.busy:
                raise Busy("Change the model when this conversation isn't working.")
            c.model = profile.name
            c.session.close()  # a Claude Code process belongs to the model it was started with
        c.log.event("model", model=profile.name, label=profile.label)

    # --- delegation ------------------------------------------------------------

    def allowed_workers(self) -> list[str]:
        if not self.cfg.delegate_enabled:
            return []
        names = self.cfg.delegate_workers or list(self.cfg.models)
        return [n for n in names if n in self.cfg.models]

    def set_worker(self, name: str | None, cid: str | None = None) -> None:
        name = name or None
        if name is not None:
            self.cfg.profile(name)  # KeyError if unknown
            if name not in self.allowed_workers():
                raise KeyError(f"'{name}' is not allowed as a worker (see [delegation] in config.toml).")
        c = self.conv(cid)
        with self.lock:
            if c.busy:
                raise Busy("Change the worker when this conversation isn't working.")
            c.worker_model = name
        label = self.cfg.profile(name).label if name else None
        c.log.event("worker_mode", worker=name, label=label)

    def _delegate_definition(self, c: Conversation) -> dict:
        workers = self.allowed_workers()
        return {
            "name": "delegate",
            "description": (
                "Hand a piece of work to a worker agent that operates the machine (shell, files, packages). "
                "The worker starts with no memory of this conversation, so the instructions must contain "
                "everything it needs: goal, context, constraints, what done looks like, and where to save "
                "results. It returns the worker's final report."),
            "input_schema": {
                "type": "object",
                "properties": {
                    "instructions": {"type": "string", "description": "Complete, self-contained instructions."},
                    "model": {"type": "string", "enum": workers,
                              "description": f"Which worker to use (default: {c.worker_model})."},
                },
                "required": ["instructions"],
            },
        }

    def _prepare_session(self, c: Conversation | None = None) -> None:
        """Sets the conversation up as a normal agent or as a coordinator with a worker."""
        c = c or self.conv()
        s = c.session
        if s.briefing is None:  # fixed for the whole conversation (Claude Code restarts on any change)
            b = self.settings.briefing()
            s.briefing = (b, self.cfg.house_rules, self._live_briefing(b, c=c) if b["enabled"] else "")
        b, rules, live_text = s.briefing
        if c.worker_model and self.cfg.delegate_enabled:
            s.tool_names = [n for n in ("read_file", "list_dir") if n in self.cfg.tools_enabled]
            own, own_handlers = self._share_tools(c)
            s.extra_tools = [self._delegate_definition(c), PROPOSE_SPEC]
            s.handlers = {"delegate": lambda args, call_id, c=c: self._delegate(c, args, call_id),
                          "propose_tool": own_handlers["propose_tool"]}
            s.system = briefing_mod.compose(self.cfg.orchestrator_rules or rules, b, live_text)
        else:
            s.tool_names = None
            s.system = briefing_mod.compose(rules, b, live_text)
            s.extra_tools, s.handlers = self._share_tools(c)

    def _live_briefing(self, b: dict, worker: str | None = None, c: Conversation | None = None) -> str:
        c = c or self.conv()

        def work_names():
            return [e["name"] + ("/" if e["dir"] else "") for e in self.list_files(".", c.id)["entries"]]
        view = SimpleNamespace(cfg=self.cfg, model=c.model, worker_model=c.worker_model,
                               shares=self.shares, sites=self.sites, made=self.made,
                               workdir=c.copy_dir or self.cfg.workdir, shared_workdir=self.cfg.workdir,
                               user=c.user)
        return briefing_mod.live(view, b["parts"], work_names, worker=worker)

    def briefing_view(self) -> dict:
        b = self.settings.briefing()
        live_text = self._live_briefing(b) if b["enabled"] else ""
        return {**b, "labels": briefing_mod.PARTS, "notes_max": briefing_mod.NOTES_MAX,
                "house_rules": self.cfg.house_rules, "house_rules_source": self.settings.house_rules_source(),
                "preview": briefing_mod.compose(self.cfg.house_rules, b, live_text),
                "current": self.session.system if self.session.briefing is not None else None}

    def _share_tools(self, c: Conversation) -> tuple[list[dict], dict]:
        """Tools handled by the supervisor itself: propose_tool, and update_shared_file when a
        folder is shared."""
        specs = [PROPOSE_SPEC]
        handlers = {"propose_tool": lambda args, call_id, c=c: self._propose_tool(c, args, call_id)}
        if self.shares.names():
            specs.append(SHARE_SPEC)
            handlers["update_shared_file"] = lambda args, call_id, c=c: self._update_shared(c, args, call_id)
        return specs, handlers

    def _propose_tool(self, c: Conversation, args: dict, call_id: str) -> tuple[str, bool]:
        """A tool the agent built: shown to you in full; installed only if you approve."""
        folder = str(args.get("folder", "")).strip()
        try:
            manifest, files = self.made.read_folder(c.tools._as_agent, folder)
            info = self.made.preview(manifest, files)
        except ToolError as e:
            return str(e), True
        answer = self._ask("made_tool", {**info, "folder": folder, "reason": str(args.get("reason", ""))[:2000],
                                         "call": call_id}, [c])
        if answer == "deny":
            return ("The user declined this tool, so nothing was installed. Ask them what to change "
                    "before proposing it again."), True
        try:
            version = self.made.install(manifest, files)
        except (ToolError, OSError) as e:
            return f"Installing it failed, so it isn't installed: {e}", True
        c.log.event("made_tool", name=manifest["name"], title=manifest["title"], version=version, call=call_id)
        todo = self.made.missing(manifest["name"])
        nxt = (f"The user still has to fill in: {', '.join(todo)}. " if todo else "") + \
              "It starts switched off; the user turns it on in the Tools tab."
        return (f"Approved and installed: {manifest['title']} ({manifest['name']}), version {version}. "
                f"The installed copy is frozen; keep your folder {folder} to make changes and propose "
                f"them again. {nxt}"), False

    def _update_shared(self, c: Conversation, args: dict, call_id: str) -> tuple[str, bool]:
        share, rel = str(args.get("share", "")), str(args.get("path", ""))
        try:
            target = self.shares.target(share, rel)
            src = self.shares.source(c.tools._as_agent, str(args.get("source", "")))
            info = self.shares.preview(src, target)
        except ShareError as e:
            return str(e), True
        answer = self._ask("file", {"share": share, "path": rel, "source": str(src),
                                    "reason": str(args.get("reason", "")), "call": call_id, **info}, [c])
        if answer == "deny":
            return "The user declined this change. Nothing was written.", True
        try:
            upd = self.shares.write(share, rel, src, target)
        except (ShareError, OSError) as e:
            return f"Writing failed, so nothing was changed: {e}", True
        c.log.event("file_update", id=upd.id, share=share, path=upd.path, backup=upd.backup,
                    size=upd.size, call=call_id)
        where = f" The original is backed up at {share}/{upd.backup}." if upd.backup else " It's a new file."
        return f"Done: {share}/{upd.path} written ({upd.size} bytes).{where}", False

    def restore_shared(self, update_id: str) -> str:
        text = self.shares.restore(update_id)
        self.log.event("file_restore", id=update_id)
        self.log.event("notice", text=text)
        return text

    def _delegate(self, c: Conversation, args: dict, call_id: str) -> tuple[str, bool]:
        instructions = str(args.get("instructions", "")).strip()
        if not instructions:
            return "No instructions given.", True
        name = str(args.get("model") or c.worker_model or "")
        if name not in self.allowed_workers():
            return f"'{name}' is not an allowed worker. Choose one of: {', '.join(self.allowed_workers())}.", True
        profile = self.cfg.profile(name)
        try:
            provider = self.provider_factory(profile)
        except ProviderError as e:
            return f"The worker could not start: {e}", True
        if provider is None:
            return f"The worker could not start: no API key for {profile.label}.", True
        worker = Session(self.cfg, provider, lambda tool, a, reason, cid="": self._approve(c, tool, a, reason, cid),
                         ChildLog(c.log, parent=call_id, agent=profile.name),
                         out=lambda *_: None, cancel=c.cancel, tools=c.tools)
        worker.owns_cancel = False
        worker.max_steps = self.cfg.delegate_max_steps
        worker.tool_names = [n for n in self.cfg.tools_enabled if n != "delegate"]
        worker.extra_tools, worker.handlers = self._share_tools(c)
        # The same three parts as the agent you talk to: house rules, a live briefing (as a worker),
        # your notes. The coordinator gets its own role's rules instead of the house rules.
        b, rules, _ = c.session.briefing or (self.settings.briefing(), self.cfg.house_rules, "")
        worker_live = self._live_briefing(b, worker=profile.name, c=c) if b["enabled"] else ""
        worker.system = briefing_mod.compose(rules, b, worker_live)
        # Claude Code on your plan as the worker: its own process, whose tool calls name the worker.
        worker.bridge_key = c.worker_key
        c.worker_session = worker
        try:
            reason = worker.run_task(instructions)
        except ProviderError as e:
            return f"The worker ({profile.label}) failed: {e}", True
        finally:
            c.worker_session = None
            worker.close()  # each job starts fresh, so a Claude Code worker doesn't linger
        report = worker.last_text or "(it gave no final report)"
        return f"The worker ({profile.label}) ended: {reason}.\n\nIts final report:\n{report}", reason != "done"

    def _active_call(self, c: Conversation) -> str | None:
        """The tool call running right now in a conversation, the worker's if one is busy."""
        w = c.worker_session
        if w is not None and w.current_call:
            return w.current_call
        return c.session.current_call

    def snapshot(self, cid: str | None = None) -> dict:
        c = self.conv(cid)
        profile = self.cfg.profile(c.model)
        return {
            "version": __version__,
            "conv": c.id,
            "conversations": self.conversations(),
            "max_running": self.cfg.conv_max_running,
            "model": c.model,
            "model_label": profile.label,
            "models": self.models(),
            "worker": c.worker_model,
            "workers": self.allowed_workers(),
            "shares": self.shares.describe(),
            "user": self.cfg.user,
            "workdir": self.cfg.workdir,
            "state": c.state,
            "session": c.log.name,
            "pending": list(self.pending_public(c.id)),
            "has_key": config.has_key(profile),
            "plan": profile.provider == "claude-code",
            "source": self.cfg.source_url,
            "plan_usage": self.plan_usage,
            "brain_ready": self.bridge is not None,
            "network": self.proxy is not None,
            "activity": bool(self.watcher and self.watcher.available),
        }

    def pending_public(self, cid: str | None = None):
        for pid, p in list(self.pending.items()):
            if cid is None or cid in p["convs"]:
                yield {k: v for k, v in p.items() if k not in ("event", "answer")} | {"id": pid}

    # --- tasks ---------------------------------------------------------------

    def start_task(self, text: str, cid: str | None = None, by: str | None = None) -> int:
        """Starts a task in the background. Returns the log position just before it, so a
        client can tell this task's events from earlier ones."""
        text = text.strip()
        if not text:
            raise ValueError("The task is empty.")
        c = self.conv(cid)
        with self.lock:
            if c.busy:
                raise Busy("This conversation is already working.")
            if self.claude_updating:
                raise Busy("Claude Code is being updated. Try again in a minute.")
            if self.pool_missing:
                raise Busy(f"Run update on your PC first: this version gives each conversation its own user, "
                           f"and {self.pool_missing} doesn't exist yet.")
            working = len(self.running())
            if working >= self.cfg.conv_max_running:
                raise Busy(f"{working} conversations are already working, the most at once "
                           "([conversations] max_running in config.toml). Wait for one, or stop one.")
            c.cancel.clear()
            c.session.task_by = by   # "uvmc": written by UVMC for you, not typed by you
            if not c.title:
                c.title = " ".join(text.split())[:80]
            c.last_active = time.time()
            after = c.log.seq
            self._set_state(c, "running")
            c.thread = threading.Thread(target=self._run, args=(c, text), daemon=True)
            c.thread.start()
        self._broadcast({"kind": "conversations", "conv": c.id})
        return after

    def _run(self, c: Conversation, text: str) -> None:
        try:
            profile = self.cfg.profile(c.model)
            try:
                client = self.provider_factory(profile)
            except ProviderError as e:
                c.log.event("error", text=str(e))
                return
            if client is None:
                c.log.event("error", text=f"No API key for {profile.label}. Add it in the panel: Settings › API keys ({profile.key}).")
                return
            try:
                self.ensure_copy(c)
            except (SyncError, OSError) as e:
                c.log.event("error", text=f"Could not make this conversation's copy of the work folder: {e}")
                return
            try:
                c.log.event("checkpoint", id=self.copies.checkpoints(c.id).create())
            except Exception as e:
                c.log.event("notice", text=f"No undo point for this task: {e}")
            c.session.provider = client
            self._prepare_session(c)
            c.session.run_task(text)
        except Exception as e:  # API errors, network errors, bugs: report, keep the service up
            c.log.event("error", text=f"{type(e).__name__}: {e}")
            c.session.messages.clear()
            c.log.event("notice", text="The conversation was reset after the error.")
        finally:
            c.last_active = time.time()
            c.task_sites.clear()
            try:
                waiting = self.copies.resolving(c.id)
            except OSError:
                waiting = {}
            if waiting:   # an agent's proposed resolution, for you to review
                c.log.event("resolution_ready", paths=sorted(waiting), explanation=c.session.last_text)
            self._set_state(c, "idle")
            self._broadcast({"kind": "conversations", "conv": c.id})

    def run_and_wait(self, text: str, model: str | None = None, new: bool = False,
                     timeout: float = 1800, no_ask: bool = False, cid: str | None = None,
                     origin: str = "api") -> dict:
        """Runs one task and returns when it ends: for scripts (POST /api/run). With new, in a
        conversation of its own, next to the ones in the panel."""
        if new:
            c = self.new_conversation(model, origin=origin)
        else:
            c = self.conv(cid)
            if c.busy:
                raise Busy("This conversation is already working.")
            if model:
                self.set_model(model, c.id)
        start_seq = c.log.seq
        c.no_ask = no_ask
        try:
            self.start_task(text, c.id)
            deadline = time.time() + timeout
            timed_out = False
            while c.busy:
                if time.time() > deadline and not timed_out:
                    timed_out = True
                    self.stop(c.id)
                time.sleep(0.2)
        finally:
            c.no_ask = False
        records = [r for r in c.log.read() if r.get("seq", 0) > start_seq]
        top = [r for r in records if not r.get("parent")]
        ends = [r for r in top if r["kind"] == "end"]
        steps = []
        for r in records:
            if r["kind"] == "result":
                steps.append({"tool": r.get("tool"), "error": bool(r.get("is_error")),
                              "result": str(r.get("text", ""))[:500], "worker": bool(r.get("parent"))})
        approvals = [{"type": r.get("type"), "answer": r.get("answer"), "automatic": bool(r.get("automatic"))}
                     for r in records if r["kind"] == "approval_done"]
        return {
            "reason": "timed out, stopped" if timed_out else (ends[-1]["reason"] if ends else "unknown"),
            "done": bool(ends) and ends[-1]["reason"] == "done" and not timed_out,
            "text": c.session.last_text,
            "model": c.model,
            "conv": c.id,
            "session": c.log.name,
            "steps": steps,
            "approvals": approvals,
            "errors": [r.get("text") for r in top if r["kind"] == "error"],
            "files_changed": [{"share": r.get("share"), "path": r.get("path"), "backup": r.get("backup")}
                              for r in records if r["kind"] == "file_update"],
        }

    def stop(self, cid: str | None = None) -> None:
        """Stops one conversation: its model, its approvals, and the programs it started."""
        c = self.conv(cid)
        if not c.busy:
            return
        c.cancel.set()
        self._set_state(c, "stopping")
        for p in list(self.pending.values()):
            if all(self.convs[x].cancel.is_set() for x in p["convs"] if x in self.convs):
                p["event"].set()
        kill_slice(c.slice)
        if not [x for x in self.running() if x is not c]:
            kill_all_agent_processes(self.cfg)   # nothing else works: also whatever escaped its box

    def undo(self, cid: str, conv: str | None = None) -> str:
        """Puts a conversation's copy back to how it was before one of its tasks. The shared
        folder and the other conversations are untouched."""
        c = self.conv(conv)
        cps = self.copies.checkpoints(c.id)
        with self.lock:
            if c.busy:
                raise Busy("Stop this conversation's task before undoing.")
            safety = cps.create()
            cps.restore(cid)
        c.log.event("undo", checkpoint=cid, safety=safety)
        c.log.event("notice", text="This conversation's copy of the work folder is back to how it was before that "
                                   "task. How it looked just now is kept as another undo point.")
        return safety

    # --- bringing a conversation's work into the main folder -------------------------------

    def changes(self, conv: str | None = None) -> dict:
        c = self.conv(conv)
        plan = self.copies.plan(c.id)
        origins = self._load_json("main_origins.json") or {}
        for x in plan["conflicts"]:
            # The conversation whose version is in the main folder, if it's open: it can resolve it instead.
            o = self.convs.get((origins.get(x["path"]) or {}).get("conv", ""))
            if o is not None and o is not c and self.copies.exists(o.id):
                x["other"] = {"id": o.id, "title": o.title or "untitled",
                              "model_label": self.cfg.models[o.model].label if o.model in self.cfg.models else o.model}
        for x in plan.get("handed", []):
            o = self.convs.get(x["conv"])
            x["title"] = (o.title or "untitled") if o else "another conversation"
        return {**plan, "conv": c.id, "copy": c.copy_dir, "main": self.cfg.workdir}

    def sync(self, conv: str | None, choices: dict | None = None) -> dict:
        """Brings a conversation's work into the main folder and the main folder's into its copy.
        Lines changed on one side only merge by themselves. A real conflict goes to an agent,
        which resolves it with the context (both versions, what each conversation was doing, the
        folders), the way Claude Code resolves a git conflict; you bring its result in like any
        other change. choices: path -> "agent" (this conversation's agent: the default),
        "agent:<id>" (another open conversation's agent, e.g. the one whose version is in the
        main folder), "mine", "main" or "later"."""
        choices = dict(choices or {})
        c = self.conv(conv)
        with self.lock:
            if c.busy:
                raise Busy("Wait until this conversation's task ends.")
            plan = self.copies.plan(c.id)
            conflicts = [x["path"] for x in plan["conflicts"]]
            own = [p for p in conflicts if choices.get(p, "agent") == "agent"]
            others: dict[str, list[str]] = {}
            for p in conflicts:
                ch = str(choices.get(p, "agent"))
                if not ch.startswith("agent:"):
                    continue
                o = self.convs.get(ch[len("agent:"):])
                if o is None or o is c or not self.copies.exists(o.id):
                    raise ValueError(f"The conversation chosen to resolve {p} isn't open any more.")
                if o.busy:
                    raise Busy(f"\"{o.title or o.id}\" is working. Let this conversation resolve {p}, or wait.")
                others.setdefault(o.id, []).append(p)
            fixed = {p: v for p, v in choices.items() if v in ("mine", "main")}
            with self.copy_lock:
                r = self.copies.sync(c.id, c.user, fixed)
                handed = {oid: self.copies.prepare_other_merge(c.id, oid, self.convs[oid].user, paths)
                          for oid, paths in others.items()}
        n_in = len(r["brought_in"]) + len(r["merged"])
        r["agent_merging"] = own
        r["other_merging"] = [{"conv": oid, "title": self.convs[oid].title, "paths": paths}
                              for oid, paths in others.items()]
        # Remember who brought each file in, so an agent resolving it later knows what it was for.
        origins = self._load_json("main_origins.json") or {}
        for path in r["brought_in"] + r["merged"]:
            origins[path] = self._about(c)
        self._save_json("main_origins.json", origins)
        c.log.event("sync", **r)
        self._broadcast({"kind": "shared_folder", "conv": c.id})
        for other in self.convs.values():
            if other is not c and n_in:
                other.log.event("notice", text=f"Another conversation ({c.title or 'untitled'}) brought {n_in} "
                                               f"file{'s' if n_in > 1 else ''} into the main folder.")
        if own:
            try:
                self.merge_with_agent(c.id, own)
            except (Busy, ValueError, SyncError, OSError) as e:
                r["agent_merging"] = []
                c.log.event("notice", text=f"The agent couldn't start resolving: {e}")
        for oid, files in handed.items():
            try:
                self._resolve_for(self.convs[oid], c, files)
            except (Busy, KeyError) as e:
                c.log.event("notice", text=f"The other conversation couldn't start resolving: {e}")
        return r

    def resolution(self, conv: str | None = None) -> dict:
        """An agent's proposed resolution, file by file, for you to accept or replace."""
        c = self.conv(conv)
        files = self.copies.proposal(c.id)
        origins = self._load_json("main_origins.json") or {}
        for f in files:
            o = self.convs.get(f.get("other_conv") or "")
            f["other_title"] = (o.title or "untitled") if o else None
            # Another conversation could resolve it instead: the one whose version is in the main folder.
            src = self.convs.get((origins.get(f["path"]) or {}).get("conv", ""))
            if src is not None and src is not c and not f.get("other_conv") and self.copies.exists(src.id):
                f["reassign"] = {"id": src.id, "title": src.title or "untitled",
                                 "model_label": self.cfg.models[src.model].label if src.model in self.cfg.models else src.model}
        return {"conv": c.id, "files": files, "explanation": c.session.last_text, "state": c.state}

    def resolve(self, conv: str | None, choices: dict) -> dict:
        """Your answer to a proposed resolution: per file "accept", "yours", "main", "other",
        "later", or "agent:<id>" (let that conversation resolve it instead)."""
        c = self.conv(conv)
        with self.lock:
            if c.busy:
                raise Busy("Wait until this conversation's task ends.")
        reassign = {p: v for p, v in choices.items() if str(v).startswith("agent:")}
        plain = {p: ("later" if p in reassign else v) for p, v in choices.items()}
        with self.copy_lock:
            r = self.copies.resolve(c.id, c.user, plain)
        if r.get("brought_in"):
            origins = self._load_json("main_origins.json") or {}
            for path in r["brought_in"]:
                origins[path] = self._about(c)
            self._save_json("main_origins.json", origins)
        c.log.event("resolved", choices=choices, brought_in=r.get("brought_in", []), undo=r.get("undo"),
                    reopened=[p for p in r.get("reopened", []) if p not in reassign])
        self._broadcast({"kind": "shared_folder", "conv": c.id})
        if reassign:   # the reopened conflicts go to the conversation you chose
            open_ = [x["path"] for x in self.copies.plan(c.id)["conflicts"]]
            self.sync(c.id, {p: reassign.get(p, "later") for p in open_})
        return r

    def _about(self, c: Conversation) -> dict:
        """What a conversation was doing, for an agent resolving a conflict with its work."""
        tasks = [x.get("text", "") for x in c.log.read() if x.get("kind") == "task" and not x.get("parent")]
        return {"conv": c.id, "title": c.title, "tasks": [t[:300] for t in tasks[-5:]],
                "last_reply": (c.session.last_text or "")[:600], "log": c.log.name, "at": time.time()}

    def _hand_transcript(self, to: Conversation, about: dict) -> str | None:
        """Writes the other conversation's log, as a readable transcript, into the resolving
        conversation's .uvmc-merge/ (from the log UVMC keeps, so it works after a close too).
        Returns its path relative to the conversation's folder."""
        name = about.get("log")
        if not name:
            return None
        try:
            records = self.history_events(name)
        except KeyError:
            return None
        title = about.get("title") or "untitled"
        slug = "-".join("".join(ch if ch.isalnum() else " " for ch in title.lower()).split())[:40] or name
        rel = f"{MERGE_DIR}/conversation-{slug}.md"
        try:
            self.copies._write(Path(to.copy_dir) / rel, transcript(records, title).encode(), to.user)
        except (SyncError, OSError):
            return None
        return rel

    @staticmethod
    def _describe(about: dict) -> str:
        text = (f"the conversation \"{about.get('title') or 'untitled'}\", whose tasks were: "
                + " / ".join(about.get("tasks") or ["(unknown)"]) + ".")
        if about.get("last_reply"):
            text += f" Its last answer: {about['last_reply']}"
        return text

    def _resolve_for(self, o: Conversation, c: Conversation, files: list[dict]) -> int:
        """Another conversation's agent resolves conflicts with c's work, in its own copy."""
        lines = []
        for f in files:
            line = (f"- {f['path']}: their version is in {f['other']}" if f.get("other")
                    else f"- {f['path']}: they removed it")
            if f.get("before"):
                line += f"; the version you both started from is in {f['before']}"
            lines.append(line + ".")
        log = self._hand_transcript(o, self._about(c))
        task = ("The user asked you to resolve conflicts with another conversation's work, since you know your "
                f"side best. It is {self._describe(self._about(c))} "
                + (f"Its whole conversation (what it was asked, its answers and steps) is in {log}. " if log else "")
                + f"Its folder, which you can read, is "
                f"{c.copy_dir}; the main work folder is {self.cfg.workdir}.\n\nFor each file, work out what both "
                "sides were doing, then make your file contain both sides' work. If a file isn't text, inspect it with "
                "your tools and decide what to keep. When done, delete the .uvmc-merge folder. If two changes really "
                "contradict each other, don't guess: say what each side wanted, suggest the resolution you think is "
                "right, and ask me. End with a short explanation of what you did for each file: the user sees it next to your proposal, then accepts it or picks another version. "
                + RESOLVE_NOTE + "\n\n" + "\n".join(lines))
        return self.start_task(task, o.id, by="uvmc")

    def merge_with_agent(self, conv: str | None, paths: list[str]) -> int:
        """Lets the conversation's agent merge conflicting files: it gets the main folder's version next
        to its own and a task saying so."""
        c = self.conv(conv)
        if c.busy:
            raise Busy("Wait until this conversation's task ends.")
        open_ = {x["path"] for x in self.copies.plan(c.id)["conflicts"]}
        paths = [p for p in paths if p in open_]
        if not paths:
            raise ValueError("None of these files is in conflict any more.")
        with self.copy_lock:
            files = self.copies.prepare_agent_merge(c.id, c.user, paths)
        origins = self._load_json("main_origins.json") or {}
        mine = _files_set(c.copy_dir, [f["path"] for f in files])
        written: dict = {}
        lines = []
        for f in files:
            own = "your version is " + f["path"] if f["path"] in mine else "you removed it"
            theirs = (f"the main folder's version is in {f['main']}" if f.get("main")
                      else "it was removed in the main folder")
            line = f"- {f['path']}: {own}; {theirs}"
            if f.get("before"):
                line += f"; the version both started from is in {f['before']}"
            line += "."
            o = origins.get(f["path"])
            if o:   # what the other side was for
                line += " The main folder's version came from " + self._describe(o)
                if o.get("log") not in written:
                    written[o.get("log")] = self._hand_transcript(c, o)
                if written.get(o.get("log")):
                    line += f" Its whole conversation (what it was asked, its answers and steps) is in {written[o.get('log')]}."
                other = self.convs.get(o.get("conv"))
                if other is not None and other is not c and self.copies.exists(other.id):
                    line += f" That conversation's folder, which you can read, is {other.copy_dir}."
            lines.append(line)
        task = ("These files were changed both here and in the main work folder, by another conversation. "
                "Resolve each one like a merge conflict: first work out what each side was doing (your own tasks "
                "in this conversation, the other conversation's below, and the files around them; you can read the "
                f"main work folder, {self.cfg.workdir}, and the other conversations' folders), then make your file "
                "contain both sides' work. If a file isn't text, inspect it with your tools and decide what to keep. "
                "To end up without the file, delete yours. When done, delete the .uvmc-merge folder. If two changes "
                "really contradict each other, don't guess: say what each side wanted, suggest the resolution you "
                "think is right, and ask me. End with a short explanation of what you did for each file: the user sees it next to your proposal, then accepts it or picks another version. "
                + RESOLVE_NOTE + "\n\n"
                + "\n".join(lines))
        return self.start_task(task, c.id, by="uvmc")

    def undo_bring_in(self, checkpoint: str, conv: str | None = None) -> None:
        with self.lock:
            if self.anything_running():
                raise Busy("Wait until no conversation is working: the main folder is theirs too.")
            self.copies.undo_bring_in(checkpoint)
            c = self.conv(conv) if (conv or self.current) in self.convs else None
            if c is not None and self.copies.exists(c.id):
                # Its changes count as not brought in again (the copy keeps them).
                self.copies._snapshot_base(c.id)
        self._broadcast({"kind": "shared_folder", "conv": conv})
        if c is not None:
            c.log.event("notice", text="The main folder is back to how it was before that bring-in. "
                                       "This conversation's copy keeps its changes; bring them in again if you want.")

    # --- approvals -----------------------------------------------------------

    def _ask(self, kind: str, info: dict, convs: list[Conversation], timeout: float | None = None) -> str:
        """Shows an approval in the conversations it concerns and waits. Returns "allow",
        "always" or "deny"."""
        pid = uuid.uuid4().hex[:12]
        if all(c.no_ask for c in convs):  # started by a script that can't answer: refuse instead of waiting
            for c in convs:
                c.log.event("approval", id=pid, type=kind, **info)
                c.log.event("approval_done", id=pid, type=kind, call=info.get("call"), answer="deny",
                            allow=False, timed_out=False, automatic=True)
            return "deny"
        entry = {"type": kind, **info, "convs": [c.id for c in convs], "event": threading.Event(), "answer": "deny"}
        self.pending[pid] = entry
        for c in convs:
            self._refresh_state(c)
            c.log.event("approval", id=pid, type=kind, shared=len(convs) > 1, **info)
        self._broadcast({"kind": "conversations"})
        deadline = time.time() + timeout if timeout else None
        timed_out = False
        while not entry["event"].wait(0.5):
            if all(c.cancel.is_set() for c in convs):
                break
            if deadline and time.time() > deadline:
                timed_out = True
                break
        self.pending.pop(pid, None)
        stopped = all(c.cancel.is_set() for c in convs)
        answer = entry["answer"] if entry["event"].is_set() and not stopped else "deny"
        for c in convs:
            c.log.event("approval_done", id=pid, type=kind, call=info.get("call"), answer=answer,
                        allow=answer != "deny", timed_out=timed_out)
            self._refresh_state(c)
        self._broadcast({"kind": "conversations"})
        return answer

    def _approve(self, c: Conversation, tool: str, args: dict, reason: str, call_id: str = "") -> bool:
        return self._ask("tool", {"tool": tool, "args": args, "reason": reason, "call": call_id}, [c]) != "deny"

    def decide(self, pid: str, answer: str) -> None:
        entry = self.pending.get(pid)
        if entry is None:
            raise KeyError("That request is no longer waiting.")
        if answer not in ("allow", "always", "deny"):
            raise ValueError("Answer must be allow, always or deny.")
        if answer == "always" and entry["type"] != "site":
            answer = "allow"
        entry["answer"] = answer
        entry["event"].set()

    # --- network -------------------------------------------------------------
    # Each conversation has its own proxy, and the firewall lets its user reach only that one,
    # so every connection and every question about a new site belongs to one conversation.

    def decide_site(self, c: Conversation, host: str, port: int) -> bool:
        """Called by a conversation's proxy for a site that is not on the allowed list."""
        if not c.busy or c.cancel.is_set():
            return False  # nobody is watching a task here, so nothing new goes out
        key = f"{c.id}/{host}"
        with self.site_lock:
            if host in c.task_sites:
                return c.task_sites[host]
            wait = self.site_waits.get(key)
            owner = wait is None
            if owner:
                wait = self.site_waits[key] = {"event": threading.Event(), "result": False}
        if not owner:  # this conversation already asked about this site; share the answer
            wait["event"].wait(self.cfg.net_approval_timeout_s + 5)
            return wait["result"]
        result = False
        try:
            if self.cfg.net_ask_unknown:
                answer = self._ask("site", {"host": host, "port": port, "call": self._active_call(c),
                                            "reason": "This site is not on your allowed list."},
                                   [c], timeout=self.cfg.net_approval_timeout_s)
                if answer == "always":
                    self.sites.add(host)
                result = answer != "deny"
            c.task_sites[host] = result
        finally:
            wait["result"] = result
            wait["event"].set()
            with self.site_lock:
                self.site_waits.pop(key, None)
        return result

    def _on_net(self, c: Conversation, info: dict) -> None:
        info = {**info, "call": self._active_call(c)}
        self.net_recent.append({**info, "conv": c.id, "t": time.time()})
        c.log.event("net", **info)

    def _on_brain_net(self, info: dict) -> None:
        """Claude Code's own connections (to Anthropic): listed under Activity, not on a tool row."""
        self.net_recent.append({**info, "who": "claude-code", "t": time.time()})
        if info.get("verdict") == "deny":
            self.log.event("notice", text=f"Claude Code tried to reach {info.get('host')}, which its proxy "
                                          f"doesn't allow ([subscription] sites in config.toml).")

    def network(self) -> dict:
        return {"sites": self.sites.snapshot(), "recent": list(self.net_recent)[::-1],
                "ask_unknown": self.cfg.net_ask_unknown}

    # --- activity ------------------------------------------------------------

    def _conv_of_process(self, info: dict) -> Conversation | None:
        """Which conversation started a program: from its box, else the only one working."""
        for pid in (info.get("pid"), info.get("ppid")):
            name = slice_of_pid(pid)
            if name:
                return next((c for c in self.convs.values() if c.slice == name), None)
        working = self.running()
        return working[0] if len(working) == 1 else None

    def _on_exec(self, info: dict) -> None:
        if not self.exec_filter.keep(info):
            return
        c = self._conv_of_process(info)
        info = {**info, "call": self._active_call(c) if c else None}
        self.exec_recent.append({**info, "conv": c.id if c else None})
        if c is not None:
            c.log.event("exec", **{k: v for k, v in info.items() if k != "t"}, at=info.get("t"))

    def activity(self) -> list[dict]:
        return list(self.exec_recent)[::-1]

    def processes(self) -> list[dict]:
        try:
            p = subprocess.run(["ps", "-u", ",".join(agent_users(self.cfg)), "-o", "user=,pid=,ppid=,etimes=,pcpu=,rss=,args="],
                               capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return []
        out = []
        for line in p.stdout.decode(errors="replace").splitlines():
            parts = line.split(None, 6)
            if len(parts) < 7:
                continue
            user, pid, ppid, age, cpu, rss, args = parts
            conv = next((c for c in self.convs.values() if c.user == user and user != self.cfg.user), None)
            out.append({"pid": int(pid), "ppid": int(ppid), "seconds": int(age), "cpu": float(cpu),
                        "memory": int(rss) * 1024, "args": args, "user": user,
                        "conv": conv.id if conv else None, "conv_title": conv.title if conv else None})
        return out

    def kill_process(self, pid: int) -> None:
        if pid not in {p["pid"] for p in self.processes()}:
            raise KeyError("That program is not running as the agent.")
        root(["kill", "-TERM", str(pid)], timeout=10)
        self.log.event("notice", text=f"You stopped program {pid}.")

    def kill_all(self) -> None:
        kill_all_agent_processes(self.cfg)
        self.log.event("notice", text="You stopped every program the agent was running.")

    # --- read-only views -----------------------------------------------------

    def history(self) -> list[dict]:
        runs = []
        for path in sorted(self.cfg.log_dir.glob("*.jsonl"), key=_log_order, reverse=True):
            first_task, tasks, ended = None, 0, None
            for e in read_log(path):
                if e.get("kind") == "task":
                    tasks += 1
                    first_task = first_task or e.get("text")
                ended = e.get("t", ended)
            open_conv = next((c for c in self.convs.values() if c.log.name == path.stem), None)
            if tasks == 0 and open_conv is None:
                continue
            runs.append({"name": path.stem, "first_task": first_task, "tasks": tasks, "last": ended,
                         "current": path.stem == self.log.name, "conv": open_conv.id if open_conv else None})
        return runs

    def history_events(self, name: str) -> list[dict]:
        path = (self.cfg.log_dir / f"{name}.jsonl").resolve()
        if path.parent != self.cfg.log_dir.resolve() or not path.exists():
            raise KeyError("No such run.")
        return read_log(path)

    def _files_as(self, conv: str | None, where: str):
        """Who looks: a conversation's user in its copy, or the agent user in the main folder."""
        if where == "shared" or not conv:
            return self.tools
        c = self.conv(conv)
        return c.tools if self.copies.exists(c.id) else self.tools

    def _where(self, conv: str | None, where: str) -> str:
        return "copy" if self._files_as(conv, where) is not self.tools else "shared"

    def list_files(self, path: str, conv: str | None = None, where: str = "copy") -> dict:
        p = self._files_as(conv, where)._as_agent(
            ': uvmc-panel; cd -- "$1" && pwd && find . -mindepth 1 -maxdepth 1 -printf "%y\\t%s\\t%T@\\t%f\\n"',
            path or ".", timeout=15)
        if p.returncode != 0:
            raise FileNotFoundError(p.stderr.decode(errors="replace").strip() or f"Cannot open {path}")
        lines = p.stdout.decode(errors="replace").splitlines()
        entries = []
        for line in lines[1:]:
            kind, size, mtime, name = (line.split("\t", 3) + ["", "", "", ""])[:4]
            entries.append({"name": name, "dir": kind == "d", "size": int(size or 0),
                            "mtime": float(mtime or 0)})
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        return {"path": lines[0] if lines else path, "entries": entries, "where": self._where(conv, where)}

    def read_file(self, path: str, limit: int = 200_000, conv: str | None = None, where: str = "copy") -> dict:
        p = self._files_as(conv, where)._as_agent(': uvmc-panel; wc -c < "$1" && head -c "$2" -- "$1"', path, str(limit), timeout=15)
        if p.returncode != 0:
            raise FileNotFoundError(p.stderr.decode(errors="replace").strip() or f"Cannot read {path}")
        size_line, _, data = p.stdout.partition(b"\n")
        size = int(size_line.strip() or 0)
        if b"\x00" in data[:8000]:
            return {"path": path, "size": size, "binary": True, "text": ""}
        return {"path": path, "size": size, "binary": False, "truncated": size > limit,
                "text": data.decode("utf-8", errors="replace")}

    def config_text(self) -> dict:
        try:
            text = self.cfg.source.read_text(encoding="utf-8")
        except OSError:
            text = ""
        return {"config": text, "house_rules": self.cfg.house_rules}


def _log_order(path: Path):
    try:
        return (path.stat().st_mtime, path.stem)
    except OSError:
        return (0.0, path.stem)


def _disk(path: str) -> dict | None:
    try:
        d = shutil.disk_usage(path)
        return {"total": d.total, "used": d.used}
    except OSError:
        return None


class Machine:
    """Light VM stats for the panel."""

    def __init__(self):
        self._prev = self._cpu_times()

    @staticmethod
    def _cpu_times():
        try:
            with open("/proc/stat") as f:
                nums = [int(x) for x in f.readline().split()[1:]]
            return sum(nums), nums[3] + (nums[4] if len(nums) > 4 else 0)
        except (OSError, ValueError, IndexError):
            return None

    def stats(self, workdir: str = "/home/agent/work") -> dict:
        cur = self._cpu_times()
        cpu = None
        if cur and self._prev and cur[0] > self._prev[0]:
            total, idle = cur[0] - self._prev[0], cur[1] - self._prev[1]
            cpu = round(100 * (1 - idle / total), 1)
        self._prev = cur
        mem = {}
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    k, v = line.split(":", 1)
                    mem[k] = int(v.split()[0]) * 1024
        except OSError:
            pass
        disk = shutil.disk_usage("/")
        try:
            with open("/proc/uptime") as f:
                uptime = float(f.read().split()[0])
        except OSError:
            uptime = None
        return {
            "cpu_percent": cpu,
            "load": os.getloadavg() if hasattr(os, "getloadavg") else None,
            "cpus": os.cpu_count(),
            "mem_total": mem.get("MemTotal"),
            "mem_used": (mem["MemTotal"] - mem["MemAvailable"]) if "MemAvailable" in mem else None,
            "disk_total": disk.total,
            "disk_used": disk.used,
            "uptime_s": uptime,
            "agent_box": slice_usage(),
            "brain_box": slice_usage("uvmc-brain.slice"),
            "agent_home": _disk(os.path.dirname(workdir.rstrip("/")) or "/"),
        }


# --- HTTP ---------------------------------------------------------------------

def create_app(ctl: Controller, token: str | None):
    app = FastAPI(title="UVMC supervisor", version=__version__, docs_url="/docs", redoc_url=None,
                  description="The API behind the UVMC panel. Sign in with the panel token: "
                              "`Authorization: Bearer <token>` (or open the panel first, then /docs).")
    machine = Machine()

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if not token:
            return PlainTextResponse("The panel has no access token yet. Run the update script on your PC.", 503)
        given = request.query_params.get("token")
        if given is not None:
            if not hmac.compare_digest(given, token):
                return PlainTextResponse("Wrong access token.", 401)
            resp = RedirectResponse(request.url.path or "/", 303)
            resp.set_cookie("uvmc_token", token, httponly=True, samesite="strict", max_age=60 * 60 * 24 * 365)
            return resp
        auth_header = request.headers.get("authorization", "")
        bearer = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
        if bearer:  # scripts and other programs
            if not hmac.compare_digest(bearer, token):
                return PlainTextResponse("Wrong access token.", 401)
        elif not hmac.compare_digest(request.cookies.get("uvmc_token", ""), token):
            return PlainTextResponse("Open the panel with the UVMC Panel shortcut on your PC; it signs you in.", 401)
        if request.method == "POST" and "application/json" not in request.headers.get("content-type", ""):
            return PlainTextResponse("Expected JSON.", 415)
        return await call_next(request)

    def fail(status: int, message: str):
        raise HTTPException(status, message)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    def conv_or_404(cid):
        try:
            return ctl.conv(cid or None)
        except KeyError as e:
            fail(404, str(e.args[0]))

    @app.get("/api/state")
    def state(conv: str = ""):
        """The state of one conversation (default: the current one), and the list of all."""
        return ctl.snapshot(conv_or_404(conv).id)

    # --- conversations: several can be open and work at the same time ----------------------
    @app.get("/api/conversations")
    def conversations():
        return ctl.conversations()

    @app.post("/api/conversations")
    async def conversation_new(request: Request):
        """Opens a conversation (body: optional model) and makes it the current one."""
        body = await request.json()
        try:
            c = ctl.new_conversation(body.get("model") or None)
        except Busy as e:
            fail(409, str(e))
        except KeyError as e:
            fail(404, str(e.args[0]))
        return ctl.snapshot(c.id)

    @app.post("/api/conversations/close")
    async def conversation_close(request: Request):
        """Closes a conversation. One with changes not brought into the main folder closes only
        with "discard": true (409 with "unsaved": true otherwise)."""
        body = await request.json()
        try:
            ctl.close_conversation(str(body.get("conv", "")), bool(body.get("discard")))
        except Unsaved as e:
            return JSONResponse({"error": str(e), "unsaved": True}, 409)
        except Busy as e:
            fail(409, str(e))
        except KeyError as e:
            fail(404, str(e.args[0]))
        return {"ok": True, "conversations": ctl.conversations(), "current": ctl.current}

    # --- a conversation's copy and the main work folder ------------------------------------
    @app.get("/api/conversations/{cid}/changes")
    async def conversation_changes(cid: str):
        """What bringing this conversation in would do: its changes ("mine"), the shared
        folder's newer ones ("theirs"), automatic merges, and conflicts."""
        conv_or_404(cid)
        try:
            return await run_in_threadpool(ctl.changes, cid)
        except OSError as e:
            fail(500, f"Could not compare the folders: {e}")

    @app.post("/api/conversations/sync")
    async def conversation_sync(request: Request):
        """Brings a conversation's changes into the main folder and the main folder's into its
        copy. Body: conv; choices for conflicts: {path: "agent" (the default) | "agent:<conv>" |
        "mine" | "main" | "later"}."""
        body = await request.json()
        c = conv_or_404(body.get("conv"))
        choices = body.get("choices") if isinstance(body.get("choices"), dict) else {}
        try:
            return await run_in_threadpool(ctl.sync, c.id, choices)
        except Busy as e:
            fail(409, str(e))
        except (SyncError, OSError) as e:
            fail(500, f"Bringing in failed: {e}")

    @app.get("/api/conversations/{cid}/resolution")
    async def conversation_resolution(cid: str):
        conv_or_404(cid)
        return await run_in_threadpool(ctl.resolution, cid)

    @app.post("/api/conversations/resolve")
    async def conversation_resolve(request: Request):
        """Your answer to an agent's proposed resolution. Body: conv; choices: {path: "accept" |
        "yours" | "main" | "other" | "later" | "agent:<conversation id>"}."""
        body = await request.json()
        c = conv_or_404(body.get("conv"))
        choices = body.get("choices") if isinstance(body.get("choices"), dict) else {}
        try:
            return await run_in_threadpool(ctl.resolve, c.id, choices)
        except Busy as e:
            fail(409, str(e))
        except ValueError as e:
            fail(400, str(e))
        except (SyncError, OSError) as e:
            fail(500, f"That failed: {e}")

    @app.post("/api/conversations/merge")
    async def conversation_merge(request: Request):
        """Lets the conversation's agent merge conflicting files (body: conv, paths)."""
        body = await request.json()
        c = conv_or_404(body.get("conv"))
        paths = [str(p) for p in body.get("paths", []) if isinstance(p, str)]
        try:
            after = await run_in_threadpool(ctl.merge_with_agent, c.id, paths)
        except Busy as e:
            fail(409, str(e))
        except ValueError as e:
            fail(400, str(e))
        except (SyncError, OSError) as e:
            fail(500, str(e))
        return {"ok": True, "after": after}

    @app.post("/api/conversations/undo-bring-in")
    async def conversation_undo_bring_in(request: Request):
        body = await request.json()
        try:
            await run_in_threadpool(ctl.undo_bring_in, str(body.get("checkpoint", "")), body.get("conv") or None)
        except Busy as e:
            fail(409, str(e))
        except KeyError as e:
            fail(404, str(e.args[0]))
        except RuntimeError as e:
            fail(500, f"Undo failed: {e}")
        return {"ok": True}

    @app.post("/api/conversations/focus")
    async def conversation_focus(request: Request):
        """Makes a conversation the default for calls that name none (the panel's pick)."""
        body = await request.json()
        try:
            ctl.focus(str(body.get("conv", "")))
        except KeyError as e:
            fail(404, str(e.args[0]))
        return {"ok": True}

    @app.get("/api/conversations/{cid}/events")
    def conversation_events(cid: str):
        c = conv_or_404(cid)
        return [{**r, "conv": c.id} for r in c.log.read()]

    @app.get("/api/events")
    def events():
        """Live events of every conversation (each names its conv), after the current one's so far."""
        q = ctl.subscribe()
        cur = ctl.conv()
        backlog = [{**r, "conv": cur.id} for r in cur.log.read()]

        def stream():
            try:
                yield f"event: hello\ndata: {json.dumps(ctl.snapshot())}\n\n"
                for record in backlog:
                    yield f"data: {json.dumps(record)}\n\n"
                while True:
                    try:
                        record = q.get(timeout=15)
                        yield f"data: {json.dumps(record)}\n\n"
                    except queue.Empty:
                        yield ": ping\n\n"
            finally:
                ctl.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/task")
    async def task(request: Request):
        body = await request.json()
        c = conv_or_404(body.get("conv"))
        try:
            after = ctl.start_task(str(body.get("text", "")), c.id)
        except Busy as e:
            fail(409, str(e))
        except ValueError as e:
            fail(400, str(e))
        return {"ok": True, "after": after, "session": c.log.name, "conv": c.id}

    @app.post("/api/run")
    async def run(request: Request):
        """Runs one task and answers when it ends (for scripts).

        Body: text; optional model (a profile name), new (in a new conversation of its own,
        next to the ones in the panel), conv (a conversation to continue; default the current
        one), timeout in seconds (default 1800, at most 7200), no_ask (refuse anything that would
        need approval instead of waiting for you in the panel). Returns the final reply, how it
        ended, every tool step, approvals, errors and files changed in shared folders."""
        body = await request.json()
        text = str(body.get("text", "")).strip()
        if not text:
            fail(400, "The task is empty.")
        try:
            timeout = max(10.0, min(float(body.get("timeout", 1800)), 7200.0))
        except (TypeError, ValueError):
            fail(400, "timeout must be a number of seconds.")
        try:
            return await run_in_threadpool(ctl.run_and_wait, text, body.get("model") or None,
                                           bool(body.get("new")), timeout, bool(body.get("no_ask")),
                                           body.get("conv") or None)
        except Busy as e:
            fail(409, str(e))
        except KeyError as e:
            fail(400, str(e.args[0] if e.args else e))

    @app.post("/api/stop")
    async def stop(request: Request):
        body = await request.json()
        ctl.stop(conv_or_404(body.get("conv")).id)
        return {"ok": True}

    @app.post("/api/new")
    def new():
        """Opens a new conversation next to the others and makes it the current one."""
        try:
            c = ctl.new_session()
        except Busy as e:
            fail(409, str(e))
        return {"ok": True, "session": c.log.name, "conv": c.id}

    @app.post("/api/approve")
    async def approve(request: Request):
        body = await request.json()
        answer = body.get("answer") or ("allow" if body.get("allow") else "deny")
        try:
            ctl.decide(str(body.get("id", "")), str(answer))
        except KeyError as e:
            fail(404, str(e.args[0]))
        except ValueError as e:
            fail(400, str(e))
        return {"ok": True}

    @app.post("/api/undo")
    async def undo(request: Request):
        body = await request.json()
        try:
            ctl.undo(str(body.get("checkpoint", "")), body.get("conv") or None)
        except Busy as e:
            fail(409, str(e))
        except KeyError as e:
            fail(404, str(e.args[0]))
        except RuntimeError as e:
            fail(500, f"Undo failed: {e}")
        return {"ok": True}

    @app.get("/api/checkpoints")
    def checkpoints(conv: str = ""):
        return ctl.copies.checkpoints(conv_or_404(conv).id).list()

    @app.get("/api/models")
    def models():
        return ctl.models()

    @app.get("/api/local-model")
    def local_model():
        return ctl.local_model()

    @app.post("/api/model")
    async def model(request: Request):
        body = await request.json()
        c = conv_or_404(body.get("conv"))
        try:
            ctl.set_model(str(body.get("name", "")), c.id)
        except Busy as e:
            fail(409, str(e))
        except KeyError as e:
            fail(404, str(e.args[0]))
        return ctl.snapshot(c.id)

    @app.post("/api/worker")
    async def worker(request: Request):
        body = await request.json()
        c = conv_or_404(body.get("conv"))
        try:
            ctl.set_worker(body.get("name") or None, c.id)
        except Busy as e:
            fail(409, str(e))
        except KeyError as e:
            fail(404, str(e.args[0]))
        return ctl.snapshot(c.id)

    @app.post("/api/shares/restore")
    async def restore_share(request: Request):
        body = await request.json()
        try:
            return {"ok": True, "text": ctl.restore_shared(str(body.get("id", "")))}
        except KeyError as e:
            fail(404, str(e.args[0]))
        except (ShareError, OSError) as e:
            fail(500, f"Restore failed: {e}")

    def settings_call(fn, *args):
        try:
            result = fn(*args)
        except SettingsError as e:
            fail(400, str(e))
        for c in ctl.convs.values():
            if c.model not in ctl.cfg.models:  # the profile in use was removed
                c.model = ctl.cfg.model
            if c.worker_model and c.worker_model not in ctl.cfg.models:
                c.worker_model = None
        ctl.log.event("settings")
        return {"ok": True, "result": result, **ctl.settings.view()}

    @app.get("/api/settings")
    def settings_view():
        return ctl.settings.view()

    @app.get("/api/settings/toml")
    def settings_toml():
        return {"toml": ctl.settings.toml()}

    @app.post("/api/settings/profile")
    async def settings_profile(request: Request):
        return settings_call(ctl.settings.save_profile, await request.json())

    @app.post("/api/settings/profile/remove")
    async def settings_profile_remove(request: Request):
        return settings_call(ctl.settings.remove_profile, str((await request.json()).get("name", "")))

    @app.post("/api/settings/default")
    async def settings_default(request: Request):
        return settings_call(ctl.settings.set_default, str((await request.json()).get("name", "")))

    # --- Claude apps on your PC (MCP), off until switched on in Settings ----------------
    @app.post("/mcp")
    async def mcp(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, 400)
        # Remember which app knocked, so the panel can show that the set-up on your PC worked.
        for m in (body if isinstance(body, list) else [body]):
            if isinstance(m, dict) and m.get("method") == "initialize":
                client = ((m.get("params") or {}).get("clientInfo") or {})
                name = str(client.get("name") or "an MCP app")[:60]
                ctl.mcp_seen[name] = {"name": name, "version": str(client.get("version") or "")[:30],
                                      "at": time.time(), "allowed": ctl.settings.mcp_enabled()}
                ctl._save_json("mcp_seen.json", ctl.mcp_seen)   # kept across restarts and updates
        if not ctl.settings.mcp_enabled():
            mid = body.get("id") if isinstance(body, dict) else None
            return JSONResponse({"jsonrpc": "2.0", "id": mid, "error": {
                "code": -32001, "message": "Claude apps on your PC are not allowed to use UVMC. "
                                           "Switch it on in the UVMC panel: Settings, Claude apps on your PC."}}, 403)
        msgs = body if isinstance(body, list) else [body]
        replies = []
        for m in msgs:
            if isinstance(m, dict):
                r = await run_in_threadpool(mcp_http.handle, ctl, m)
                if r is not None:
                    replies.append(r)
        if not replies:
            return Response(status_code=202)
        return JSONResponse(replies if isinstance(body, list) else replies[0])

    @app.get("/mcp")
    def mcp_get():
        return Response(status_code=405)

    @app.get("/api/settings/mcp")
    def settings_mcp_view():
        return {"enabled": ctl.settings.mcp_enabled(),
                "seen": sorted(ctl.mcp_seen.values(), key=lambda s: -s["at"])}

    @app.post("/api/settings/mcp")
    async def settings_mcp(request: Request):
        body = await request.json()
        ctl.settings.set_mcp_enabled(bool(body.get("enabled")))
        ctl.log.event("notice", text="Claude apps on your PC may now use UVMC." if ctl.settings.mcp_enabled()
                      else "Claude apps on your PC can no longer use UVMC.")
        return {"ok": True, **ctl.settings.view()}

    # --- what the agent is told -----------------------------------------------------------
    @app.get("/api/briefing")
    async def briefing_get():
        return await run_in_threadpool(ctl.briefing_view)

    @app.post("/api/briefing")
    async def briefing_save(request: Request):
        body = await request.json()
        try:
            ctl.settings.save_briefing(body if isinstance(body, dict) else {})
        except SettingsError as e:
            fail(400, str(e))
        return await run_in_threadpool(ctl.briefing_view)

    @app.post("/api/house-rules")
    async def house_rules_save(request: Request):
        body = await request.json()
        try:
            ctl.settings.save_house_rules(str(body.get("text", "")))
        except SettingsError as e:
            fail(400, str(e))
        return await run_in_threadpool(ctl.briefing_view)

    @app.post("/api/house-rules/reset")
    async def house_rules_reset():
        ctl.settings.reset_house_rules()
        return await run_in_threadpool(ctl.briefing_view)

    # --- Claude Code itself -----------------------------------------------------------
    @app.get("/api/claude")
    async def claude_info():
        version = await run_in_threadpool(ctl.claude_version)
        return {"version": version, "installed": bool(version), "updating": ctl.claude_updating}

    @app.post("/api/claude/update")
    async def claude_update():
        try:
            result = await run_in_threadpool(ctl.update_claude)
        except Busy as e:
            fail(409, str(e))
        if not result["ok"]:
            fail(500, result["message"])
        return result

    # --- signing in with your Claude plan --------------------------------------------
    @app.post("/api/signin/start")
    async def signin_start():
        try:
            url = await run_in_threadpool(ctl.signin.start)
        except SignInError as e:
            fail(400, str(e))
        return {"url": url}

    @app.post("/api/signin/code")
    async def signin_code(request: Request):
        body = await request.json()
        try:
            message = await run_in_threadpool(ctl.signin.submit, str(body.get("code", "")))
        except (SignInError, SettingsError) as e:
            fail(400, str(e))
        ctl.log.event("settings")
        return {"ok": True, "message": message, **ctl.settings.view()}

    @app.post("/api/signin/cancel")
    def signin_cancel():
        ctl.signin.cancel()
        return {"ok": True}

    @app.post("/api/settings/key")
    async def settings_key(request: Request):
        body = await request.json()
        return settings_call(ctl.settings.set_key, str(body.get("name", "")), str(body.get("key", "")))

    # --- tools the agent built (Tools tab) -------------------------------------------------
    def made_call(fn, *args):
        try:
            return fn(*args)
        except KeyError as e:
            fail(404, str(e.args[0]))
        except ToolError as e:
            fail(409, str(e))

    @app.get("/api/made-tools")
    def made_tools():
        return ctl.made.view()

    @app.get("/api/made-tools/{name}")
    def made_tool(name: str):
        return made_call(ctl.made.view, name)

    @app.get("/api/made-tools/{name}/runs/{run_id}")
    def made_tool_log(name: str, run_id: str):
        return {"text": made_call(ctl.made.log, name, run_id)}

    @app.get("/api/made-tools/{name}/file")
    def made_tool_file(name: str, path: str):
        return made_call(ctl.made.file, name, path)

    @app.post("/api/made-tools/run")
    async def made_tool_run(request: Request):
        body = await request.json()
        return made_call(ctl.made.run, str(body.get("name", "")), "you")

    @app.post("/api/made-tools/stop")
    async def made_tool_stop(request: Request):
        body = await request.json()
        made_call(ctl.made.stop, str(body.get("name", "")))
        return {"ok": True}

    @app.post("/api/made-tools/enable")
    async def made_tool_enable(request: Request):
        body = await request.json()
        made_call(ctl.made.set_enabled, str(body.get("name", "")), bool(body.get("on")))
        return made_call(ctl.made.view, str(body.get("name", "")))

    @app.post("/api/made-tools/settings")
    async def made_tool_settings(request: Request):
        body = await request.json()
        values = body.get("values") if isinstance(body.get("values"), dict) else {}
        made_call(ctl.made.save_settings, str(body.get("name", "")), values)
        return made_call(ctl.made.view, str(body.get("name", "")))

    @app.post("/api/made-tools/remove")
    async def made_tool_remove(request: Request):
        body = await request.json()
        made_call(ctl.made.remove, str(body.get("name", "")))
        return {"ok": True}

    @app.get("/api/network")
    def network():
        return ctl.network()

    @app.post("/api/sites")
    async def add_site(request: Request):
        body = await request.json()
        try:
            ctl.sites.add(str(body.get("host", "")))
        except ValueError as e:
            fail(400, str(e))
        return ctl.sites.snapshot()

    @app.post("/api/sites/remove")
    async def remove_site(request: Request):
        body = await request.json()
        try:
            ctl.sites.remove(str(body.get("host", "")))
        except ValueError as e:
            fail(400, str(e))
        return ctl.sites.snapshot()

    @app.get("/api/activity")
    def activity():
        return ctl.activity()

    @app.get("/api/processes")
    def processes():
        return ctl.processes()

    @app.post("/api/processes/kill")
    async def kill(request: Request):
        body = await request.json()
        try:
            ctl.kill_process(int(body.get("pid", 0)))
        except (KeyError, ValueError) as e:
            fail(404, str(e.args[0]) if e.args else "Unknown program.")
        return {"ok": True}

    @app.post("/api/processes/kill-all")
    def kill_all():
        ctl.kill_all()
        return {"ok": True}

    @app.get("/api/history")
    def history():
        return ctl.history()

    @app.get("/api/history/{name}")
    def history_run(name: str):
        try:
            return ctl.history_events(name)
        except KeyError as e:
            fail(404, str(e.args[0]))

    @app.get("/api/files")
    def files(path: str = ".", conv: str = "", where: str = "copy"):
        """A folder: in a conversation's copy (default), or where=shared in the main work folder."""
        try:
            return ctl.list_files(path, conv or None, where)
        except FileNotFoundError as e:
            fail(404, str(e))
        except KeyError as e:
            fail(404, str(e.args[0]))

    @app.get("/api/file")
    def file(path: str, conv: str = "", where: str = "copy"):
        try:
            return ctl.read_file(path, conv=conv or None, where=where)
        except FileNotFoundError as e:
            fail(404, str(e))
        except KeyError as e:
            fail(404, str(e.args[0]))

    @app.get("/api/machine")
    def machine_stats():
        return machine.stats(ctl.cfg.workdir)

    @app.get("/api/config")
    def config_view():
        return ctl.config_text()

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app


def load_token() -> str | None:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def build_provider(profile: Profile):
    """The real provider for a profile, with its key read fresh each task."""
    return make_provider(profile, config.api_key(profile.key))


def serve(cfg: config.Config, host: str, port: int) -> None:
    import uvicorn
    ctl = Controller(cfg, build_provider)
    try:
        ctl.start_background()
    except OSError as e:  # e.g. the proxy port is taken: the agent then simply has no internet
        print(f"Warning: could not start the network proxy: {e}")
        ctl.start_background(proxy=False)
    app = create_app(ctl, load_token())
    print(f"UVMC supervisor {__version__} serving the panel on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning", timeout_graceful_shutdown=3)
