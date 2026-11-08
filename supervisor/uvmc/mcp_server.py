"""The only hands Claude Code gets: an MCP server that forwards every tool call to the supervisor.

Claude Code (running as the brain user, with its own tools switched off) starts this
script over stdio. Each tool call goes to the supervisor through a unix socket; the
supervisor applies the rules, asks you when needed, runs the action as the agent user
and logs it in the panel. This script runs nothing itself.

Standalone on purpose (standard library only, no package imports), so it can run with
`python -I` and nothing from the environment changes what it loads.

    python3 -I mcp_server.py /run/uvmc/bridge.sock CONVERSATION_KEY

The key names the conversation this Claude Code belongs to (several can work at once).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading

PROTOCOL = "2025-06-18"
NAME = "uvmc"


def ask_supervisor(path: str, request: dict, timeout: float | None = None) -> dict:
    """One request, one answer, one connection. Tool calls can wait a long time for you."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(path)
        s.sendall(json.dumps(request).encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    if not data:
        raise ConnectionError("the supervisor closed the connection")
    return json.loads(data)


CONV = ""   # the conversation key, from the command line


def handle(path: str, msg: dict) -> dict | None:
    """Answers one JSON-RPC message; None for notifications."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None  # notifications (initialized, cancelled): nothing to answer

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
        return ok({"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": NAME, "version": "1"},
                   "instructions": "These tools act on the UVMC machine. Every call is checked, "
                                   "logged and, when needed, approved by the user."})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        try:
            tools = ask_supervisor(path, {"op": "list", "conv": CONV}, timeout=30).get("tools", [])
        except (OSError, ValueError) as e:
            print(f"uvmc mcp: cannot reach the supervisor: {e}", file=sys.stderr)
            tools = []
        return ok({"tools": [{"name": t["name"], "description": t.get("description", ""),
                              "inputSchema": t.get("input_schema") or {"type": "object"}} for t in tools]})
    if method == "tools/call":
        params = msg.get("params") or {}
        try:
            answer = ask_supervisor(path, {"op": "call", "conv": CONV, "name": params.get("name", ""),
                                           "args": params.get("arguments") or {}})
            text, is_error = str(answer.get("text", "")), bool(answer.get("is_error"))
        except (OSError, ValueError) as e:
            text, is_error = f"The supervisor could not be reached: {e}", True
        return ok({"content": [{"type": "text", "text": text}], "isError": is_error})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}


def main() -> None:
    global CONV
    path = sys.argv[1] if len(sys.argv) > 1 else "/run/uvmc/bridge.sock"
    CONV = sys.argv[2] if len(sys.argv) > 2 else ""
    # Nothing here needs the login; don't keep it around.
    for k in list(os.environ):
        if k.startswith(("CLAUDE_CODE_OAUTH", "ANTHROPIC_")):
            del os.environ[k]
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        for m in (msg if isinstance(msg, list) else [msg]):
            if isinstance(m, dict):
                # Tool calls can wait minutes for your approval; answer other messages meanwhile.
                threading.Thread(target=reply, args=(path, m), daemon=True).start()


_out = threading.Lock()


def reply(path: str, msg: dict) -> None:
    r = handle(path, msg)
    if r is not None:
        with _out:
            sys.stdout.write(json.dumps(r) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
