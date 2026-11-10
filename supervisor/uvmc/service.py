"""The supervisor as a background service: one agent session, an HTTP API, and the panel.

The panel (static/index.html) is only a client of this API. Anything that controls
the VM itself (start, stop, snapshot, reset) lives on the host, never here.
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import re
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

from . import __version__, config, privacy
from . import private as private_mod
from .privacy import GENERAL, NeedsConfirm, PrivacyError, Projects, Scope, is_share, share_of
from .agent import Session
from . import briefing as briefing_mod
from . import mcp_http
from .brain import Bridge
from .audit import ExecFilter, ExecWatcher, set_quiet as audit_quiet
from .checkpoints import Checkpoints
from .executor import (agent_users, clean_user, conversation_slice, kill_all_agent_processes, kill_slice, make_executor,
                       no_sudo, oom_kills, root, slice_of_pid, slice_usage)
from .executor import SLICE as SLICE_AGENT
from .lock import Lock, LockError
from .log import ChildLog, RunLog, read_log
from .made_tools import GET_CODE_SPEC, PROPOSE_SPEC, ToolBox, ToolError
from .workcopies import CONTEXT_DIR, MERGE_DIR, SyncError, WorkCopies
from .transcript import transcript
from .netproxy import Proxy, SiteList
from .providers import Profile, ProviderError, ToolCall, make as make_provider
from .settings import Settings, SettingsError
from .signin import KEY_NAME as PLAN_KEY, SignIn, SignInError
from .shares import SPEC as SHARE_SPEC, ShareError, Shares
from .tools import Tools
from .updates import Updates

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
    sealed = False   # a private conversation whose files may not reach the main folder: keep or erase them


PRIVATE_DEFAULTS = {"offline": True, "pc_only": True, "sealed": True}


BRING_IN_SPEC = {
    "name": "bring_in_worker",
    "description": (
        "Brings a finished job's changes into your work folder. Files only the worker changed are copied; a "
        "text file also changed in your folder since the job started (another job brought in) is merged line "
        "by line when the changes are in different places. Changed in the same place, it's a conflict: say "
        "which version to keep with choices, or leave it open. When nothing is left open, the job's folder "
        "is removed."),
    "input_schema": {
        "type": "object",
        "properties": {
            "job": {"type": "string", "description": "The job's id, from delegate's result."},
            "choices": {"type": "object", "additionalProperties": {"type": "string", "enum": ["worker", "mine"]},
                        "description": 'For conflicts: path -> "worker" (its version) or "mine" (keep yours).'},
        },
        "required": ["job"],
    },
}

DISCARD_SPEC = {
    "name": "discard_worker",
    "description": "Drops a finished job: none of its changes reach your folder, and its folder is removed.",
    "input_schema": {"type": "object", "properties": {"job": {"type": "string"}}, "required": ["job"]},
}

JOB_ID = re.compile(r"^[0-9a-f]{8}w\d+$")   # a job's folder: its conversation's id, w, a number


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
        self.private = False                    # erased when closed (private.py)
        self.opts = {k: False for k in PRIVATE_DEFAULTS}   # a private one's restrictions (private.py)
        self.keep_files = False                 # you chose to keep a sealed private conversation's files
        self.projects: list[str] = []           # the projects it opened (privacy.py; "" = General)
        self.level = "open"                     # what it may see: its model's level, or lower
        self.seen = "open"                      # the most private file it has had in its copy
        self.chosen = False                     # projects or level chosen by you (else they follow the model)
        self.given: set[str] = set()            # files you gave it one by one, above its level
        self.context_from: dict | None = None   # another conversation whose transcript it starts with
        self.context_file: str | None = None    # where that transcript is in its copy
        self.worker_model: str | None = None    # set: the model coordinates and this profile works
        self.jobs: dict[str, Worker] = {}       # its workers' jobs: running, or done and not brought in yet
        self.job_seq = 0
        self.job_room = threading.Semaphore(cfg.delegate_parallel)   # jobs at once
        self.cancel = threading.Event()
        self.state = "idle"                     # idle | running | waiting | stopping
        self.thread: threading.Thread | None = None
        self.no_ask = False                     # a script asked that approvals be refused, not asked
        self.title = ""
        self.created = self.last_active = time.time()
        self.slice = conversation_slice(self.id)
        self.bridge_lock = threading.Lock()     # Claude Code's tool calls, one at a time (per conversation)
        self.tools = Tools(cfg, make_executor(cfg, self.slice, user=self.user, workdir=copy_dir))
        self.log: RunLog
        self.session: Session

    @property
    def busy(self) -> bool:
        return self.state != "idle"

    @property
    def offline(self) -> bool:
        """No internet: every site refused, allowed ones too."""
        return self.private and self.opts["offline"]

    @property
    def pc_only(self) -> bool:
        """Only a model on the PC, its workers too."""
        return self.private and self.opts["pc_only"]

    @property
    def sealed(self) -> bool:
        """Nothing reaches the main folder, another conversation, a shared folder or a tool."""
        return self.private and self.opts["sealed"] and not self.keep_files

    def running_jobs(self) -> list["Worker"]:
        return [w for w in list(self.jobs.values()) if w.session is not None]


class Worker:
    """One job a coordinator handed over: a worker model in its own sandbox, like a
    conversation's: its own user (after the conversations' ones), its own copy of the
    coordinator's folder, its own proxy and box. Several can run at once. When the job ends
    its folder is frozen, and the coordinator brings its changes in (or drops them)."""

    def __init__(self, cfg: config.Config, conv: Conversation, jid: str, slot: int, copy_dir: str,
                 profile: Profile, call: str, instructions: str):
        self.id = jid
        self.conv = conv
        self.slot = slot
        self.user = f"{cfg.conv_user_prefix}{slot}" if not no_sudo() else cfg.user
        self.port = cfg.conv_proxy_port + slot if not no_sudo() else 0
        self.copy_dir = copy_dir
        self.profile = profile
        self.call = call                         # the coordinator's delegate call: its steps nest under it
        self.instructions = instructions
        self.proxy: Proxy | None = None
        self.task_sites: dict[str, bool] = {}
        self.key = secrets.token_hex(16)         # a Claude Code worker's tool calls name it with this
        self.bridge_lock = threading.Lock()
        self.slice = conversation_slice(jid)
        self.tools = Tools(cfg, make_executor(cfg, self.slice, user=self.user, workdir=copy_dir))
        self.session: Session | None = None      # while it works
        self.reason = ""                         # how it ended
        self.report = ""


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
        self.shares = Shares(agent_home=os.path.dirname(cfg.workdir.rstrip("/")) or "/home/agent",
                             rw_root=Path(cfg.shares_rw), view_root=Path(cfg.shares_view),
                             hold_root=Path(cfg.shares_hold))
        self.proxy: Proxy | None = None
        self.brain_proxy: Proxy | None = None     # Claude Code's own way online: Anthropic only
        self.bridge: Bridge | None = None         # where Claude Code's tool calls come in
        self.claude_updating = False
        self.mcp_seen: dict = self._load_json("mcp_seen.json") or {}   # Claude apps on your PC that connected: name -> info
        self.mcp_conv: str | None = None          # the conversation the Claude apps used last
        self.plan_usage = self._load_plan_usage()   # the latest from Claude Code, for the header
        self.pc_context = 0                         # tokens the model on the PC holds (/props), once seen
        self.watcher: ExecWatcher | None = None
        self.made = ToolBox(cfg, notify=lambda: self._broadcast({"kind": "made_tools"}),   # tools the agent built
                            on_run=lambda name, record: self._broadcast(
                                {"kind": "tool_run", **self.made.result(name, record["id"])}))
        # newer versions of UVMC's parts, for Settings > Updates (a notice the first time one is seen)
        # The lock: the VM closed to the PC, only the model on the PC used (lock.py)
        self.vm_lock = Lock(cfg.state_dir)
        self.updates = Updates(cfg.state_dir, claude_version=self.claude_version,
                               notify=lambda items: self._broadcast({"kind": "updates", "new": items}))
        self.copies = WorkCopies(cfg)             # each conversation's own copy of the work folder
        self.copy_lock = threading.Lock()
        # Projects and their privacy levels: which conversation may see which files
        self.projects = Projects(cfg.state_dir / "projects.json", cfg.privacy_general)
        self.pool_missing = self._pool_missing()  # setup makes the conversations' users
        first = cfg.conv_max_open + 1               # the workers' users come after them
        self.job_slots = list(range(first, first + cfg.delegate_slots))   # free ones
        self.job_slot_cond = threading.Condition()
        self.oom_seen: dict[str, int] = {}        # memory boxes: processes stopped for lack of memory, so far
        # Private conversations: whether this machine keeps nothing of them (checked at start in
        # the VM, private.py), and what closing one couldn't remove.
        self.private_check: dict = {"state": "ok" if no_sudo() else "checking", "problems": []}
        self.private_left: list[str] = []
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
        # Who may read each shared folder: set now (closing what an earlier run left open), and
        # again every half minute, for a folder shared meanwhile (set_access skips unchanged ones).
        def share_loop():
            while True:
                self._share_access()
                time.sleep(30)
        threading.Thread(target=share_loop, daemon=True, name="share-access").start()
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
            # In the VM: is nothing of a private conversation kept? And users left as they were.
            threading.Thread(target=self._start_private, name="uvmc-private", daemon=True).start()
        if brain:
            self.start_brain_support()
        if audit:   # outside the VM (tests) there are no boxes to watch
            threading.Thread(target=self._memory_watch, name="uvmc-memory", daemon=True).start()
        if audit and self.cfg.updates_check:   # and no version checks from the tests
            threading.Thread(target=self.updates.loop, args=(self.cfg.updates_every_hours,),
                             name="uvmc-updates", daemon=True).start()

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
                     or any(w.session.brain is not None for w in c.running_jobs())]
            alert(using, "claude-code",
                  f"Claude Code ran out of memory: its box ({cap('uvmc-brain.slice')} for all \"my plan\" "
                  "conversations and workers together) was full, so the system stopped one of its processes and "
                  "a reply may have broken off. Fewer \"my plan\" conversations at once helps; or raise "
                  "[subscription] memory in config.toml (or give the VM more memory), then run update.")
        agent_box = cap(SLICE_AGENT)
        hit = []
        for c in convs:
            if grew(f"{SLICE_AGENT}/{c.slice}") or any(grew(f"{SLICE_AGENT}/{w.slice}") for w in c.running_jobs()):
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
        w = None
        if c is None and given:   # a Claude Code worker's calls
            w = next((w for c in self.convs.values() for w in c.running_jobs()
                      if hmac.compare_digest(w.key, given)), None)
            c = w.conv if w is not None else None
        if c is None and not given:   # an MCP server from before conversations had keys
            with_brain = [c for c in self.convs.values() if c.session.brain is not None]
            c = with_brain[0] if len(with_brain) == 1 else None
        if c is None:
            closed = "This conversation was closed" if given else "No task is running"
            return {"text": f"{closed}, so nothing was done.", "is_error": True}
        s = w.session if w is not None else c.session
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
        if w is None and name == "delegate":   # jobs handed over together run together
            result = s._handle(ToolCall(call_id, name, args))
        else:
            with w.bridge_lock if w is not None else c.bridge_lock:  # one at a time, like the other models' calls
                result = s._handle(ToolCall(call_id, name, args))
        return {"text": result["content"], "is_error": result["is_error"]}

    # --- conversations and events ----------------------------------------------------

    def new_conversation(self, model: str | None = None, origin: str = "panel", focus: bool = True,
                         projects: list[str] | None = None, level: str | None = None,
                         transcript_from: str | None = None, private: bool = False,
                         private_opts: dict | None = None) -> Conversation:
        """Opens a conversation. Over the limit, the oldest idle one with nothing left to bring
        into the main folder (not the current one) closes. projects: the ones it opens (none
        given: General if its level allows, else a scratch project of its own); level: what it
        may see, its model's level or lower; transcript_from: another conversation whose whole
        transcript it starts with (only if it may see everything that one has seen); private: a private
        conversation (private.py: erased when closed); private_opts: its restrictions, each on
        unless given false: offline (no internet), pc_only (only a model on the PC), sealed
        (nothing reaches the main folder)."""
        opts = {k: bool((private_opts or {}).get(k, v)) for k, v in PRIVATE_DEFAULTS.items()}
        if model is not None:
            self.cfg.profile(model)  # KeyError if unknown
        base_conv = self.convs.get(self.current)
        if private and opts["pc_only"] and model is None:   # the model on the PC, if the current one isn't
            fit = [n for n, p in self.cfg.models.items() if privacy.private_ok(p)]
            model = base_conv.model if base_conv and base_conv.model in fit else (fit[0] if fit else None)
            if model is None:
                raise PrivacyError("A private conversation uses only the model on your PC, and no model profile "
                                   "runs there (Settings > Models: one with {pc} in its address, level local).")
        profile = self.cfg.profile(model or (base_conv.model if base_conv else self.cfg.model))
        if private:
            self._check_private(profile, opts["pc_only"])
        chosen_level = bool(level)
        level = self._check_level(profile, "local" if private and opts["pc_only"] else level)
        if projects is not None:
            self._check_projects(projects, level, profile.label)
        context = None
        if transcript_from:
            other = self.conv(transcript_from)
            if other.private:
                raise PrivacyError("A private conversation's transcript isn't handed to another conversation.")
            if privacy.rank(level) < privacy.rank(other.seen):
                raise PrivacyError(f"\"{other.title or 'That conversation'}\" has seen {other.seen} files; a {level} "
                                   "conversation can't start with its transcript.")
            context = self._about(other)
        with self.lock:
            if len(self.convs) >= self.cfg.conv_max_open:
                idle = [c for c in self.convs.values() if not c.busy and c.id != self.current
                        and not c.private and not self.unsaved(c)]   # a private one closes only when you say
                if not idle:
                    raise Busy(f"{len(self.convs)} conversations are open, and each is working or has changes not "
                               "brought into the main folder yet. Close one first.")
                self._close(min(idle, key=lambda c: c.last_active))
            base = self.convs.get(self.current)
            used = {c.slot for c in self.convs.values()}
            slot = next(i for i in range(1, self.cfg.conv_max_open + 1) if i not in used)
            cid = uuid.uuid4().hex[:8]
            c = Conversation(self.cfg, profile.name, origin, slot, str(self.copies.copy_dir(cid)), cid)
            c.private = private
            if private:
                c.opts = opts
            if base is not None and model is None:
                c.worker_model = base.worker_model
            if c.pc_only and c.worker_model and not privacy.private_ok(self.cfg.profile(c.worker_model)):
                c.worker_model = None
            c.level = level
            c.chosen = projects is not None or chosen_level or context is not None or private
            c.context_from = context
            if context is not None:
                c.seen = privacy.higher(c.seen, context.get("seen") or "open")   # its history comes along
            if private:   # counts as having seen its level's files: nothing of it goes anywhere less private
                c.seen = level
            c.projects = list(dict.fromkeys(projects)) if projects is not None else self._default_projects(cid, level)
            self._new_log(c)
            self.convs[c.id] = c
            if focus or not self.current:
                self.current = c.id
        if private:
            err = audit_quiet(c.user, True)
            if err:
                with self.lock:
                    self._close(c)
                raise PrivacyError(f"The audit log could not be told to leave out its commands ({err}), so no "
                                   "private conversation was opened.")
        if self.net_on:
            self._start_proxy(c)
        self._broadcast({"kind": "conversations", "conv": c.id})
        return c

    # --- privacy: projects and levels (privacy.py) ---------------------------------------------

    # --- the lock (lock.py) ----------------------------------------------------------------

    def model_allowed(self, profile: Profile) -> bool:
        """While the VM is locked, only the model on the PC is used."""
        return privacy.pc_model(profile) or not self.vm_lock.locked

    def _locked_text(self, profile: Profile) -> str:
        return (f"UVMC is locked: only the model on your PC works, and {profile.label} isn't it. Unlock it in "
                "the panel, or switch this conversation to the model on your PC.")

    def set_lock(self, on: bool, pin: str) -> dict:
        """Locks or unlocks the VM (the PIN both ways; the first lock sets it). Locking stops the
        tasks that use another model and closes Claude Code's idle processes."""
        self.vm_lock.set(on, pin)
        if on:
            for c in list(self.running()):
                profiles = [self.cfg.profile(c.model)] + [w.profile for w in c.running_jobs()]
                if not all(privacy.pc_model(p) for p in profiles):
                    c.log.event("notice", text="Stopped: UVMC was locked, and this task uses a model that isn't on "
                                               "your PC.")
                    self.stop(c.id)
            with self.lock:
                for c in self.convs.values():
                    if not c.busy and c.session.brain is not None:
                        c.session.close()
        text = ("UVMC is locked: no program on your PC can reach the VM, and only the model on your PC works."
                if on else "UVMC is unlocked.")
        for c in list(self.convs.values()):
            c.log.event("notice", text=text)
        self._broadcast({"kind": "lock"})
        return self.vm_lock.view()

    def _check_private(self, profile: Profile, pc_only: bool = True) -> None:
        """A private conversation may start: the machine was checked, and (pc_only) the model is on the PC."""
        check = self.private_check
        if check["state"] == "checking":
            raise PrivacyError("UVMC is still checking that private conversations leave nothing behind on this "
                               "machine. Try again in a minute.")
        if check["state"] != "ok":
            raise PrivacyError("Private conversations are off: " + " ".join(check["problems"]))
        if pc_only and not privacy.private_ok(profile):
            raise PrivacyError(f"This private conversation uses only the model on your PC (level local); "
                               f"{profile.label} isn't one.")

    def private_view(self) -> dict:
        """For the panel: whether private conversations can start, why not, and what closing
        one couldn't remove."""
        return {"state": self.private_check["state"], "problems": list(self.private_check["problems"]),
                "left": list(self.private_left),
                "models": [n for n, p in self.cfg.models.items() if privacy.private_ok(p)]}

    def _start_private(self) -> None:
        """At start, in the VM: audit rules a previous run left for private conversations are
        taken off, the machine is checked (private.py), and the users no conversation has now
        are cleaned (a crash leaves them as they were)."""
        in_use = {c.user for c in self.convs.values()}
        pool = agent_users(self.cfg)[1:]
        try:
            for user in pool:
                audit_quiet(user, False)
            problems = private_mod.self_check(self.cfg, spare_user=pool[-1])
        except Exception as e:   # noqa: BLE001 (never keep the supervisor from starting)
            problems = [f"The check itself failed: {type(e).__name__}: {e}"]
        self.private_check = {"state": "failed" if problems else "ok", "problems": problems}
        self._broadcast({"kind": "private"})
        for user in pool:
            # Looked at again for each user: a conversation or job may have started meanwhile.
            busy = {c.user for c in list(self.convs.values())} | {
                w.user for c in list(self.convs.values()) for w in list(c.jobs.values())}
            if user not in in_use and user not in busy:
                try:
                    left = clean_user(self.cfg, user)
                except Exception as e:   # noqa: BLE001
                    left = [str(e)]
                if left:
                    print(f"Warning: cleaning {user}: {'; '.join(left)}")

    def _sweep_private(self) -> None:
        """What private conversations left behind (a crash, a restart) is erased: they never come back."""
        try:
            for f in Path(self.cfg.private_log_dir).glob("*.jsonl"):
                f.unlink(missing_ok=True)
        except OSError as e:
            self.private_left.append(f"{self.cfg.private_log_dir}: {e}")
        try:
            metas = list(self.copies.state.glob("*/meta.json"))
        except OSError:
            metas = []
        for meta in metas:
            try:
                private = bool(json.loads(meta.read_text(encoding="utf-8")).get("private"))
            except (OSError, ValueError):
                continue
            if private:
                try:
                    self.copies.remove(meta.parent.name)
                except (SyncError, OSError) as e:
                    self.private_left.append(f"a private conversation's copy ({meta.parent.name}): {e}")

    @staticmethod
    def _check_sealed(c: Conversation) -> None:
        if c.sealed:
            raise PrivacyError("This private conversation keeps its files to itself: nothing of it reaches the main "
                               "folder. When you close it, you can keep its files or erase them.")

    def keep_files(self, cid: str) -> None:
        """You chose to keep a sealed private conversation's files: it may bring them into the main
        folder (only that; shared folders and tools stay closed to it)."""
        c = self.conv(cid)
        if not (c.private and c.opts["sealed"]):
            raise ValueError("Only a private conversation that keeps its files to itself needs this.")
        c.keep_files = True
        c.log.event("notice", text="You chose to keep this conversation's files: bring them into the main folder, "
                                   "then close it to erase the rest.")
        self._broadcast({"kind": "conversations", "conv": c.id})

    @staticmethod
    def _name_for_others(c: Conversation) -> str:
        """How a conversation is named in another one's log."""
        return "a private conversation" if c.private else (c.title or "untitled")

    def _check_level(self, profile: Profile, level: str | None) -> str:
        """A conversation's level: its model's (the ceiling) unless you choose a lower one."""
        if level is None or level == "":
            return profile.level
        privacy.check(level)
        if privacy.rank(level) > privacy.rank(profile.level):
            raise PrivacyError(f"{profile.label} is {profile.level}: a conversation with it can't be {level}.")
        return level

    def _check_projects(self, names: list[str], level: str, who: str) -> None:
        if not names:
            raise ValueError("Choose at least one project.")
        for n in names:
            if not self.projects.has(n):
                raise KeyError(f"There's no project called '{n}'.")
            if is_share(n) and share_of(n) not in self.shares.names():
                raise KeyError(f"No folder called '{share_of(n)}' is shared from your PC right now.")
            lowest = self.projects.lowest(n)
            if privacy.rank(lowest) > privacy.rank(level):
                raise PrivacyError(f"{self._project_label(n)} is {self.projects.project_level(n)}: "
                                   f"{who} ({level}) may not open it.")

    def _default_projects(self, cid: str, level: str) -> list[str]:
        """No projects chosen: General if the level allows, else a scratch project of its own
        (made at its first task)."""
        if privacy.rank(self.projects.lowest(GENERAL)) <= privacy.rank(level):
            return [GENERAL]
        return [f"scratch-{cid}"]

    def _project_label(self, name: str) -> str:
        return self.projects.label(name)

    @staticmethod
    def _folders(names: list[str]) -> tuple[str, ...]:
        """The projects that are folders of the main folder (not General, not shares)."""
        return tuple(n for n in names if n != GENERAL and not is_share(n))

    def scope(self, c: Conversation | Worker) -> Scope:
        """What a conversation may see: the one check for its copy, its bring-in, its briefing."""
        conv = c.conv if isinstance(c, Worker) else c
        level = privacy.lower(conv.level, c.profile.level) if isinstance(c, Worker) else conv.level
        return Scope(self.projects, conv.projects, level, conv.seen, conv.given)

    def _note_seen(self, c: Conversation) -> None:
        """Its "seen" level only goes up: the history keeps what it read, even once deleted."""
        try:
            levels = self.copies.visible_levels(c.id, self.scope(c))
        except (OSError, SyncError):
            return
        for lvl in levels:
            c.seen = privacy.higher(c.seen, lvl)
        for share in self.scope(c).shares(self.shares.names()):   # opening one counts as seeing it
            c.seen = privacy.higher(c.seen, self.projects.share_level(share))

    def share_access(self) -> dict[str, set[str]]:
        """Which Linux users may read which shared folder now: each conversation's user for the
        shares it opened (level allowing), and its running workers' users (at their level)."""
        names = self.shares.names()
        access: dict[str, set[str]] = {n: set() for n in names}
        for c in list(self.convs.values()):
            for share in self.scope(c).shares(names):
                access[share].add(c.user)
            for w in list(c.jobs.values()):
                if w.session is not None:
                    for share in self.scope(w).shares(names):
                        access[share].add(w.user)
        return access

    def _share_access(self) -> None:
        """Puts share_access() into the VM (each share's folder lets in only those users)."""
        try:
            self.shares.set_access(self.share_access())
        except Exception as e:   # never stops the work; the folder stays closed to whoever isn't listed
            print(f"Warning: could not set access to the shared folders: {e}")

    def _claim_new_projects(self, c: Conversation) -> list[str]:
        """A top-level folder the agent made, outside General, becomes a project at the
        conversation's level (General, when it's open, claims it as usual)."""
        if GENERAL in c.projects or not self.copies.exists(c.id):
            return []
        made = []
        try:
            entries = list(Path(c.copy_dir).iterdir())
        except OSError:
            return []
        for e in entries:
            name = e.name
            if not e.is_dir() or name == MERGE_DIR or name in c.projects or self.projects.has(name):
                continue
            if (Path(self.cfg.workdir) / name).exists() or not privacy.NAME.match(name):
                continue   # something of General's, which it can't see: stays blocked
            self.projects.add(name, c.level, by=c.id)
            c.projects.append(name)
            made.append(name)
        if made:
            self._save_meta(c)
            c.log.event("notice", text=f"New project{'s' if len(made) > 1 else ''} made by this conversation: "
                                       f"{', '.join(made)} ({c.level}). Change the level in the panel if needed.")
            self._broadcast({"kind": "projects"})
        return made

    def projects_view(self) -> dict:
        d = self.projects.describe()
        users = {}
        for c in self.convs.values():
            for n in c.projects:
                users.setdefault(n, []).append({"id": c.id, "title": c.title or "untitled"})
        for p in d["projects"]:
            p["conversations"] = users.get(p["name"], [])
            p["exists"] = p["name"] == GENERAL or (Path(self.cfg.workdir) / p["name"]).is_dir()
        d["models"] = [{"name": m.name, "label": m.label, "level": m.level} for m in self.cfg.models.values()]
        known = d.pop("shares")
        d["shares"] = [{"name": f"share:{n}", "share": n, "label": n, "level": self.projects.share_level(n),
                        "asked": bool((known.get(n) or {}).get("asked")), "lowest": self.projects.share_level(n),
                        "conversations": users.get(f"share:{n}", []), "exists": True}
                       for n in self.shares.names()]
        return d

    def add_project(self, name: str, level: str, cid: str | None = None) -> dict:
        """A new, empty project; its level is the conversation's model's unless you choose."""
        name = name.strip()
        if not level:
            level = self.cfg.profile(self.conv(cid).model if (cid or self.current) in self.convs else None).level
        if self.projects.has(name):
            raise ValueError(f"There's already a project called '{name}'.")
        if (Path(self.cfg.workdir) / name).exists():
            raise ValueError(f"'{name}' is already in the main folder (in General). Choose another name.")
        self.projects.add(name, level)
        with self.copy_lock:
            self.copies.make_folder(name)
        self._broadcast({"kind": "projects"})
        return self.projects_view()

    def claim_project(self, name: str, level: str, confirm: bool = False) -> dict:
        """A folder already in General becomes a project of its own, with its level. Conversations
        on General that may not see it any more keep what they have, and bring nothing back there."""
        if self.projects.has(name) and name != GENERAL:
            raise ValueError(f"'{name}' is already a project.")
        if not (Path(self.cfg.workdir) / name).is_dir():
            raise KeyError(f"There's no folder called '{name}' in the main folder.")
        privacy.check(level)
        current = self.projects.level(name)
        if privacy.rank(level) < privacy.rank(current) and not confirm:
            raise NeedsConfirm(self._loosen_text(name, current, level))
        self.projects.add(name, level)
        self._broadcast({"kind": "projects"})
        return self.projects_view()

    def _loosen_text(self, what: str, current: str, level: str) -> str:
        """What making something less private means: which of your models gain access."""
        gain = [m.label for m in self.cfg.models.values()
                if privacy.rank(level) <= privacy.rank(m.level) < privacy.rank(current)]
        return (f"Make {what} {level} (now {current})? "
                + (f"These models will be able to see it: {', '.join(gain)}." if gain else
                   "None of your models gains access."))

    def set_project_level(self, name: str, level: str, confirm: bool = False) -> dict:
        privacy.check(level)
        if not self.projects.has(name) or (is_share(name) and share_of(name) not in self.shares.names()):
            raise KeyError(f"There's no project called '{name}'.")
        if self.projects.loosening(name, level) and not confirm:
            raise NeedsConfirm(self._loosen_text(self._project_label(name), self.projects.project_level(name), level))
        self.projects.set_level(name, level)
        self._after_level_change()
        return self.projects_view()

    def set_zone(self, path: str, level: str | None, confirm: bool = False) -> dict:
        """A folder with its own level (None: back to its project's)."""
        current = self.projects.level(path)
        if level is not None:
            privacy.check(level)
        new = level or self.projects.project_level(self.projects.project_of(path))
        if privacy.rank(new) < privacy.rank(current) and not confirm:
            raise NeedsConfirm(self._loosen_text(f"the folder {path}", current, new))
        self.projects.set_zone(path, level)
        self._after_level_change()
        return self.projects_view()

    def set_mark(self, path: str, level: str | None, confirm: bool = False) -> dict:
        """A single file marked stricter than where it is ("sensitive": local)."""
        current, around = self.projects.level(path), self.projects.level(path, with_mark=False)
        new = privacy.higher(around, level) if level else around
        if privacy.rank(new) < privacy.rank(current) and not confirm:
            raise NeedsConfirm(self._loosen_text(path, current, new))
        self.projects.set_mark(path, level)
        self._after_level_change()
        return self.projects_view()

    def _after_level_change(self) -> None:
        """Raising a level: conversations whose model may no longer see something they have keep
        their copy, but a running one is told; nothing more private reaches them from now on.
        A shared folder is live: who may read it changes at once."""
        self._share_access()
        self._broadcast({"kind": "projects"})

    def open_project(self, cid: str, name: str) -> dict:
        """Adds a project to a conversation that isn't working: its files come into the copy."""
        c = self.conv(cid)
        self._check_projects([name], c.level, self.cfg.profile(c.model).label)
        with self.lock:
            if c.busy:
                raise Busy("Add a project when this conversation isn't working.")
            if name in c.projects:
                return {"projects": c.projects}
            c.projects.append(name)
        if is_share(name):
            self._note_seen(c)
            self._share_access()
            c.session.briefing = None
            c.log.event("notice", text=f"The shared folder {share_of(name)} added to this conversation "
                                       f"(/mnt/shared/{share_of(name)}, read-only).")
        elif self.copies.exists(c.id):
            with self.copy_lock:
                added = self.copies.add_visible(c.id, c.user, self.scope(c), self._folders([name]))
            self._note_seen(c)
            c.session.briefing = None   # told afresh at the next task (Claude Code restarts with it)
            c.log.event("notice", text=f"Project {self._project_label(name)} added to this conversation "
                                       f"({len(added)} file{'s' if len(added) != 1 else ''}).")
        self._save_meta(c)
        self._broadcast({"kind": "conversations", "conv": c.id})
        return {"projects": c.projects}

    def give_file(self, cid: str, path: str, confirm: bool = False) -> dict:
        """One file it may not see, given to this conversation as a one-off: the file keeps its
        level. The conversation has then seen that level, so its changes into less private places
        are held for your review, and only models allowed that level may take over from it."""
        c = self.conv(cid)
        rel = privacy._clean(path)
        if not (Path(self.cfg.workdir) / rel).is_file():
            raise KeyError(f"There's no file {rel} in the main folder.")
        if self.scope(c).visible(rel):
            raise ValueError(f"This conversation can already see {rel}.")
        level = self.projects.level(rel)
        profile = self.cfg.profile(c.model)
        if not confirm:
            raise NeedsConfirm(
                f"Give {rel} ({level}) to \"{c.title or 'this conversation'}\"? {profile.label} ({profile.level}) will read "
                f"it. The file stays {level}; this conversation's changes into less private places will wait for your "
                "review.")
        with self.lock:
            if c.busy:
                raise Busy("Give it a file when this conversation isn't working.")
        self.ensure_copy(c)
        with self.copy_lock:
            self.copies.give(c.id, c.user, rel)
        c.given.add(rel)
        c.seen = privacy.higher(c.seen, level)
        self._save_meta(c)
        c.log.event("notice", text=f"You gave this conversation {rel} ({level}). It is in its copy now; the file "
                                   "keeps its level in the main folder.")
        self._broadcast({"kind": "conversations", "conv": c.id})
        return {"given": sorted(c.given), "seen": c.seen}

    def held_diff(self, cid: str, path: str) -> dict:
        c = self.conv(cid)
        held = {x["path"] for x in self.copies.plan(c.id, self.scope(c))["held"]}
        if path not in held:
            raise KeyError("That change isn't waiting for your review.")
        return self.copies.held_diff(c.id, path)

    def _start_proxy(self, c: Conversation) -> None:
        """A conversation's own proxy: every connection it makes is known to be its own."""
        if c.proxy is not None:
            return
        try:
            c.proxy = Proxy(c.port, self.cfg.net_ports, self.sites,
                            lambda host, port, c=c: self.decide_site(c, host, port),
                            lambda info, c=c: self._on_net(c, info), offline=c.offline).start()
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
            if c.private:   # only enough to erase it at the next start
                (d / "meta.json").write_text(json.dumps({"private": True, "slot": c.slot}), encoding="utf-8")
                return
            (d / "meta.json").write_text(json.dumps({"title": c.title, "slot": c.slot, "origin": c.origin,
                                                     "model": c.model, "created": c.created,
                                                     "projects": c.projects, "level": c.level,
                                                     "seen": c.seen, "given": sorted(c.given)}),
                                         encoding="utf-8")
        except OSError:
            pass

    def _recover(self) -> None:
        """Conversations whose copies outlived a restart come back, so their work can still be
        brought in. Their chat history is in Runs; the model starts afresh. Private ones are
        erased instead."""
        self._sweep_private()
        try:
            metas = sorted(self.copies.state.glob("*/meta.json"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        try:   # jobs don't survive a restart: their coordinator starts afresh
            for d in self.copies.root.iterdir():
                if JOB_ID.match(d.name):
                    self.copies.remove(d.name)
        except (OSError, SyncError):
            pass
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
            # From before projects (0.14 and older): its copy holds the whole main folder, so it
            # counts as General, at the most private level, and its model must be allowed that far.
            c.projects = [p for p in m.get("projects", [GENERAL]) if isinstance(p, str)] or [GENERAL]
            ceiling = self.cfg.profile(model).level
            c.level = m.get("level") if m.get("level") in privacy.RANK else ceiling
            c.level = privacy.lower(c.level, ceiling)
            c.seen = m.get("seen") if m.get("seen") in privacy.RANK else "local"
            c.given = {p for p in m.get("given", []) if isinstance(p, str)}
            c.chosen = True
            self._new_log(c)
            c.log.event("notice", text="UVMC restarted. This conversation's copy of the work folder is kept, "
                                       "so you can still bring its changes in; the agent starts afresh.")
            self.convs[c.id] = c
            self.current = c.id

    def _new_log(self, c: Conversation) -> None:
        old = getattr(c, "session", None)
        if old is not None:
            old.close()
        c.log = RunLog(self.cfg.private_log_dir if c.private else self.cfg.log_dir,
                       listener=lambda r, cid=c.id: self._broadcast({**r, "conv": cid}), private=c.private)
        c.session = Session(self.cfg, None, lambda tool, args, reason, call_id="", c=c: self._approve(c, tool, args, reason, call_id),
                            c.log, out=lambda *_: None, cancel=c.cancel, tools=c.tools)
        c.session.bridge_key = c.key
        c.log.event("session", model=c.model, origin=c.origin)

    def new_session(self) -> Conversation:
        """"New conversation": opens a fresh one next to the others (same model and worker as
        the current one) and makes it current."""
        return self.new_conversation()

    def ensure_copy(self, c: Conversation) -> None:
        """A conversation's copy of the main work folder (what it may see of it), made at its
        first task. Its scratch project, if it has one, is made now."""
        if not self.copies.exists(c.id):
            if c.private:   # first, so a crash while copying still leaves it marked for erasing
                self._save_meta(c)
            for name in c.projects:
                if name != GENERAL and not self.projects.has(name) and name == f"scratch-{c.id}":
                    self.projects.add(name, c.level, scratch=True, by=c.id)
                    self._broadcast({"kind": "projects"})
            # A project removed meanwhile (or never made) is dropped rather than left dangling.
            c.projects = [n for n in c.projects if self.projects.has(n)] or c.projects
            with self.copy_lock:
                for name in self._folders(c.projects):
                    self.copies.make_folder(name)
                self.copies.create(c.id, c.user, self.scope(c), self._folders(c.projects))
            self._note_seen(c)
            self._share_access()
            c.log.event("workcopy", path=c.copy_dir, shared=self.cfg.workdir,
                        projects=[self._project_label(n) for n in c.projects], level=c.level)
            if c.context_from is not None:   # the transcript you started it with
                c.context_file = self._hand_transcript(c, c.context_from, CONTEXT_DIR)
        self._save_meta(c)

    def unsaved(self, c: Conversation) -> int:
        """How many of its changes aren't in the main folder yet (held and blocked ones too:
        closing drops them)."""
        try:
            plan = self.copies.plan(c.id, self.scope(c))
        except OSError:
            return 0
        return (len(plan["mine"]) + len(plan["merged"]) + len(plan["conflicts"]) + len(plan.get("resolving", []))
                + len(plan["held"]) + len(plan["blocked"]))

    def close_conversation(self, cid: str, discard: bool = False, erase: bool = False) -> None:
        """erase: for a private conversation, you confirmed that it is erased for good."""
        with self.lock:
            c = self.conv(cid)
            if c.busy:
                raise Busy("This conversation is working. Stop it first.")
            n = self.unsaved(c)
            if n and not discard:
                if c.sealed:
                    e = Unsaved(f"This private conversation has {n} changed file{'s' if n > 1 else ''}. Keep them "
                                "(you bring them into the main folder, then it's erased), or erase everything.")
                    e.sealed = True
                    raise e
                raise Unsaved(f"This conversation has {n} change{'s' if n > 1 else ''} not brought into the shared "
                              "folder. Bring them in first, or close it anyway and drop them.")
            if c.private and not erase:
                raise NeedsConfirm("Erase this private conversation for good? Its messages, its steps and its "
                                   "copy of the files are deleted, and it can't be opened again. Files you "
                                   "brought into the main folder stay.")
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
        for jid in list(c.jobs):
            self._drop_job(c, jid)
        left = []
        try:
            self.copies.remove(c.id)
        except (SyncError, OSError) as e:
            left.append(f"its copy of the work folder ({e})")
        # Its user's leftovers go too (every conversation): what it still runs, its home folder,
        # its files in /tmp. The next conversation with this user starts clean.
        kill_slice(c.slice)
        try:
            left += clean_user(self.cfg, c.user)
        except Exception as e:   # noqa: BLE001 (a timeout)
            left.append(str(e))
        if c.private:
            # PRIVATE CONVERSATIONS: everything it left is erased here (CLAUDE.md, "Private conversations").
            c.log.erase()
            err = audit_quiet(c.user, False)
            if err:
                left.append(f"the audit rules for {c.user} ({err})")
            for recent in (self.net_recent, self.exec_recent):
                kept = [x for x in recent if x.get("conv") != c.id]
                recent.clear()
                recent.extend(kept)
            if left:
                self.private_left += [f"A private conversation: {x}" for x in left]
                self._broadcast({"kind": "private"})
        elif left:
            print(f"Warning: closing conversation {c.id} left: {'; '.join(left)}")
        scratch = f"scratch-{c.id}"
        if scratch in c.projects and self.projects.has(scratch):
            try:   # an unused scratch project goes with its conversation; one with files stays
                if self.copies.remove_empty_folder(scratch) or not (Path(self.cfg.workdir) / scratch).exists():
                    self.projects.remove(scratch)
            except (SyncError, OSError):
                pass
        self.convs.pop(c.id, None)
        self._share_access()
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
                        "created": c.created, "last_active": c.last_active, "current": c.id == self.current,
                        "projects": [{"name": n, "label": self._project_label(n)} for n in c.projects],
                        "level": c.level, "seen": c.seen, "private": c.private, "private_opts": dict(c.opts) if c.private else None, "sealed": c.sealed,
                        "waits_for_unlock": c.model in self.cfg.models and not self.model_allowed(self.cfg.profile(c.model)),
                        "ceiling": self.cfg.models[c.model].level if c.model in self.cfg.models else "open"})
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
                 "on_pc": "{pc}" in p.base_url, "level": p.level}
                for p in self.cfg.models.values()]

    def local_model(self) -> dict:
        """The models on your PC (windows\\local-model.cmd): found, running, which ones, which one is used."""
        from . import local_models, pc
        p = next((p for p in self.cfg.models.values() if pc.PLACEHOLDER in p.base_url), None)
        picked = self.settings.local_model()
        out = {"profile": p.name if p else None, "label": p.label if p else "", "pc": pc.address(),
               "has_key": bool(p and config.has_key(p)), "running": False, "key_ok": None,
               "model": "", "file": "", "context": 0, "build": "", "error": "",
               "router": False, "models": [], "picked": picked, "in_use": "",
               "hardware": local_models.hardware(), "families": []}
        hw = out["hardware"]
        out["families"] = [{**{k: f.get(k) for k in ("id", *local_models.FAMILY)},
                            "sizes": [{**{k: s.get(k) for k in ("id", *local_models.SIZE)},
                                       "fit": local_models.fit(s, hw)} for s in f["sizes"]]}
                           for f in local_models.families()]
        if not p or not out["pc"]:
            return out
        import httpx
        root = pc.resolve(p.base_url).rstrip("/").removesuffix("/v1")
        key = config.api_key(p.key)
        auth = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            r = httpx.get(root + "/models", headers=auth, timeout=3)
        except httpx.HTTPError as e:
            out["error"] = f"Nothing answers at {root} ({type(e).__name__})."
            return out
        out["running"] = True
        if r.status_code == 401:
            out["key_ok"] = False
            return out
        out["key_ok"] = True
        try:
            rows = local_models.router_models(r.json()) if r.status_code == 200 else None
        except ValueError:
            rows = None
        props_q = {}
        if rows is not None:
            # local-model 0.16 and later: llama.cpp's router, several models, one on the card at a time.
            out["router"] = True
            out["models"] = [{**local_models.info(m["id"]), "status": m["status"]} for m in rows]
            out["in_use"] = out["model"] = local_models.choose(picked, rows)
            loaded = [m["id"] for m in rows if m["status"] == "loaded"]
            if not loaded:
                return out
            props_q = {"model": loaded[0]}
        try:
            r = httpx.get(root + "/props", headers=auth, params=props_q, timeout=3)
            d = r.json()
        except (httpx.HTTPError, ValueError):
            if not out["router"]:
                out["error"] = f"Unexpected answer from {root}."
            return out
        if not isinstance(d, dict):
            return out
        out.update(build=str(d.get("build_info") or ""),
                   file=str(d.get("model_path") or "").replace("\\", "/").rsplit("/", 1)[-1],
                   context=int((d.get("default_generation_settings") or {}).get("n_ctx") or 0))
        if out["context"]:
            self.pc_context = out["context"]
        if not out["router"]:
            out["model"] = out["in_use"] = str(d.get("model_alias") or "")
        return out

    def set_local_model(self, mid: str) -> dict:
        """Picks the model on the PC UVMC asks for, from the next task on, and asks the PC to load it
        now so the first answer doesn't wait for it. A size on the list that isn't downloaded yet can
        be picked too: UVMC uses it as soon as local-model -Add has fetched it (the panel shows how)."""
        from . import local_models
        status = self.local_model()
        have = mid in [m["id"] for m in status["models"]]
        if mid and status["router"] and not have and mid not in local_models.catalog():
            raise SettingsError(f"'{mid}' is neither downloaded on your PC nor on UVMC's list.")
        self.settings.set_local_model(mid)
        if mid and status["router"] and have:
            from . import pc
            import httpx
            p = self.cfg.profile(status["profile"])
            key = config.api_key(p.key)
            try:   # loading takes a few seconds; the panel checks back
                httpx.post(pc.resolve(p.base_url).rstrip("/").removesuffix("/v1") + "/models/load",
                           json={"model": mid}, headers={"Authorization": f"Bearer {key}"} if key else {}, timeout=2)
            except httpx.HTTPError:
                pass
        if mid and status["router"] and not have:
            text = f"The model on your PC will be {mid} once it's downloaded (local-model -Add {mid} on the PC)."
        elif mid:
            text = f"The model on your PC is now {mid}."
        else:
            text = "The model on your PC: whichever local-model has loaded."
        self.log.event("notice", text=text)
        return self.local_model()

    def set_model(self, name: str, cid: str | None = None, confirm: bool = False) -> None:
        """Switching model. To one at least as safe as the current: done. To a less safe one
        that may see everything this conversation has seen: only once you confirm. To one that
        may not: refused (the history holds those files); start a new conversation instead."""
        profile = self.cfg.profile(name)  # KeyError if unknown
        c = self.conv(cid)
        if c.pc_only and not privacy.private_ok(profile):
            raise PrivacyError(f"This private conversation keeps the model on your PC; {profile.label} isn't one.")
        old = self.cfg.profile(c.model) if c.model in self.cfg.models else None
        started = self.copies.exists(c.id) or bool(c.session.messages)
        new_level = profile.level
        if privacy.rank(new_level) < privacy.rank(c.level):
            if started and privacy.rank(new_level) < privacy.rank(c.seen):
                raise PrivacyError(
                    f"This conversation has seen {c.seen} files, and {profile.label} is {new_level}: it would get them "
                    f"with the conversation's history. Start a new conversation with {profile.label} instead "
                    "(on the projects it may open).")
            if started and not confirm:
                names = ", ".join(self._project_label(n) for n in c.projects)
                raise NeedsConfirm(
                    f"Switch from {old.label if old else c.model} to {profile.label} ({new_level})? It will get this "
                    f"conversation's history and the files of: {names}" + (
                        f", and this conversation will be {new_level} from now on." if c.level != new_level else "."))
        elif old is not None and privacy.rank(new_level) < privacy.rank(old.level) and started and not confirm:
            raise NeedsConfirm(f"Switch from {old.label} ({old.level}) to {profile.label} ({new_level})? "
                               "It will get this conversation's history and its files.")
        with self.lock:
            if c.busy:
                raise Busy("Change the model when this conversation isn't working.")
            c.model = profile.name
            if not started and not c.chosen:
                # Nothing seen yet and nothing chosen: it follows the model, as if opened with it.
                c.level = new_level
                c.projects = self._default_projects(c.id, new_level)
            elif privacy.rank(new_level) < privacy.rank(c.level):
                c.level = new_level
                if not started:   # nothing seen yet: its projects must still allow the new level
                    c.projects = [n for n in c.projects
                                  if privacy.rank(self.projects.lowest(n)) <= privacy.rank(new_level)
                                  or n == f"scratch-{c.id}"] or [f"scratch-{c.id}"]
            c.session.close()  # a Claude Code process belongs to the model it was started with
        self._save_meta(c) if self.copies.exists(c.id) else None
        self._share_access()
        c.log.event("model", model=profile.name, label=profile.label, level=c.level)

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
        if name is not None and c.pc_only and not privacy.private_ok(self.cfg.profile(name)):
            raise PrivacyError("This private conversation's worker can only be the model on your PC.")
        if name is not None and privacy.rank(self.cfg.profile(name).level) < privacy.rank(c.seen):
            p = self.cfg.profile(name)
            raise PrivacyError(f"{p.label} is {p.level}, and this conversation has {c.seen} files it would hand on. "
                               f"Choose a worker at least {c.seen}.")
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
                "Hand a job to a worker agent that operates the machine (shell, files, packages). The worker "
                "starts with no memory of this conversation, so the instructions must contain everything it "
                "needs: goal, context, constraints, what done looks like, and where to save results. It works "
                "in its own copy of your work folder, so its changes reach your folder only when you bring "
                f"them in (bring_in_worker). Several delegate calls in one reply run at the same time (up to "
                f"{self.cfg.delegate_parallel}), each in its own copy: give them independent jobs. Returns the "
                "worker's final report and the files it changed."),
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
        job_specs, job_handlers = self._job_tools(c)
        if c.worker_model and self.cfg.delegate_enabled:
            s.tool_names = [n for n in ("read_file", "list_dir") if n in self.cfg.tools_enabled]
            own, own_handlers = self._share_tools(c)
            s.extra_tools = [self._delegate_definition(c), *job_specs, PROPOSE_SPEC]
            s.handlers = {"delegate": lambda args, call_id, c=c: self._delegate(c, args, call_id),
                          **job_handlers, "propose_tool": own_handlers["propose_tool"]}
            s.parallel_tools = {"delegate"}
            s.system = briefing_mod.compose(self.cfg.orchestrator_rules or rules, b, live_text)
        else:
            s.tool_names = None
            s.system = briefing_mod.compose(rules, b, live_text)
            s.extra_tools, s.handlers = self._share_tools(c)
            s.parallel_tools = set()
            if c.jobs:   # the worker was switched off with jobs not brought in: they can still be
                s.extra_tools = s.extra_tools + job_specs
                s.handlers = {**s.handlers, **job_handlers}

    def _job_tools(self, c: Conversation) -> tuple[list[dict], dict]:
        return [BRING_IN_SPEC, DISCARD_SPEC], {
            "bring_in_worker": lambda args, call_id, c=c: self._bring_in_job(c, args, call_id),
            "discard_worker": lambda args, call_id, c=c: self._discard_job(c, args, call_id)}

    def _live_briefing(self, b: dict, worker: str | None = None, c: Conversation | None = None,
                       job: Worker | None = None) -> str:
        c = c or self.conv()

        def work_names():
            if job is not None:   # its own copy, which the supervisor (in the agent's group) can read
                top = Path(job.copy_dir)
                return sorted(n.name + ("/" if n.is_dir() else "") for n in top.iterdir() if n.name != MERGE_DIR)
            return [e["name"] + ("/" if e["dir"] else "") for e in self.list_files(".", c.id)["entries"]]
        view = SimpleNamespace(cfg=self.cfg, model=c.model, worker_model=c.worker_model,
                               shares=self.shares, sites=self.sites, made=self.made,
                               workdir=(job.copy_dir if job else c.copy_dir) or self.cfg.workdir,
                               shared_workdir=self.cfg.workdir, coordinator_workdir=c.copy_dir,
                               user=job.user if job else c.user, privacy_note=self._privacy_note(c),
                               share_names=self.scope(job or c).shares(self.shares.names()), seen=c.seen,
                               offline=c.offline)
        return briefing_mod.live(view, b["parts"], work_names, worker=worker)

    def _privacy_note(self, c: Conversation) -> str:
        """Its projects, its level, and how many files it may not see (never which ones)."""
        names = list(self._folders(c.projects))
        parts = []
        if names:
            parts.append(f"your project folder{'s' if len(names) > 1 else ''} {', '.join(n + '/' for n in names)}")
        if GENERAL in c.projects:
            parts.append("the top level of the work folder (the General project)")
        text = (f"- This conversation works on {' and '.join(parts)}, at privacy level {c.level}. Changes elsewhere "
                "can't be brought into the main folder.")
        if GENERAL not in c.projects:
            text += " A new folder you make at the top level becomes a new project."
        try:
            hidden = self.copies.hidden_count(self.scope(c))
        except (OSError, SyncError):
            hidden = 0
        if hidden:
            text += (f" {hidden} file{'s are' if hidden > 1 else ' is'} hidden from you by its privacy level; don't "
                     "look for them.")
        if c.given:
            text += f" The user also gave you: {', '.join(sorted(c.given)[:20])}."
        if c.private:
            text += "\n- This is a private conversation: the user erases it when they close it."
            if c.offline:
                text += (" It has no internet: every site is refused, so nothing can be looked up, downloaded or "
                         "installed from online.")
            if c.sealed:
                text += (" It keeps its files to itself: nothing you change reaches the main folder, the user's "
                         "shared folders or a tool (when they close it, they choose whether to keep your files).")
        if c.context_file:
            text += (f"\n- The user started this conversation with another one's whole transcript, in {c.context_file} "
                     f"(\"{(c.context_from or {}).get('title') or 'untitled'}\"): read it first, it's where this work "
                     "comes from.")
        return text

    def briefing_view(self) -> dict:
        b = self.settings.briefing()
        live_text = self._live_briefing(b) if b["enabled"] else ""
        return {**b, "labels": briefing_mod.PARTS, "notes_max": briefing_mod.NOTES_MAX,
                "house_rules": self.cfg.house_rules, "house_rules_source": self.settings.house_rules_source(),
                "preview": briefing_mod.compose(self.cfg.house_rules, b, live_text),
                "current": self.session.system if self.session.briefing is not None else None}

    def _share_tools(self, c: Conversation, job: Worker | None = None) -> tuple[list[dict], dict]:
        """Tools handled by the supervisor itself: propose_tool, and update_shared_file when a
        folder is shared. job: for a worker, whose files are in its own folder."""
        specs = [PROPOSE_SPEC]
        handlers = {"propose_tool": lambda args, call_id, c=c: self._propose_tool(c, args, call_id, job)}
        if self.made.registry:
            specs.append(GET_CODE_SPEC)
            handlers["get_tool_code"] = lambda args, call_id, c=c: self._get_tool_code(c, args, job)
        if self.scope(job or c).shares(self.shares.names()):
            specs.append(SHARE_SPEC)
            handlers["update_shared_file"] = lambda args, call_id, c=c: self._update_shared(c, args, call_id, job)
        return specs, handlers

    def copy_tool_code(self, c: Conversation, name: str, job: Worker | None = None) -> tuple[str, int, list[str]]:
        """An approved tool's code, copied into the conversation's work folder (or a job's) as
        tools/<name>, or tools/<name>-v<version> if that folder is already there, so nothing of
        the agent's is lost."""
        version, files = self.made.code(name)
        level = self.made.level(name)
        allowed = privacy.lower(c.level, job.profile.level) if job is not None else c.level
        if privacy.rank(level) > privacy.rank(allowed):
            raise PrivacyError(f"The tool {name} is {level}: a {allowed} conversation may not have its code.")
        if job is None:
            self.ensure_copy(c)
        c.seen = privacy.higher(c.seen, level)   # its code may carry what its conversation saw
        at = job or c
        folder = f"tools/{name}"
        if (Path(at.copy_dir) / folder).exists():
            folder = f"tools/{name}-v{version}"
            if (Path(at.copy_dir) / folder).exists():
                return folder, version, sorted(files)   # this version is there already
        self.copies.put_files(at.id, at.user, folder, files)
        return folder, version, sorted(files)

    def _get_tool_code(self, c: Conversation, args: dict, job: Worker | None = None) -> tuple[str, bool]:
        name = str(args.get("name", "")).strip()
        try:
            folder, version, files = self.copy_tool_code(c, name, job)
        except KeyError as e:
            known = ", ".join(sorted(self.made.registry)) or "none"
            return f"{e.args[0]} The approved tools: {known}.", True
        except PrivacyError as e:
            return str(e), True
        except (OSError, SyncError) as e:
            return f"Could not copy it: {e}", True
        return (f"Version {version} of {name} is now in {folder} in your work folder: {', '.join(files[:40])}. "
                f"Change it there, test it (set UVMC_REPORT=uvmc-report.json to see its report), and propose "
                f"it again with propose_tool and folder {folder}."), False

    def work_on_tool(self, name: str) -> dict:
        """A fresh conversation for working on one tool: its code is already in its folder."""
        self.made.get(name)  # KeyError if unknown
        level = self.made.level(name)
        base = self.convs.get(self.current)
        model = self.cfg.profile(base.model if base else self.cfg.model)
        if privacy.rank(model.level) < privacy.rank(level):
            raise PrivacyError(f"This tool is {level} and {model.label} is {model.level}: switch to a model that is at "
                               f"least {level}, then try again.")
        c = self.new_conversation()
        folder, version, _ = self.copy_tool_code(c, name)
        self._save_meta(c)
        title = self.made.registry[name]["manifest"]["title"]
        c.log.event("notice", text=f"UVMC copied version {version} of the tool {title} ({name}) into {folder} "
                                   "in this conversation's folder, for you and the agent to work on.")
        return {"conv": c.id, "folder": folder, "version": version, "name": name, "title": title}

    def set_tool_level(self, name: str, level: str, confirm: bool = False) -> dict:
        """A tool's level, changed later (Tools tab): lowering it asks, like a project's."""
        privacy.check(level)
        current = self.made.level(name)   # KeyError if unknown
        if privacy.rank(level) < privacy.rank(current) and not confirm:
            raise NeedsConfirm(self._loosen_text(f"the tool {name}", current, level))
        self.made.set_level(name, level)
        return self.made.view(name)

    def _propose_tool(self, c: Conversation, args: dict, call_id: str, job: Worker | None = None) -> tuple[str, bool]:
        """A tool the agent built: shown to you in full; installed only if you approve."""
        if c.private and c.opts["sealed"]:
            return ("This is a private conversation that keeps its files to itself: tools can't be installed from "
                    "it. The user can build it in a normal conversation.", True)
        folder = str(args.get("folder", "")).strip()
        try:
            manifest, files = self.made.read_folder((job or c).tools._as_agent, folder)
            info = self.made.preview(manifest, files)
        except ToolError as e:
            return str(e), True
        # Its level: its code may carry what this conversation has seen (and what an earlier
        # version had); you can change it on the card, and later in the Tools tab.
        suggested = c.seen
        if manifest["name"] in self.made.registry:
            suggested = privacy.higher(suggested, self.made.level(manifest["name"]))
        choice: dict = {}
        answer = self._ask("made_tool", {**info, "folder": folder, "reason": str(args.get("reason", ""))[:2000],
                                         "call": call_id, "level": suggested, "seen": c.seen}, [c], choice=choice)
        if answer == "deny":
            return ("The user declined this tool, so nothing was installed. Ask them what to change "
                    "before proposing it again."), True
        level = choice.get("level") or suggested
        try:
            version = self.made.install(manifest, files, level)
        except (ToolError, OSError) as e:
            return f"Installing it failed, so it isn't installed: {e}", True
        c.log.event("made_tool", name=manifest["name"], title=manifest["title"], version=version, call=call_id,
                    level=level)
        todo = self.made.missing(manifest["name"])
        nxt = (f"The user still has to fill in: {', '.join(todo)}. " if todo else "") + \
              "It starts switched off; the user turns it on in the Tools tab."
        return (f"Approved and installed: {manifest['title']} ({manifest['name']}), version {version}. "
                f"The installed copy is frozen; keep your folder {folder} to make changes and propose "
                f"them again. {nxt}"), False

    def _update_shared(self, c: Conversation, args: dict, call_id: str, job: Worker | None = None) -> tuple[str, bool]:
        if c.private and c.opts["sealed"]:
            return ("This is a private conversation that keeps its files to itself: it can't write into the user's "
                    "shared folders. Say what you would change instead.", True)
        share, rel = str(args.get("share", "")), str(args.get("path", ""))
        visible = self.scope(job or c).shares(self.shares.names())
        if share not in visible:
            return (f"'{share}' is not one of this conversation's shared folders"
                    + (f" (yours: {', '.join(visible)})." if visible else "."), True)
        level = self.projects.share_level(share)
        try:
            target = self.shares.target(share, rel)
            src = self.shares.source((job or c).tools._as_agent, str(args.get("source", "")))
            info = self.shares.preview(src, target)
        except ShareError as e:
            return str(e), True
        down = privacy.rank(level) < privacy.rank(c.seen)   # data could flow to a less private place
        answer = self._ask("file", {"share": share, "path": rel, "source": str(src), "level": level, "seen": c.seen,
                                    "warning": (f"This conversation has seen {c.seen} files and {share} is {level}: "
                                                "check that nothing private goes into it.") if down else "",
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
        """One job, in its own sandbox: a user from the workers' pool, a copy of the coordinator's
        folder, its own proxy and box. Several run at once when the coordinator asks for several
        in one reply. The coordinator brings the result in afterwards (bring_in_worker)."""
        instructions = str(args.get("instructions", "")).strip()
        if not instructions:
            return "No instructions given.", True
        name = str(args.get("model") or c.worker_model or "")
        if name not in self.allowed_workers():
            return f"'{name}' is not an allowed worker. Choose one of: {', '.join(self.allowed_workers())}.", True
        profile = self.cfg.profile(name)
        if not self.model_allowed(profile):
            return (f"UVMC is locked: only the model on the user's PC works, and {profile.label} isn't it.", True)
        if c.pc_only and not privacy.private_ok(profile):
            return (f"This is a private conversation: its workers can only be the model on the user's PC, and "
                    f"{profile.label} isn't.", True)
        if privacy.rank(profile.level) < privacy.rank(c.seen):
            # The instructions and the folder carry what this conversation has seen.
            fit = [n for n in self.allowed_workers() if privacy.rank(self.cfg.profile(n).level) >= privacy.rank(c.seen)]
            return (f"{profile.label} is {profile.level}, and this conversation has {c.seen} files: it can't be given "
                    f"this job. " + (f"Workers that may: {', '.join(fit)}." if fit else "No allowed worker may.")), True
        try:
            provider = self.provider_factory(profile)
        except ProviderError as e:
            return f"The worker could not start: {e}", True
        if provider is None:
            return f"The worker could not start: no API key for {profile.label}.", True
        missing = self._job_pool_missing()
        if missing:
            return (f"Workers need their own users, and {missing} doesn't exist yet: the user has to run update "
                    "on their PC first. Nothing was done."), True
        while not c.job_room.acquire(timeout=0.5):   # this coordinator's jobs at once
            if c.cancel.is_set():
                return "Stopped by the user.", True
        slot = None
        try:
            slot = self._take_job_slot(c)            # all conversations' jobs at once
            if slot is None:
                return "Stopped by the user.", True
            with self.lock:
                c.job_seq += 1
                jid = f"{c.id}w{c.job_seq}"
            w = Worker(self.cfg, c, jid, slot, str(self.copies.copy_dir(jid)), profile, call_id, instructions)
            log = ChildLog(c.log, parent=call_id, agent=profile.name)
            if c.private:
                err = audit_quiet(w.user, True)
                if err:
                    return f"The audit log could not be told to leave out the worker's commands ({err}), so nothing was done.", True
            try:
                self.copies.create_job(jid, c.copy_dir, w.user)
            except (SyncError, OSError) as e:
                if c.private:
                    audit_quiet(w.user, False)
                return f"Could not make the job's own folder, so nothing was done: {e}", True
            c.jobs[jid] = w
            log.event("job", id=jid, folder=w.copy_dir, user=w.user)
            if self.net_on:
                self._start_job_proxy(c, w)
            worker = Session(self.cfg, provider, lambda tool, a, reason, cid="": self._approve(c, tool, a, reason, cid),
                             log, out=lambda *_: None, cancel=c.cancel, tools=w.tools)
            worker.owns_cancel = False
            worker.max_steps = self.cfg.delegate_max_steps
            worker.tool_names = [n for n in self.cfg.tools_enabled if n != "delegate"]
            worker.extra_tools, worker.handlers = self._share_tools(c, w)
            # The same three parts as the agent you talk to: house rules, a live briefing (as a worker),
            # your notes. The coordinator gets its own role's rules instead of the house rules.
            b, rules, _ = c.session.briefing or (self.settings.briefing(), self.cfg.house_rules, "")
            worker_live = self._live_briefing(b, worker=profile.name, c=c, job=w) if b["enabled"] else ""
            worker.system = briefing_mod.compose(rules, b, worker_live)
            # Claude Code on your plan as the worker: its own process, whose tool calls name this job.
            worker.bridge_key = w.key
            w.session = worker
            self._share_access()
            failed = None
            try:
                w.reason = worker.run_task(instructions)
            except ProviderError as e:
                failed = str(e)
                w.reason = "failed"
            finally:
                w.session = None
                self._share_access()
                worker.close()   # each job starts fresh, so a Claude Code worker doesn't linger
                self._end_job(c, w, log)
            w.report = worker.last_text or "(it gave no final report)"
            return self._job_result(c, w, log, failed)
        finally:
            if slot is not None:
                self._free_job_slot(slot)
            c.job_room.release()

    def _job_result(self, c: Conversation, w: Worker, log, failed: str | None) -> tuple[str, bool]:
        head = (f"Job {w.id}: the worker ({w.profile.label}) " +
                (f"failed: {failed}" if failed else f"ended: {w.reason}.\n\nIts final report:\n{w.report}"))
        try:
            plan = self.copies.job_plan(w.id, c.copy_dir)
        except (SyncError, OSError) as e:
            log.event("job_done", id=w.id, changes=[], conflicts=[], error=str(e))
            return (f"{head}\n\nIts changes could not be compared with your folder ({e}). Its folder is "
                    f"{w.copy_dir}; discard_worker drops it."), True
        changes = [f"{x['path']} ({x['change']})" for x in plan["changed"] + plan["merged"]]
        conflicts = [f"{x['path']} ({x['kind']})" for x in plan["conflicts"]]
        log.event("job_done", id=w.id, changes=changes, conflicts=conflicts)
        if not changes and not conflicts:
            self._drop_job(c, w.id)
            tail = "It changed no files, so there is nothing to bring in."
        else:
            listed = ", ".join(changes + conflicts)
            tail = (f"Its changes are in its own folder, {w.copy_dir}, not in yours yet: {listed}. "
                    f"Check them there with read_file if you need to, then bring them in with bring_in_worker "
                    f'(job "{w.id}"), or drop them with discard_worker.')
            if conflicts:
                tail += (" Some of them were also changed in your folder since the job started (another job "
                         "brought in): bring_in_worker will ask you which version to keep.")
        return f"{head}\n\n{tail}", bool(failed) or w.reason != "done"

    def _end_job(self, c: Conversation, w: Worker, log) -> None:
        """Nothing of the job keeps running, and its folder can't change any more: its user does
        the next job."""
        kill_slice(w.slice)
        if not no_sudo():
            root(["pkill", "-KILL", "-u", w.user], timeout=15)
        try:   # its user's home folder and /tmp files: the next job with this user starts clean
            left = clean_user(self.cfg, w.user)
        except Exception as e:   # noqa: BLE001
            left = [str(e)]
        if c.private:
            err = audit_quiet(w.user, False)
            left += [f"the audit rules for {w.user} ({err})"] if err else []
            if left:
                self.private_left += [f"A private conversation's worker: {x}" for x in left]
                self._broadcast({"kind": "private"})
        elif left:
            log.event("notice", text=f"The worker's leftovers could not all be removed: {'; '.join(left)}")
        if w.proxy is not None:
            w.proxy.stop()
            w.proxy = None
        w.task_sites.clear()
        try:
            self.copies.freeze_job(w.id, reader=c.user)
        except (SyncError, OSError) as e:
            log.event("notice", text=f"The job's folder could not be locked: {e}")

    def _start_job_proxy(self, c: Conversation, w: Worker) -> None:
        try:
            w.proxy = Proxy(w.port, self.cfg.net_ports, self.sites,
                            lambda host, port, c=c, w=w: self.decide_site(c, host, port, w),
                            lambda info, c=c, w=w: self._on_net(c, info, w), offline=c.offline).start()
            w.tools.executor.proxy_port = w.proxy.port
        except OSError as e:
            c.log.event("notice", text=f"A worker's proxy could not start ({e}), so it has no internet.")

    def _job_pool_missing(self) -> str:
        """Setup makes the workers' users after the conversations'; an older setup doesn't."""
        if no_sudo():
            return ""
        import pwd
        first = self.cfg.conv_max_open + 1
        for i in range(first, first + self.cfg.delegate_slots):
            try:
                pwd.getpwnam(f"{self.cfg.conv_user_prefix}{i}")
            except KeyError:
                return f"{self.cfg.conv_user_prefix}{i}"
        return ""

    def _take_job_slot(self, c: Conversation) -> int | None:
        with self.job_slot_cond:
            while not self.job_slots:
                if c.cancel.is_set():
                    return None
                self.job_slot_cond.wait(0.5)
            return self.job_slots.pop(0)

    def _free_job_slot(self, slot: int) -> None:
        with self.job_slot_cond:
            self.job_slots.append(slot)
            self.job_slots.sort()
            self.job_slot_cond.notify_all()

    def _drop_job(self, c: Conversation, jid: str) -> None:
        c.jobs.pop(jid, None)
        try:
            self.copies.remove(jid)
        except (SyncError, OSError) as e:
            print(f"Warning: could not remove the folder of job {jid}: {e}")

    def _waiting_jobs(self, c: Conversation) -> str:
        done = [w.id for w in c.jobs.values() if w.session is None]
        return f"Jobs waiting to be brought in: {', '.join(done)}." if done else "No job is waiting to be brought in."

    def _bring_in_job(self, c: Conversation, args: dict, call_id: str) -> tuple[str, bool]:
        """A finished job's changes into the coordinator's folder, the three-way way."""
        jid = str(args.get("job", "")).strip()
        w = c.jobs.get(jid)
        if w is None:
            return f"There is no job {jid!r}. {self._waiting_jobs(c)}", True
        if w.session is not None:
            return f"Job {jid} is still working; bring it in when it has reported.", True
        raw = args.get("choices")
        choices = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        bad = sorted(k for k, v in choices.items() if v not in ("worker", "mine"))
        if bad:
            return f'Choices are "worker" or "mine"; not for: {", ".join(bad)}. Nothing was brought in.', True
        try:
            r = self.copies.bring_in_job(jid, c.copy_dir, c.user, choices)
        except (SyncError, OSError) as e:
            return f"Bringing it in failed: {e}", True
        c.log.event("job_brought_in", id=jid, call=call_id, **r)
        parts = []
        if r["brought_in"]:
            parts.append(f"brought in: {', '.join(r['brought_in'])}")
        if r["merged"]:
            parts.append(f"merged line by line with your changes: {', '.join(r['merged'])}")
        if r["kept"]:
            parts.append(f"kept your version: {', '.join(r['kept'])}")
        text = (f"Job {jid}: " + "; ".join(parts) + ".") if parts else f"Job {jid}: nothing to bring in."
        if r["open_conflicts"]:
            # Its versions go next to yours (.uvmc-merge/), where a new worker finds them too:
            # no agent can open another's folder.
            placed = []
            for rel in r["open_conflicts"]:
                src = Path(w.copy_dir) / rel
                try:
                    data = src.read_bytes() if src.is_file() else None
                except OSError:
                    data = None
                if data is not None:
                    try:
                        self.copies._write(Path(c.copy_dir) / MERGE_DIR / (rel + ".worker"), data, c.user)
                        placed.append(f"{MERGE_DIR}/{rel}.worker")
                    except (SyncError, OSError):
                        pass
            where = (f"Its versions are in your folder: {', '.join(placed)}." if placed
                     else f"Its versions are in {w.copy_dir}.")
            text += (f" Still open, changed both in its folder and in yours: {', '.join(r['open_conflicts'])}. "
                     f"{where} Call bring_in_worker again with choices for each of "
                     'these files ("worker" or "mine"); or delegate combining them (a new worker gets the '
                     f"{MERGE_DIR} folder too), bring that job in, and then call discard_worker for this one.")
            return text, False
        self._drop_job(c, jid)
        return text + " Its folder is removed.", False

    def _discard_job(self, c: Conversation, args: dict, call_id: str) -> tuple[str, bool]:
        jid = str(args.get("job", "")).strip()
        w = c.jobs.get(jid)
        if w is None:
            return f"There is no job {jid!r}. {self._waiting_jobs(c)}", True
        if w.session is not None:
            return f"Job {jid} is still working.", True
        self._drop_job(c, jid)
        c.log.event("job_discarded", id=jid, call=call_id)
        return f"Job {jid} is dropped: none of its changes are in your folder, and its folder is removed.", False

    def _active_call(self, c: Conversation, w: Worker | None = None) -> str | None:
        """The tool call running right now in a conversation, or in one of its jobs."""
        if w is not None:
            return w.session.current_call if w.session is not None else None
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
            "private": self.private_view(),
            "lock": self.vm_lock.view(),
            "context": self.context_view(c),
        }

    # --- compacting (compact.py) ---------------------------------------------------

    def context_view(self, c: Conversation) -> dict:
        """How full the conversation's context is, and whether it can be compacted (the model on the PC)."""
        from . import pc
        p = self.cfg.models.get(c.model)
        on_pc = bool(p and pc.PLACEHOLDER in p.base_url and p.provider != "claude-code")
        return {"tokens": c.session.context_tokens, "estimated": c.session.context_estimated,
                "size": self.pc_context if on_pc else 0, "compact": on_pc,
                "can_trim": on_pc and bool(c.session.messages)}

    def compact(self, cid: str, mode: str, check: bool = False) -> dict:
        """Compacts a conversation's history, when you ask (never by itself). "trim": long tool
        outputs shortened, at once. "summary": the model writes a summary that replaces the history,
        in the background (the conversation shows as working; Stop leaves it as it was)."""
        from . import compact as compact_mod, pc
        c = self.conv(cid)
        p = self.cfg.profile(c.model)
        if pc.PLACEHOLDER not in p.base_url or p.provider == "claude-code":
            raise ValueError("Compacting is for the model on your PC; other models keep their own context.")
        if mode not in ("trim", "summary"):
            raise ValueError("mode is trim or summary.")
        with self.lock:
            if c.busy:
                raise Busy("Compact when this conversation isn't working.")
            if not c.session.messages:
                raise ValueError("There's nothing to compact yet.")
            if mode == "trim":
                before = compact_mod.estimate(c.session)
                r = compact_mod.trim(c.session)
                c.log.event("compact", mode="trim", shortened=r["shortened"], before=before,
                            after=compact_mod.estimate(c.session))
                self._broadcast({"kind": "conversations", "conv": c.id})
                return r
            if not self.model_allowed(p):
                raise Busy(self._locked_text(p))
            c.cancel.clear()
            self._set_state(c, "running")
            c.thread = threading.Thread(target=self._compact_run, args=(c, p, check), daemon=True)
            c.thread.start()
        self._broadcast({"kind": "conversations", "conv": c.id})
        return {"started": True}

    def _compact_run(self, c: Conversation, p: Profile, check: bool) -> None:
        from . import compact as compact_mod
        try:
            client = self.provider_factory(p)
            if client is None:
                raise ProviderError(f"No key for {p.label}: run local-model on your PC.")
            c.session.provider = client
            try:
                self.local_model()   # the context size, if the PC answers
            except Exception:
                pass
            before = compact_mod.estimate(c.session)
            summary = compact_mod.summarize(c.session, self.pc_context, check)
            if c.cancel.is_set():
                raise KeyboardInterrupt
            compact_mod.apply_summary(c.session, summary)
            c.log.event("compact", mode="summary", before=before, after=compact_mod.estimate(c.session),
                        summary=summary, checked=check)
        except KeyboardInterrupt:
            c.log.event("notice", text="Compacting stopped: the conversation is as it was.")
        except Exception as e:
            c.log.event("error", text=f"Compacting failed ({type(e).__name__}: {e}). The conversation is as it was.")
        finally:
            c.cancel.clear()
            self._set_state(c, "idle")
            self._broadcast({"kind": "conversations", "conv": c.id})

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
            profile = self.cfg.profile(c.model) if c.model in self.cfg.models else None
            if profile is not None and not self.model_allowed(profile):
                raise Busy(self._locked_text(profile))
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
            if not self.model_allowed(profile):
                c.log.event("error", text=self._locked_text(profile))
                return
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
                     origin: str = "api", projects: list[str] | None = None, level: str | None = None) -> dict:
        """Runs one task and returns when it ends: for scripts (POST /api/run). With new, in a
        conversation of its own, next to the ones in the panel (projects, level: as for a new
        conversation)."""
        if new:
            c = self.new_conversation(model, origin=origin, projects=projects, level=level)
        else:
            c = self.conv(cid)
            if c.private:
                raise Busy("A private conversation takes its tasks from the panel only.")
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
        for w in c.running_jobs():
            kill_slice(w.slice)
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
        self._claim_new_projects(c)
        plan = self.copies.plan(c.id, self.scope(c))
        origins = self._load_json("main_origins.json") or {}
        for x in plan["conflicts"]:
            # The conversation whose version is in the main folder, if it's open: it can resolve it instead.
            o = self.convs.get((origins.get(x["path"]) or {}).get("conv", ""))
            if o is not None and o is not c and self.copies.exists(o.id) and self._may_hand(c, o, x["path"]):
                x["other"] = {"id": o.id, "title": o.title or "untitled",
                              "model_label": self.cfg.models[o.model].label if o.model in self.cfg.models else o.model}
        for x in plan.get("handed", []):
            o = self.convs.get(x["conv"])
            x["title"] = (o.title or "untitled") if o else "another conversation"
        return {**plan, "conv": c.id, "copy": c.copy_dir, "main": self.cfg.workdir, "level": c.level,
                "seen": c.seen, "projects": [{"name": n, "label": self._project_label(n)} for n in c.projects]}

    def sync(self, conv: str | None, choices: dict | None = None) -> dict:
        """Brings a conversation's work into the main folder and the main folder's into its copy.
        Lines changed on one side only merge by themselves. A real conflict goes to an agent,
        which resolves it with the context (both versions, what each conversation was doing, the
        folders), the way Claude Code resolves a git conflict; you bring its result in like any
        other change. choices: path -> "agent" (this conversation's agent: the default),
        "agent:<id>" (another open conversation's agent, e.g. the one whose version is in the
        main folder), "mine", "main" or "later". For a held change (it would put what this
        conversation has seen somewhere less private): "release" brings it in; without it, it
        stays held."""
        choices = dict(choices or {})
        c = self.conv(conv)
        self._check_sealed(c)
        release = {p for p, v in choices.items() if v == "release"}
        with self.lock:
            if c.busy:
                raise Busy("Wait until this conversation's task ends.")
            self._claim_new_projects(c)
            scope = self.scope(c)
            plan = self.copies.plan(c.id, scope, release)
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
                self._check_handover(c, o, p)
                others.setdefault(o.id, []).append(p)
            fixed = {p: v for p, v in choices.items() if v in ("mine", "main")}
            with self.copy_lock:
                r = self.copies.sync(c.id, c.user, fixed, scope=scope, release=release)
                handed = {oid: self.copies.prepare_other_merge(c.id, oid, self.convs[oid].user, paths)
                          for oid, paths in others.items()}
            self._note_seen(c)
            self._save_meta(c)
        n_in = len(r["brought_in"]) + len(r["merged"])
        r["agent_merging"] = own
        r["other_merging"] = [{"conv": oid, "title": self._name_for_others(self.convs[oid]), "paths": paths}
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
                other.log.event("notice", text=f"Another conversation ({self._name_for_others(c)}) brought {n_in} "
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

    def _may_hand(self, c: Conversation, o: Conversation, path: str) -> bool:
        try:
            self._check_handover(c, o, path)
            return True
        except PrivacyError:
            return False

    def _check_handover(self, c: Conversation, o: Conversation, path: str) -> None:
        """Another conversation resolving c's conflict gets c's version and c's whole transcript:
        only if it may see everything c has seen, and the file itself."""
        if privacy.rank(o.level) < privacy.rank(c.seen):
            raise PrivacyError(f"\"{o.title or o.id}\" is {o.level}, and this conversation has seen {c.seen} files: "
                               "it can't resolve this conflict (it would get them with the transcript).")
        if not self.scope(o).visible(path):
            raise PrivacyError(f"\"{o.title or o.id}\" may not see {path}.")

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
            if (src is not None and src is not c and not f.get("other_conv") and self.copies.exists(src.id)
                    and self._may_hand(c, src, f["path"])):
                f["reassign"] = {"id": src.id, "title": src.title or "untitled",
                                 "model_label": self.cfg.models[src.model].label if src.model in self.cfg.models else src.model}
        return {"conv": c.id, "files": files, "explanation": c.session.last_text, "state": c.state}

    def resolve(self, conv: str | None, choices: dict) -> dict:
        """Your answer to a proposed resolution: per file "accept", "yours", "main", "other",
        "later", or "agent:<id>" (let that conversation resolve it instead)."""
        c = self.conv(conv)
        self._check_sealed(c)
        with self.lock:
            if c.busy:
                raise Busy("Wait until this conversation's task ends.")
        reassign = {p: v for p, v in choices.items() if str(v).startswith("agent:")}
        plain = {p: ("later" if p in reassign else v) for p, v in choices.items()}
        with self.copy_lock:
            r = self.copies.resolve(c.id, c.user, plain, self.scope(c))
        if r.get("brought_in"):
            origins = self._load_json("main_origins.json") or {}
            for path in r["brought_in"]:
                origins[path] = self._about(c)
            self._save_json("main_origins.json", origins)
        c.log.event("resolved", choices=choices, brought_in=r.get("brought_in", []), undo=r.get("undo"),
                    reopened=[p for p in r.get("reopened", []) if p not in reassign])
        self._broadcast({"kind": "shared_folder", "conv": c.id})
        if reassign:   # the reopened conflicts go to the conversation you chose
            open_ = [x["path"] for x in self.copies.plan(c.id, self.scope(c))["conflicts"]]
            self.sync(c.id, {p: reassign.get(p, "later") for p in open_})
        return r

    def _about(self, c: Conversation) -> dict:
        """What a conversation was doing, for an agent resolving a conflict with its work. Of a
        private one: nothing but that it was one (no tasks, no answer, no log)."""
        if c.private:
            return {"conv": "private", "title": "a private conversation", "tasks": [], "last_reply": "",
                    "log": None, "at": time.time(), "seen": "local", "private": True}
        tasks = [x.get("text", "") for x in c.log.read() if x.get("kind") == "task" and not x.get("parent")]
        return {"conv": c.id, "title": c.title, "tasks": [t[:300] for t in tasks[-5:]],
                "last_reply": (c.session.last_text or "")[:600], "log": c.log.name, "at": time.time(),
                "seen": c.seen}

    def _hand_transcript(self, to: Conversation, about: dict, folder: str = MERGE_DIR) -> str | None:
        """Writes the other conversation's log, as a readable transcript, into the resolving
        conversation's .uvmc-merge/ (from the log UVMC keeps, so it works after a close too), or
        another folder of its copy. Returns its path relative to the conversation's folder."""
        name = about.get("log")
        if not name:
            return None
        try:
            records = self.history_events(name)
        except KeyError:
            return None
        title = about.get("title") or "untitled"
        slug = "-".join("".join(ch if ch.isalnum() else " " for ch in title.lower()).split())[:40] or name
        rel = f"{folder}/conversation-{slug}.md"
        try:
            self.copies._write(Path(to.copy_dir) / rel, transcript(records, title).encode(), to.user)
        except (SyncError, OSError):
            return None
        return rel

    @staticmethod
    def _describe(about: dict) -> str:
        if about.get("private"):
            return "a private conversation (UVMC keeps nothing about what it was for)."
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
                + "Its version and the common one are in .uvmc-merge/ (you can't open its folder or the main "
                "folder directly).\n\nFor each file, work out what both "
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
        open_ = {x["path"] for x in self.copies.plan(c.id, self.scope(c))["conflicts"]}
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
            if o and privacy.rank(c.level) < privacy.rank(o.get("seen") or "local"):
                o = None   # it saw more private files than this conversation may: nothing of it is told
                line += " The main folder's version came from another conversation."
            if o:   # what the other side was for
                line += " The main folder's version came from " + self._describe(o)
                # Its whole log only if this conversation may see everything that one could.
                if o.get("log") not in written:
                    written[o.get("log")] = (self._hand_transcript(c, o) if privacy.rank(c.level)
                                             >= privacy.rank(o.get("seen") or "local") else None)
                if written.get(o.get("log")):
                    line += f" Its whole conversation (what it was asked, its answers and steps) is in {written[o.get('log')]}."
            lines.append(line)
        task = ("These files were changed both here and in the main work folder, by another conversation. "
                "Resolve each one like a merge conflict: first work out what each side was doing (your own tasks "
                "in this conversation, the other conversation's below, and the files around them; the other "
                "versions are in .uvmc-merge/), then make your file "
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
                self.copies._snapshot_base(c.id, scope=self.scope(c))
        self._broadcast({"kind": "shared_folder", "conv": conv})
        if c is not None:
            c.log.event("notice", text="The main folder is back to how it was before that bring-in. "
                                       "This conversation's copy keeps its changes; bring them in again if you want.")

    # --- approvals -----------------------------------------------------------

    def _ask(self, kind: str, info: dict, convs: list[Conversation], timeout: float | None = None,
             choice: dict | None = None) -> str:
        """Shows an approval in the conversations it concerns and waits. Returns "allow",
        "always" or "deny". choice: filled with what you chose on the card besides the answer
        (a tool's level)."""
        pid = uuid.uuid4().hex[:12]
        if any(c.private for c in convs):   # the card says what would stay after it's erased
            info = {**info, "private": True}
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
        if choice is not None:
            choice.update(entry.get("choice") or {})
        for c in convs:
            c.log.event("approval_done", id=pid, type=kind, call=info.get("call"), answer=answer,
                        allow=answer != "deny", timed_out=timed_out)
            self._refresh_state(c)
        self._broadcast({"kind": "conversations"})
        return answer

    def _approve(self, c: Conversation, tool: str, args: dict, reason: str, call_id: str = "") -> bool:
        return self._ask("tool", {"tool": tool, "args": args, "reason": reason, "call": call_id}, [c]) != "deny"

    def decide(self, pid: str, answer: str, level: str | None = None) -> None:
        """level: for a tool the agent built, the privacy level you approve it at."""
        entry = self.pending.get(pid)
        if entry is None:
            raise KeyError("That request is no longer waiting.")
        if answer not in ("allow", "always", "deny"):
            raise ValueError("Answer must be allow, always or deny.")
        if level and entry["type"] == "made_tool":
            entry["choice"] = {"level": privacy.check(level)}
        if answer == "always" and (entry["type"] != "site" or entry.get("always") is False):
            answer = "allow"   # a conversation that has seen private files: asked again next time
        entry["answer"] = answer
        entry["event"].set()

    # --- network -------------------------------------------------------------
    # Each conversation has its own proxy, and the firewall lets its user reach only that one,
    # so every connection and every question about a new site belongs to one conversation.

    def decide_site(self, c: Conversation, host: str, port: int, w: Worker | None = None) -> bool:
        """Called by a conversation's proxy (or one of its jobs') for a site that is not on the
        allowed list. The question appears in the conversation, on the step that asked."""
        if not c.busy or c.cancel.is_set() or (w is not None and w.session is None):
            return False  # nobody is watching a task here, so nothing new goes out
        at = w or c
        key = f"{at.id}/{host}"
        with self.site_lock:
            if host in at.task_sites:
                return at.task_sites[host]
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
                # A conversation that has seen paid or local files could send them out through the
                # site: you're asked each time (no "always"), and told what it has seen.
                private = privacy.rank(c.seen) > privacy.rank("open") or c.private
                reason = "This site is not on your allowed list."
                if private:
                    reason += (f" This conversation has seen {c.seen} files, which could go out through it: "
                               "allow it only if you trust the site with them.")
                answer = self._ask("site", {"host": host, "port": port, "call": self._active_call(c, w),
                                            "reason": reason, "seen": c.seen, "always": not private},
                                   [c], timeout=self.cfg.net_approval_timeout_s)
                if answer == "always":
                    self.sites.add(host)
                result = answer != "deny"
            at.task_sites[host] = result
        finally:
            wait["result"] = result
            wait["event"].set()
            with self.site_lock:
                self.site_waits.pop(key, None)
        return result

    def _on_net(self, c: Conversation, info: dict, w: Worker | None = None) -> None:
        info = {**info, "call": self._active_call(c, w)}
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

    def _conv_of_process(self, info: dict) -> tuple[Conversation | None, Worker | None]:
        """Which conversation (and which of its jobs) started a program: from its box, else the
        only one working."""
        for pid in (info.get("pid"), info.get("ppid")):
            name = slice_of_pid(pid)
            if name:
                for c in list(self.convs.values()):
                    if c.slice == name:
                        return c, None
                    w = next((w for w in c.running_jobs() if w.slice == name), None)
                    if w is not None:
                        return c, w
                return None, None
        working = self.running()
        if len(working) == 1:
            jobs = working[0].running_jobs()
            return working[0], (jobs[0] if len(jobs) == 1 else None)
        return None, None

    def _on_exec(self, info: dict) -> None:
        if not self.exec_filter.keep(info):
            return
        c, w = self._conv_of_process(info)
        info = {**info, "call": self._active_call(c, w) if c else None}
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
            conv = next((c for c in self.convs.values() if user != self.cfg.user
                         and (c.user == user or any(w.user == user for w in c.running_jobs()))), None)
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
        out = {"path": lines[0] if lines else path, "entries": entries, "where": self._where(conv, where)}
        self._add_levels(out, conv)
        return out

    def _add_levels(self, out: dict, conv: str | None) -> None:
        """Each entry's privacy level, and whether this conversation may see it, when the listing
        is inside the main folder or a conversation's copy (the same paths in both)."""
        c = self.convs.get(conv or "") if conv else None
        top = c.copy_dir if (c is not None and out["where"] == "copy") else self.cfg.workdir
        here = os.path.normpath(out["path"])
        top = os.path.normpath(top)
        if here != top and not here.startswith(top + os.sep):
            return
        rel_dir = os.path.relpath(here, top).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        if rel_dir.split("/", 1)[0] in (MERGE_DIR, CONTEXT_DIR):
            return
        scope = self.scope(c) if c is not None else None
        zones, marks = self.projects.data["zones"], self.projects.data["marks"]
        for e in out["entries"]:
            rel = f"{rel_dir}/{e['name']}" if rel_dir else e["name"]
            if e["name"] in (MERGE_DIR, CONTEXT_DIR) and not rel_dir:
                continue
            e["rel"] = rel
            e["level"] = self.projects.level(rel)
            e["zone"] = zones.get(rel)
            e["mark"] = marks.get(rel)
            if scope is not None:
                e["hidden"] = not scope.visible(rel)
                e["given"] = rel in c.given
        info = {"rel": rel_dir, "project": self.projects.project_of(rel_dir) if rel_dir else None}
        if rel_dir:
            info.update(level=self.projects.level(rel_dir), zone=zones.get(rel_dir),
                        project_label=self._project_label(info["project"]),
                        project_level=self.projects.project_level(info["project"]))
        out["privacy"] = info

    def read_file(self, path: str, limit: int = 200_000, conv: str | None = None, where: str = "copy") -> dict:
        p = self._files_as(conv, where)._as_agent(': uvmc-panel; wc -c < "$1" && head -c "$2" -- "$1"', path, str(limit), timeout=15)
        if p.returncode != 0:
            raise FileNotFoundError(p.stderr.decode(errors="replace").strip() or f"Cannot read {path}")
        size_line, _, data = p.stdout.partition(b"\n")
        size = int(size_line.strip() or 0)
        info = self._file_level(path, conv, where)
        if b"\x00" in data[:8000]:
            return {"path": path, "size": size, "binary": True, "text": "", **info}
        return {"path": path, "size": size, "binary": False, "truncated": size > limit,
                "text": data.decode("utf-8", errors="replace"), **info}

    def _file_level(self, path: str, conv: str | None, where: str) -> dict:
        """A file's level, mark, and whether the conversation may see it (inside the main folder
        or its copy only)."""
        c = self.convs.get(conv or "") if conv else None
        where_now = self._where(conv, where)
        top = os.path.normpath(c.copy_dir if (c is not None and where_now == "copy") else self.cfg.workdir)
        full = os.path.normpath(path if os.path.isabs(path) else os.path.join(top, path))
        if not full.startswith(top + os.sep):
            return {}
        rel = os.path.relpath(full, top).replace(os.sep, "/")
        if rel.split("/", 1)[0] in (MERGE_DIR, CONTEXT_DIR):
            return {}
        out = {"rel": rel, "level": self.projects.level(rel), "mark": self.projects.data["marks"].get(rel),
               "around": self.projects.level(rel, with_mark=False), "where": where_now}
        if c is not None:
            out["hidden"] = not self.scope(c).visible(rel)
            out["given"] = rel in c.given
        return out

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

def signin_target(path: str | None) -> str:
    """Where the panel goes after taking ?token=: the same page (deep links work), but only ever a page
    of the panel. Browsers drop tabs and newlines from addresses and read "//host" or "/\\host" as
    another site, so those go and the leading slashes become one."""
    return "/" + (path or "/").translate({9: None, 10: None, 13: None}).lstrip("/\\")


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
            # A page that sets the cookie and moves on by itself, not a redirect: opened through the PC
            # helper's link (it starts at 127.0.0.1), the browser counts a whole redirect chain as
            # cross-site and withholds the Strict cookie at its end. The page's own step starts here,
            # same-site, so the cookie goes along. It keeps the part after "#" (the helper's key).
            # (Not only for Sec-Fetch-Site: cross-site: browsers send that header to HTTPS only.)
            target = signin_target(request.url.path)
            target_js = json.dumps(target).replace("<", "\\u003c")   # safe inside <script>
            target_attr = target.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
            resp = Response(
                '<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>UVMC</title>'
                f"<script>location.replace({target_js} + location.hash)</script>"
                f'<noscript><a href="{target_attr}">Open the UVMC panel</a></noscript>', media_type="text/html")
            resp.headers["Cache-Control"] = "no-store"
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

    # Privacy (privacy.py): refused outright, or allowed once you confirm (send "confirm": true).
    @app.exception_handler(PrivacyError)
    async def privacy_refused(request: Request, exc: PrivacyError):
        return JSONResponse({"detail": str(exc), "privacy": True}, 403)

    @app.exception_handler(NeedsConfirm)
    async def privacy_confirm(request: Request, exc: NeedsConfirm):
        return JSONResponse({"detail": str(exc), "confirm": True}, 409)

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
        """Opens a conversation and makes it the current one. Body, all optional: model;
        projects (names, "" for General; default: General if allowed, else a scratch project);
        level (local, paid or open: its model's or lower); like (a conversation id: the same
        projects and level as that one, "New conversation on the same projects"); transcript
        (with like: it starts with that conversation's whole transcript); private (true: a private
        conversation, private.py); private_options (with private: offline, pc_only, sealed, each
        true unless given false)."""
        body = await request.json()
        projects = body.get("projects")
        level = body.get("level") or None
        try:
            if body.get("like"):
                other = ctl.conv(str(body["like"]))
                projects = list(other.projects) if projects is None else projects
                level = level or other.level
            if projects is not None and not (isinstance(projects, list) and all(isinstance(p, str) for p in projects)):
                fail(400, "projects is a list of project names.")
            transcript_from = str(body["like"]) if body.get("like") and body.get("transcript") else None
            c = ctl.new_conversation(body.get("model") or None, projects=projects, level=level,
                                     transcript_from=transcript_from, private=bool(body.get("private")),
                                     private_opts=body.get("private_options") if isinstance(body.get("private_options"), dict) else None)
        except Busy as e:
            fail(409, str(e))
        except KeyError as e:
            fail(404, str(e.args[0]))
        except ValueError as e:
            fail(400, str(e))
        return ctl.snapshot(c.id)

    @app.post("/api/conversations/project")
    async def conversation_project(request: Request):
        """Adds a project to a conversation (body: conv, name)."""
        body = await request.json()
        c = conv_or_404(body.get("conv"))
        try:
            return await run_in_threadpool(ctl.open_project, c.id, str(body.get("name", "")))
        except Busy as e:
            fail(409, str(e))
        except KeyError as e:
            fail(404, str(e.args[0]))
        except ValueError as e:
            fail(400, str(e))

    @app.post("/api/conversations/give")
    async def conversation_give(request: Request):
        """Gives a conversation one file it may not see (body: conv, path in the main folder,
        confirm). The file keeps its level."""
        body = await request.json()
        c = conv_or_404(body.get("conv"))
        try:
            return await run_in_threadpool(ctl.give_file, c.id, str(body.get("path", "")), bool(body.get("confirm")))
        except Busy as e:
            fail(409, str(e))
        except KeyError as e:
            fail(404, str(e.args[0]))
        except ValueError as e:
            fail(400, str(e))
        except SyncError as e:
            fail(500, str(e))

    @app.get("/api/conversations/{cid}/held")
    async def conversation_held(cid: str, path: str):
        """A change held for your review (it would move what the conversation has seen to a less
        private place), against the main folder's version."""
        conv_or_404(cid)
        try:
            return await run_in_threadpool(ctl.held_diff, cid, path)
        except KeyError as e:
            fail(404, str(e.args[0]))

    # --- projects and privacy levels -----------------------------------------------------------
    @app.get("/api/projects")
    def projects_view():
        """The projects, their levels, zones and marks, and each model's level."""
        return ctl.projects_view()

    def _level_arg(body: dict, allow_none: bool = False):
        lvl = body.get("level")
        if lvl in (None, "") and allow_none:
            return None
        if lvl not in privacy.RANK:
            fail(400, f"level must be one of: {', '.join(privacy.LEVELS)}.")
        return lvl

    @app.post("/api/projects")
    async def project_add(request: Request):
        """A new, empty project (body: name; level, default the current conversation's model's)."""
        body = await request.json()
        try:
            return ctl.add_project(str(body.get("name", "")), _level_arg(body, True) or "", body.get("conv") or None)
        except (ValueError, KeyError) as e:
            fail(400, str(e.args[0] if e.args else e))
        except SyncError as e:
            fail(500, str(e))

    @app.post("/api/projects/claim")
    async def project_claim(request: Request):
        """A folder in General becomes a project of its own (body: name, level, confirm)."""
        body = await request.json()
        try:
            return ctl.claim_project(str(body.get("name", "")), _level_arg(body), bool(body.get("confirm")))
        except KeyError as e:
            fail(404, str(e.args[0]))
        except ValueError as e:
            fail(400, str(e))

    @app.post("/api/projects/level")
    async def project_level(request: Request):
        """A project's level (body: name, "" for General; level; confirm, needed to lower it)."""
        body = await request.json()
        try:
            return ctl.set_project_level(str(body.get("name", "")), _level_arg(body), bool(body.get("confirm")))
        except KeyError as e:
            fail(404, str(e.args[0]))

    @app.post("/api/projects/zone")
    async def project_zone(request: Request):
        """A folder with its own level (body: path in the main folder; level, or null to drop it; confirm)."""
        body = await request.json()
        try:
            return ctl.set_zone(str(body.get("path", "")), _level_arg(body, True), bool(body.get("confirm")))
        except ValueError as e:
            fail(400, str(e))

    @app.post("/api/projects/mark")
    async def project_mark(request: Request):
        """A file marked stricter than where it is (body: path; level, "local" for sensitive, or null; confirm)."""
        body = await request.json()
        try:
            return ctl.set_mark(str(body.get("path", "")), _level_arg(body, True), bool(body.get("confirm")))
        except ValueError as e:
            fail(400, str(e))

    @app.post("/api/conversations/close")
    async def conversation_close(request: Request):
        """Closes a conversation. One with changes not brought into the main folder closes only
        with "discard": true (409 with "unsaved": true otherwise); a private one only with
        "erase": true (409 with "confirm": true otherwise, the question to ask)."""
        body = await request.json()
        try:
            ctl.close_conversation(str(body.get("conv", "")), bool(body.get("discard")), bool(body.get("erase")))
        except Unsaved as e:
            return JSONResponse({"error": str(e), "unsaved": True, "sealed": e.sealed}, 409)
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

    @app.post("/api/conversations/keep")
    async def conversation_keep(request: Request):
        """A private conversation that keeps its files to itself: you chose to keep them, so it may
        now bring them into the main folder (then you close it to erase the rest)."""
        body = await request.json()
        try:
            ctl.keep_files(str(body.get("conv", "")))
        except KeyError as e:
            fail(404, str(e.args[0]))
        except ValueError as e:
            fail(400, str(e))
        return {"ok": True}

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
        need approval instead of waiting for you in the panel); with new, projects and level (as
        for POST /api/conversations). Returns the final reply, how it ended, every tool step,
        approvals, errors and files changed in shared folders."""
        body = await request.json()
        text = str(body.get("text", "")).strip()
        if not text:
            fail(400, "The task is empty.")
        try:
            timeout = max(10.0, min(float(body.get("timeout", 1800)), 7200.0))
        except (TypeError, ValueError):
            fail(400, "timeout must be a number of seconds.")
        try:
            projects = body.get("projects")
            if projects is not None and not (isinstance(projects, list) and all(isinstance(p, str) for p in projects)):
                fail(400, "projects is a list of project names.")
            return await run_in_threadpool(ctl.run_and_wait, text, body.get("model") or None,
                                           bool(body.get("new")), timeout, bool(body.get("no_ask")),
                                           body.get("conv") or None, "api", projects, body.get("level") or None)
        except Busy as e:
            fail(409, str(e))
        except KeyError as e:
            fail(400, str(e.args[0] if e.args else e))

    @app.get("/api/lock")
    def lock_view():
        return ctl.vm_lock.view()

    @app.post("/api/lock")
    async def lock_set(request: Request):
        """Locks ({"on": true, "pin": ...}) or unlocks ({"on": false, "pin": ...}) the VM: closed to
        every program on the PC, only the model on the PC used (lock.py). The first lock sets the
        PIN. 403 for a wrong PIN or while wrong tries are slowed down."""
        body = await request.json()
        try:
            return await run_in_threadpool(ctl.set_lock, bool(body.get("on")), str(body.get("pin") or ""))
        except LockError as e:
            fail(403, str(e))

    @app.post("/api/lock/seen")
    def lock_seen():
        """The panel showed that a restart released the lock."""
        ctl.vm_lock.forget_restart()
        return {"ok": True}

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
            ctl.decide(str(body.get("id", "")), str(answer), str(body.get("level") or "") or None)
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

    @app.post("/api/local-model")
    async def set_local_model(request: Request):
        body = await request.json()
        return settings_call(ctl.set_local_model, str(body.get("model", "") if isinstance(body, dict) else ""))

    @app.post("/api/model")
    async def model(request: Request):
        body = await request.json()
        c = conv_or_404(body.get("conv"))
        try:   # to a less safe model: 409 with "confirm" until you send confirm: true (or 403)
            ctl.set_model(str(body.get("name", "")), c.id, bool(body.get("confirm")))
        except Busy as e:
            fail(409, str(e))
        except KeyError as e:
            fail(404, str(e.args[0]))
        return ctl.snapshot(c.id)

    @app.post("/api/conversations/compact")
    async def conversation_compact(request: Request):
        body = await request.json()
        c = conv_or_404(body.get("conv"))
        try:
            r = await run_in_threadpool(ctl.compact, c.id, str(body.get("mode", "")), bool(body.get("check")))
        except Busy as e:
            fail(409, str(e))
        except ValueError as e:
            fail(400, str(e))
        return {**r, "context": ctl.context_view(c)}

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

    @app.get("/api/updates")
    async def updates_info():
        return {**ctl.updates.snapshot(), "enabled": ctl.cfg.updates_check}

    @app.post("/api/updates/check")
    async def updates_check():
        return {**(await run_in_threadpool(ctl.updates.check)), "enabled": ctl.cfg.updates_check}

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

    @app.post("/api/made-tools/notify")
    async def made_tool_notify(request: Request):
        body = await request.json()
        made_call(ctl.made.set_notify, str(body.get("name", "")), str(body.get("mode", "")))
        return {"ok": True}

    @app.post("/api/made-tools/level")
    async def made_tool_level(request: Request):
        body = await request.json()
        try:
            return made_call(ctl.set_tool_level, str(body.get("name", "")), str(body.get("level", "")),
                             bool(body.get("confirm")))
        except ValueError as e:
            fail(400, str(e))

    @app.post("/api/made-tools/seen")
    async def made_tools_seen(request: Request):
        body = await request.json()
        made_call(ctl.made.mark_seen, str(body.get("name", "")), str(body.get("id", "")) or None)
        return {"ok": True}

    @app.get("/api/made-tools/{name}")
    def made_tool(name: str):
        return made_call(ctl.made.view, name)

    @app.get("/api/made-tools/{name}/runs/{run_id}")
    def made_tool_log(name: str, run_id: str):
        return {"text": made_call(ctl.made.log, name, run_id)}

    @app.post("/api/made-tools/{name}/work")
    def made_tool_work(name: str):
        """Work on this tool: a new conversation with its code in its folder."""
        try:
            return made_call(ctl.work_on_tool, name)
        except Busy as e:
            fail(409, str(e))

    @app.get("/api/made-tools/{name}/runs/{run_id}/result")
    def made_tool_result(name: str, run_id: str):
        return made_call(ctl.made.result, name, run_id)

    @app.get("/api/made-tools/{name}/runs/{run_id}/images/{n}")
    def made_tool_image(name: str, run_id: str, n: int):
        data, kind = made_call(ctl.made.image, name, run_id, n)
        # Only ever drawn as a picture: its type comes from its bytes, and nothing in it may run.
        return Response(data, media_type=kind, headers={
            "Content-Security-Policy": "default-src 'none'; sandbox", "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=86400"})

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
