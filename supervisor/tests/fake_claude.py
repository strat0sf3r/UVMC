#!/usr/bin/env python3
"""A stand-in for `claude -p --input-format stream-json --output-format stream-json`.

It speaks the same stream-json lines and calls tools through the real MCP server from
--mcp-config, so the whole path (process, MCP server, bridge, supervisor) is exercised.
What it does depends on the message:
    run: <command>   calls run_command through MCP, then reports the result
    write: <path>    calls write_file
    wait             says so, then waits for an interrupt
    limit            ends the turn with a usage-limit error
    anything else    answers with text only
Writes its arguments and what it saw to fake.json in the folder it starts in.
"""

import json
import os
import select
import subprocess
import sys
import uuid

args = sys.argv[1:]

if args == ["--version"]:
    print("9.9.9 (Claude Code)")
    sys.exit(0)

# `claude setup-token`, drawn like the real one: the link inside a terminal hyperlink code,
# spaces as cursor moves, and the token followed straight by cursor codes and more text.
if args[:1] == ["setup-token"]:
    url = "https://claude.com/cai/oauth/authorize?code=true&client_id=x&scope=user%3Ainference&state=abc"
    sys.stdout.write("\x1b[2G\x1b[1mThis\x1b[7Gwill\x1b[12Gguide\x1b[22m\r\r\n"
                     "\x1b[38;5;246mBrowser didn't open? Use the url below to sign in\x1b[39m\r\r\n\r\r\n"
                     f"\x1b]8;id=1;{url}\x07\x1b[38;5;246m{url}\x1b[39m\x1b]8;;\x07\r\r\n\r\r\n"
                     "\x1b[2GPaste\x1b[8Gcode\x1b[13Ghere\x1b[18Gif\x1b[21Gprompted\x1b[30G>\r\r\n")
    sys.stdout.flush()
    code = sys.stdin.readline().strip()
    if code == "good#abc":
        token = "sk-ant-oat01-" + "Ab3_-" * 20 + "QQ"
        sys.stdout.write(f"\x1b[2K\x1b[1A\x1b[32m{token}\x1b[39m\x1b[1B\x1b[2GStore\x1b[8Gthis\x1b[13Gtoken\r\r\n")
    else:
        sys.stdout.write("\x1b[38;5;211mOAuth error: Invalid code\x1b[39m\x1b[K\r\x1b[2B\x1b[KPress Enter to retry.\r\r\n")
    sys.stdout.flush()
    sys.stdin.readline()
    sys.exit(0)


def arg(name):
    return args[args.index(name) + 1] if name in args else None


log = os.path.join(os.getcwd(), "fake.json")  # the brain starts in its own folder
if log:
    with open(log, "w") as f:
        json.dump({"argv": args, "token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
                   "api_key": os.environ.get("ANTHROPIC_API_KEY"),
                   "proxy": os.environ.get("HTTPS_PROXY")}, f)


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


class MCP:
    def __init__(self, spec):
        self.p = subprocess.Popen([spec["command"], *spec.get("args", [])], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True)
        self.n = 0
        self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fake"}})
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.p.stdin.flush()
        self.tools = [t["name"] for t in self.call("tools/list", {})["tools"]]

    def call(self, method, params):
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params}) + "\n")
        self.p.stdin.flush()
        while True:
            msg = json.loads(self.p.stdout.readline())
            if msg.get("id") == self.n:
                return msg["result"]


mcp = MCP(json.loads(arg("--mcp-config"))["mcpServers"]["uvmc"])
session = str(uuid.uuid4())
out({"type": "system", "subtype": "init", "session_id": session, "model": arg("--model"),
     "tools": ["mcp__uvmc__" + t for t in mcp.tools],
     "mcp_servers": [{"name": "uvmc", "status": "connected"}]})


def assistant(content, mid=None):
    out({"type": "assistant", "parent_tool_use_id": None, "session_id": session,
         "message": {"id": mid or "msg_" + uuid.uuid4().hex[:8], "role": "assistant", "content": content,
                     "usage": {"input_tokens": 100, "output_tokens": 20}}})


def result(text, error=False, subtype="success"):
    out({"type": "result", "subtype": subtype, "is_error": error, "result": text, "session_id": session,
         "usage": {"input_tokens": 100, "output_tokens": 20}})


def tool(name, inp):
    tid = "toolu_" + uuid.uuid4().hex[:10]
    mid = "msg_" + uuid.uuid4().hex[:8]
    assistant([{"type": "text", "text": f"Using {name}."}], mid)            # one reply, split in two
    assistant([{"type": "tool_use", "id": tid, "name": "mcp__uvmc__" + name, "input": inp}], mid)
    r = mcp.call("tools/call", {"name": name, "arguments": inp})
    text = r["content"][0]["text"]
    out({"type": "user", "parent_tool_use_id": None,
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": text,
                                                  "is_error": r.get("isError", False)}]}})
    return text, r.get("isError", False)


for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("type") != "user":
        continue
    text = msg["message"]["content"]
    if log:
        with open(log + ".msgs", "a") as f:
            f.write(json.dumps(text) + "\n")
    task = text.rsplit("(Now the user says:)\n", 1)[-1]
    if task.startswith("run: "):
        res, err = tool("run_command", {"command": task[5:]})
        assistant([{"type": "text", "text": f"Result ({'error' if err else 'ok'}): {res}"}])
        result("ok")
    elif task.startswith("write: "):
        res, err = tool("write_file", {"path": task[7:], "content": "hello\n"})
        assistant([{"type": "text", "text": res}])
        result("ok")
    elif task == "wait":
        assistant([{"type": "text", "text": "Waiting."}])
        while True:
            r, _, _ = select.select([sys.stdin], [], [], 30)
            if not r:
                break
            ctl = json.loads(sys.stdin.readline())
            if ctl.get("type") == "control_request" and ctl["request"]["subtype"] == "interrupt":
                out({"type": "control_response", "response": {"subtype": "success", "request_id": ctl["request_id"]}})
                result("interrupted", error=True, subtype="error_during_execution")
                break
    elif task == "limit":
        result("Claude usage limit reached. Your limit resets at 5pm.", error=True)
    else:
        out({"type": "rate_limit_event", "session_id": session, "uuid": "u1", "rate_limit_info": {
            "status": "allowed_warning", "resetsAt": 1794150000, "rateLimitType": "five_hour", "utilization": 0.81,
            "isUsingOverage": False,
            "unifiedWindows": {"five_hour": {"utilization": 0.81, "resetsAt": 1794150000},
                               "seven_day": {"utilization": 0.3, "resetsAt": 1794600000000}}}})
        assistant([{"type": "text", "text": "Hi from the plan."}])
        result("Hi from the plan.")
