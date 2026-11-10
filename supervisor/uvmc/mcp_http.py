"""UVMC as a tool for the Claude apps on your PC (MCP), off until you switch it on in Settings.

Claude Code or Claude Desktop on your PC start `uvmc mcp` (a small relay in this repo), which
looks up the machine and its token through Multipass and passes MCP messages to POST /mcp here.
Your PC's assistant can then hand jobs to the agent in the VM. It gets no extra powers: the
task runs with the same rules, walls and logs, and anything that needs approval waits for
you in the panel. The PC's assistant can't approve anything.
"""

from __future__ import annotations

import posixpath
import time

from . import privacy

PROTOCOL = "2025-06-18"
APPS_LEVEL = "paid"   # the Claude apps on your PC: what they get back is what a paid model may see


def _private(c) -> str | None:
    """A conversation that has seen files the Claude apps may not: they get no reply from it."""
    if privacy.rank(c.seen) > privacy.rank(APPS_LEVEL):
        return (f"That conversation has seen {c.seen} files, so its replies stay in the UVMC panel: "
                f"the Claude apps on your PC are {APPS_LEVEL}. State: {c.state}.")
    return None

TOOLS = [
    {"name": "run_task",
     "description": ("Give a task to the AI agent in the UVMC machine (a sandboxed Ubuntu VM with its own "
                     "tools, files and internet access through approvals). Waits up to wait_seconds for it "
                     "to finish and returns its final reply and steps; if it is still running, use "
                     "task_status later. Anything risky waits for the user's approval in the UVMC panel."),
     "inputSchema": {"type": "object", "properties": {
         "task": {"type": "string", "description": "What the agent should do, self-contained."},
         "new_conversation": {"type": "boolean", "description": (
             "Run it in a new conversation of its own (default true). It shows up in the UVMC panel next "
             "to the user's own conversations. False continues the last one you used (or 'conversation').")},
         "conversation": {"type": "string", "description": "Continue this conversation (an id a result gave)."},
         "wait_seconds": {"type": "integer", "description": "How long to wait for the result (default 240, max 900)."}},
         "required": ["task"]}},
    {"name": "task_status",
     "description": ("What a UVMC conversation is doing now: working or idle, approvals waiting for the user, "
                     "its latest reply. Default: the last conversation you used."),
     "inputSchema": {"type": "object", "properties": {"conversation": {"type": "string"}}}},
    {"name": "stop_task",
     "description": "Stop the task a UVMC conversation is running. Default: the last conversation you used.",
     "inputSchema": {"type": "object", "properties": {"conversation": {"type": "string"}}}},
    {"name": "list_files",
     "description": ("List a folder in a UVMC conversation's work folder (relative paths start there). "
                     "Default: the last conversation you used."),
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string", "description": "Default: the work folder."},
                                                      "conversation": {"type": "string"}}}},
    {"name": "read_file",
     "description": ("Read a text file from a UVMC conversation's work folder, e.g. a result it produced. "
                     "Default: the last conversation you used."),
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "conversation": {"type": "string"}},
                     "required": ["path"]}},
]


