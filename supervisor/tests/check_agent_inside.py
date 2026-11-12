"""An agent inside a service's account, for real (UVMC 0.28+), on the VM. Run as the supervisor's
user:  python3 tests/check_agent_inside.py [--model claude-plan-haiku]

A throwaway "check-agent" account. Part 1 needs no model: the commands an agent's conversation
runs (the supervisor's SessionExecutor with the session's agent properties) run as the service,
in its app folder (writable), without its data folder, its secrets or UVMC's key. Part 2 (with
--model) opens a real inside conversation through the API and gives it one short task; its
command must report the same. Prints PASS/FAIL per check and removes the account.
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, "/opt/uvmc")
from uvmc import config  # noqa: E402
from uvmc.executor import SessionExecutor  # noqa: E402

TOKEN = Path("/etc/uvmc/panel_token").read_text().strip()
API = "http://127.0.0.1:8080"
NAME = "check-agent"
ok = True
PROFILE = f"""contract = 1
name = "{NAME}"
title = "Check agent"
run = "sleep infinity"
memory = "96M"

[network]
sites = []
ssh = []

[[settings]]
name = "token"
label = "A secret"
type = "secret"
required = false
"""


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"  ({str(detail)[:500]})" if detail and not cond else ""))


def api(path, body=None, timeout=200):
    req = urllib.request.Request(API + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
                                 method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{e.code} {e.read().decode(errors='replace')[:300]}") from None


PROBE = ('id -un; pwd; echo hi > probe.txt && echo app-writable; ls "$APP_DATA" >/dev/null 2>&1 && echo data-visible || echo data-hidden; '
         'ls "${CREDENTIALS_DIRECTORY:-/nonexistent}" >/dev/null 2>&1 && echo has-credentials || echo no-credentials; '
         'env | grep -q SECRET-ONLY-WHEN-RUNNING && echo secret-in-env || echo no-secret')


def probe_ok(out: str, label: str):
    check(f"{label}: runs as the service", f"uvmc-s-{NAME}" in out, out)
    check(f"{label}: in its app folder, writable", f"/var/lib/uvmc-services/{NAME}/app" in out and "app-writable" in out, out)
    check(f"{label}: its data folder hidden", "data-hidden" in out, out)
    check(f"{label}: no secrets", "no-credentials" in out and "no-secret" in out, out)


def main():
    model = sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else None
    try:
        api("/api/services/add-local", {"profile": PROFILE})
        api("/api/services/approve", {"name": NAME, "commit": "local"})
        api("/api/services/settings", {"name": NAME, "values": {"token": "SECRET-ONLY-WHEN-RUNNING"}})
        # part 1: the executor an inside conversation uses
        cfg = config.load(Path("/etc/uvmc/config.toml"))
        api("/api/services/session/start", {"name": NAME, "who": "agent"})
        props = Path(f"/var/lib/uvmc-services/{NAME}/session-agent.properties")
        import subprocess
        lines = subprocess.run(["sudo", "-n", "cat", str(props)], capture_output=True, text=True).stdout.splitlines()
        state = json.loads(Path("/var/lib/uvmc/services.json").read_text())[NAME]
        ex = SessionExecutor(cfg, "uvmc-agent.slice", lambda: lines, f"uvmc-s-{NAME}-agent", user=f"uvmc-s-{NAME}",
                             workdir=f"/var/lib/uvmc-services/{NAME}/app", proxy_port=state["port"])
        p = ex.run('exec bash -lc "$1" 2>&1', PROBE, timeout=60)
        probe_ok(p.stdout.decode(errors="replace"), "executor")
        api("/api/services/session/end", {"name": NAME, "start_again": False})
        # part 2: a real inside conversation, one short task
        if model:
            r = api("/api/services/agent", {"name": NAME, "model": model})
            conv = r["conv"]
            check("an inside conversation opens its session", api(f"/api/services/{NAME}")["session"]["who"] == "agent")
            res = api("/api/run", {"text": "Run exactly this one command with run_command and reply with its output only: " + PROBE,
                                   "conv": conv, "timeout": 240, "no_ask": True}, timeout=300)
            outs = " ".join(str(s.get("output", "")) + str(s.get("result", "")) + str(s) for s in res.get("steps", []))
            probe_ok(outs, "real agent")
            api("/api/conversations/close", {"conv": conv})
            check("closing its conversation ends the session", api(f"/api/services/{NAME}")["session"] is None)
            check("its data folder survived (never cleaned like an agent's home)",
                  subprocess.run(["sudo", "-n", "test", "-d", f"/var/lib/uvmc-services/{NAME}/data"]).returncode == 0)
    finally:
        try:
            if api(f"/api/services/{NAME}").get("session"):
                api("/api/services/session/end", {"name": NAME, "start_again": False})
        except RuntimeError:
            pass
        try:
            api("/api/services/remove", {"name": NAME})
        except RuntimeError:
            pass
    print("ALL PASS" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
