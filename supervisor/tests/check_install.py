"""Installing in place for real (UVMC 0.27+), on the VM, through the live supervisor's API and the
root helper uvmc-service-shell (what `uvmc shell` on the PC runs). Run as the supervisor's user:

    python3 tests/check_install.py

A throwaway "check-local" account: approved empty; a session installs a tiny app into it with
commands as the service (the helper's one-command form); checked: the commands run as the
service, can write the app folder, see the data folder, get no secrets, reach a site on the
session's list, wait on an unlisted one until it's answered (refused here) ; the session ends;
the app runs with its app folder read-only; a rollback restores the folder; removal leaves
nothing. Prints PASS/FAIL per check, removes the account.
"""

import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

TOKEN = Path("/etc/uvmc/panel_token").read_text().strip()
API = "http://127.0.0.1:8080"
NAME = "check-local"
ok = True

PROFILE = f"""contract = 1
name = "{NAME}"
title = "Check local"
run = "python3 app.py"
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

APP = r'''import os, time
print("app running as", os.getuid(), flush=True)
try:
    open("written-while-running", "w").write("x")
    print("app folder writable while running", flush=True)
except OSError as e:
    print("app folder read-only while running", e.errno, flush=True)
while True:
    time.sleep(30)
'''


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"  ({str(detail)[:400]})" if detail and not cond else ""))


def api(path, body=None):
    req = urllib.request.Request(API + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
                                 method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=200) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{e.code} {e.read().decode(errors='replace')[:300]}") from None


def inside(*cmd, timeout=120):
    """A command as the service, in its session's walls (the helper's one-command form)."""
    return subprocess.run(["sudo", "-n", "/usr/local/sbin/uvmc-service-shell", NAME, *cmd],
                          capture_output=True, text=True, timeout=timeout)


def wait(cond, seconds=60):
    end = time.time() + seconds
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def main():
    try:
        run_checks()
    finally:
        try:
            if (api(f"/api/services/{NAME}").get("session")):
                api("/api/services/session/end", {"name": NAME, "start_again": False})
        except RuntimeError:
            pass
        try:
            api("/api/services/remove", {"name": NAME})
        except RuntimeError:
            pass
    print("ALL PASS" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


def run_checks():
    v = api("/api/services/add-local", {"profile": PROFILE})
    check("an empty account is only proposed", not v["installed"] and v["proposal"]["commit"] == "local")
    v = api("/api/services/approve", {"name": NAME, "commit": "local"})
    app = Path(f"/var/lib/uvmc-services/{NAME}/app")
    check("approved: the account and its empty app folder", v["installed"] and subprocess.run(
        ["sudo", "-n", "ls", "-A", str(app)], capture_output=True, text=True).stdout.strip() == "")
    api("/api/services/settings", {"name": NAME, "values": {"token": "SECRET-IN-RUN-ONLY"}})
    r = inside("id", "-un")
    check("no shell without a session", r.returncode != 0 and "No install session" in r.stderr, r.stderr)
    s = api("/api/services/session/start", {"name": NAME, "who": "shell", "sites": ["example.com"]})
    check("a session opens with a snapshot", bool(s.get("snapshot")), s)
    r = inside("id", "-un")
    check("commands run as the service", r.stdout.strip() == f"uvmc-s-{NAME}", r.stdout + r.stderr)
    r = inside("sh", "-c", f"cat > app.py <<'EOF'\n{APP}EOF\nls")
    check("the app folder is writable in a session", "app.py" in r.stdout, r.stdout + r.stderr)
    r = inside("sh", "-c", 'ls "$APP_DATA" >/dev/null && echo data-visible; ls "$CREDENTIALS_DIRECTORY" 2>&1; env | grep -c SECRET-IN-RUN-ONLY')
    check("your shell sees the data folder", "data-visible" in r.stdout, r.stdout + r.stderr)
    check("no secrets in a session", "SECRET-IN-RUN-ONLY" not in r.stdout and r.stdout.strip().endswith("0"), r.stdout)
    r = inside("python3", "-c", "import urllib.request; print(urllib.request.urlopen('https://example.com/', timeout=20).status)")
    check("a site on the session's list is reached", r.stdout.strip() == "200", r.stdout + r.stderr)
    # an unlisted site waits for an answer: refuse it from here
    answered = {}

    def answer():
        if wait(lambda: api(f"/api/services/{NAME}")["asks"], 30):
            q = api(f"/api/services/{NAME}")["asks"][0]
            answered["host"] = q["host"]
            api("/api/services/answer", {"name": NAME, "id": q["id"], "allow": False})
    t = threading.Thread(target=answer)
    t.start()
    r = inside("python3", "-c", "import urllib.request\ntry:\n urllib.request.urlopen('https://example.org/', timeout=60); print('reached')\n"
               "except Exception as e:\n print('refused', type(e).__name__)")
    t.join(40)
    check("an unlisted site is asked, and refused stays refused", answered.get("host") == "example.org" and "refused" in r.stdout,
          f"{answered} {r.stdout} {r.stderr}")
    end = api("/api/services/session/end", {"name": NAME})
    check("the session ends, listing the sites it used", end["new_sites"] == ["example.com"], end)
    r = inside("true")
    check("no shell after the session", r.returncode != 0)
    api("/api/services/enable", {"name": NAME, "on": True})
    check("the app runs, its app folder read-only", wait(lambda: "read-only while running" in api(f"/api/services/{NAME}")["log"], 60),
          api(f"/api/services/{NAME}")["log"][-600:])
    snaps = api(f"/api/services/{NAME}").get("snapshots") or []
    api("/api/services/rollback", {"name": NAME, "snapshot": snaps[-1]})   # the oldest: before app.py existed
    has_app = subprocess.run(["sudo", "-n", "test", "-e", str(app / "app.py")]).returncode == 0
    check("a rollback restores the app folder", not has_app)
    api("/api/services/remove", {"name": NAME})
    check("removal leaves nothing", subprocess.run(["id", f"uvmc-s-{NAME}"], capture_output=True).returncode != 0
          and not Path(f"/var/lib/uvmc-services/{NAME}").exists())


if __name__ == "__main__":
    main()