def _text(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def _summary(ctl, c, after: int) -> str:
    records = [r for r in c.log.read() if r.get("seq", 0) > after]
    ends = [r for r in records if r["kind"] == "end" and not r.get("parent")]
    steps = [f"- {r.get('tool')}: {'error' if r.get('is_error') else 'ok'}" for r in records if r["kind"] == "result"]
    errors = [str(r.get("text")) for r in records if r["kind"] == "error" and not r.get("parent")]
    out = [f"Conversation {c.id}. " + (f"Ended: {ends[-1]['reason']}." if ends else "Still running.")]
    if c.session.last_text:
        out.append("Final reply from the UVMC agent:\n" + c.session.last_text)
    if steps:
        out.append("Steps:\n" + "\n".join(steps[-30:]))
    if errors:
        out.append("Errors:\n" + "\n".join(errors))
    return "\n\n".join(out)


def _status(ctl, c) -> str:
    pend = list(ctl.pending_public(c.id))
    lines = [f"Conversation {c.id}. State: {c.state}. Model: {ctl.cfg.profile(c.model).label}."]
    if pend:
        lines.append(f"{len(pend)} approval(s) waiting for the user in the UVMC panel: "
                     + "; ".join(str(p.get("tool") or p.get("host") or p.get("path") or p.get("type")) for p in pend))
    if c.session.last_text:
        lines.append("Latest reply:\n" + c.session.last_text)
    return "\n".join(lines)


def _conv(ctl, args: dict):
    """The conversation a call is about: the one named, else the last one the apps used."""
    cid = str(args.get("conversation") or ctl.mcp_conv or "")
    try:
        c = ctl.conv(cid or None)
    except KeyError:
        return None
    return None if c.private else c   # a private conversation is never shown to the Claude apps


def call(ctl, name: str, args: dict) -> dict:
    from .service import Busy   # here, to avoid a circular import
    if name == "run_task":
        task = str(args.get("task", "")).strip()
        if not task:
            return _text("The task is empty.", True)
        try:
            if args.get("new_conversation", True) and not args.get("conversation"):
                # At most paid: what it finds and says comes back to the Claude app.
                base = ctl.convs.get(ctl.current)   # the model a new conversation gets
                model = ctl.cfg.profile(base.model if base else None)
                level = privacy.lower(model.level, APPS_LEVEL)
                c = ctl.new_conversation(origin="claude-app", focus=False, level=level)
            else:
                c = _conv(ctl, args)
                if c is None:
                    return _text("That conversation is closed. Start a new one.", True)
            ctl.mcp_conv = c.id
            after = ctl.start_task(task, c.id)
        except Busy as e:
            return _text(f"UVMC is busy: {e} Use task_status, or stop_task.", True)
        try:
            wait = max(5, min(int(args.get("wait_seconds", 240)), 900))
        except (TypeError, ValueError):
            wait = 240
        end = time.time() + wait
        time.sleep(0.3)
        while c.busy and time.time() < end:
            time.sleep(0.5)
        if _private(c):
            return _text(_private(c))
        if c.busy:
            return _text(f"The task is still running after {wait} s.\n\n{_status(ctl, c)}\n\nCall task_status later.")
        return _text(_summary(ctl, c, after))
    if name in ("task_status", "stop_task"):
        c = _conv(ctl, args)
        if c is None:
            return _text("That conversation is closed.", True)
        if name == "task_status":
            return _text(_private(c) or _status(ctl, c))
        was = c.busy
        ctl.stop(c.id)
        return _text("Stopped." if was else "Nothing was running.")
    if name in ("list_files", "read_file"):
        c = _conv(ctl, args)
        if c is not None and not ctl.copies.exists(c.id):
            c = None   # no copy yet: the files come from the main folder
        conv = c.id if c is not None else None
        # The Claude apps on your PC are Claude: they see what a paid model may (privacy.py).
        if c is not None and privacy.rank(c.seen) > privacy.rank(APPS_LEVEL):
            return _text(f"That conversation has {c.seen} files: the Claude apps on your PC may not read its folder.", True)
        rel = posixpath.normpath(str(args.get("path") or ".").replace("\\", "/")).lstrip("/")
        if rel.startswith(".."):
            return _text("That path is outside the work folder.", True)
        if c is None:
            scope = privacy.Scope(ctl.projects, [privacy.GENERAL, *ctl.projects.names()], APPS_LEVEL)
            if rel not in ("", ".") and not scope.visible(rel):
                return _text(f"{rel} is hidden from the Claude apps by its privacy level.", True)
    if name == "list_files":
        try:
            d = ctl.list_files(rel or ".", conv)
        except FileNotFoundError as e:
            return _text(str(e), True)
        entries = d["entries"]
        if c is None:   # the main folder: only what a paid model may see
            entries = [e for e in entries if scope.visible(posixpath.normpath(posixpath.join(rel, e["name"])))]
        names = [e["name"] + ("/" if e["dir"] else f" ({e['size']} bytes)") for e in entries]
        return _text(f"{d['path']}:\n" + ("\n".join(names) if names else "(empty)"))
    if name == "read_file":
        try:
            d = ctl.read_file(str(args.get("path", "")), limit=100_000, conv=conv)
        except FileNotFoundError as e:
            return _text(str(e), True)
        if d["binary"]:
            return _text(f"{d['path']} is a binary file ({d['size']} bytes).", True)
        more = f"\n\n(cut off: the file has {d['size']} bytes)" if d.get("truncated") else ""
        return _text(d["text"] + more)
    return _text(f"Unknown tool {name}.", True)


def handle(ctl, msg: dict) -> dict | None:
    """One JSON-RPC message; None for notifications."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
        return ok({"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "uvmc", "version": "1"},
                   "instructions": "UVMC is a sandboxed machine with its own AI agent. Hand it work that "
                                   "should run there; its risky steps wait for the user's approval."})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        p = msg.get("params") or {}
        args = p.get("arguments") if isinstance(p.get("arguments"), dict) else {}
        ctl.log.event("notice", text=f"A Claude app on your PC used UVMC: {p.get('name')}.")
        return ok(call(ctl, str(p.get("name", "")), args))
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
