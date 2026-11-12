"""Services for real (UVMC 0.25+), on the VM, through the live supervisor's API. Run as the
supervisor's user (ubuntu, which has sudo):

    python3 tests/check_services.py

A throwaway app "check-app" in a local repository under /tmp: added, approved, set up, switched
on; then checked: it runs as its own user, restarts after a crash, reaches its declared site
and SSH machine (this VM's own sshd: the host key is verified, the login is refused, which is
the point: nothing is authorized), is refused an undeclared site and a direct connection,
reports through the client library, can't be reached by an agent's user; an update waits for
approval and runs after it; removal leaves nothing. Prints PASS/FAIL per check, removes the app.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOKEN = Path("/etc/uvmc/panel_token").read_text().strip()
API = "http://127.0.0.1:8080"
WORK = Path("/tmp/uvmc-check-services")
VM_IP = subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout.split()[0]
ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"  ({str(detail)[:400]})" if detail and not cond else ""))


def api(path, body=None):
    req = urllib.request.Request(API + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
                                 method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{e.code} {e.read().decode(errors='replace')[:300]}") from None


def git(*args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "check", "GIT_AUTHOR_EMAIL": "check@example.com",
           "GIT_COMMITTER_NAME": "check", "GIT_COMMITTER_EMAIL": "check@example.com"}
    return subprocess.run(["git", "-C", str(WORK / "app"), *args], check=True, capture_output=True, text=True, env=env).stdout


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


MANIFEST = f"""contract = 1
name = "check-app"
title = "Check app"
description = "A throwaway app for check_services.py"
run = "python3 app.py"
memory = "96M"

[network]
sites = ["example.com"]
ssh = ["self"]

[ssh.self]
host = "{VM_IP}"
user = "nobody"
key = "self_key"

[[settings]]
name = "greeting"
label = "Greeting"
type = "text"
default = "hello"

[[settings]]
name = "self_key"
label = "SSH key"
type = "secret"
"""

APP = r'''import os, socket, subprocess, sys, time, urllib.request
from uvmc_client import Uvmc
data = os.environ["APP_DATA"]
marker = os.path.join(data, "started-once")
if not os.path.exists(marker):
    open(marker, "w").close()
    print("first start: exiting with 3 to test the restart", flush=True)
    sys.exit(3)
print("VERSION", "@VERSION@", flush=True)
print("greeting", os.environ.get("APP_GREETING"), flush=True)
print("whoami", subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip(), flush=True)
print("secret file", os.path.exists(os.path.join(os.environ.get("CREDENTIALS_DIRECTORY", "/x"), "self_key")), flush=True)
print("secret in env", any("BEGIN OPENSSH" in v for v in os.environ.values()), flush=True)
for url in ("https://example.com/", "https://example.org/"):
    try:
        urllib.request.urlopen(url, timeout=15).read(100)
        print("web", url, "ok", flush=True)
    except Exception as e:
        print("web", url, "refused", type(e).__name__, flush=True)
try:
    socket.create_connection(("1.1.1.1", 443), timeout=5).close()
    print("direct ok", flush=True)
except OSError as e:
    print("direct refused", e.errno, flush=True)
r = subprocess.run(["ssh", "-o", "ConnectTimeout=10", "self", "true"], capture_output=True, text=True)
print("ssh exit", r.returncode, r.stderr.strip().replace("\n", " | ")[:300], flush=True)
u = Uvmc()
print("report", u.report("Check app", ("ok", "@VERSION@ running"), [u.text("hello from check-app")]), u.last_error, flush=True)
while True:
    u.heartbeat()
    time.sleep(20)
'''


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    (WORK / "app").mkdir(parents=True)
    os.chmod(WORK, 0o755)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(WORK / "key")], check=True)
    git("init", "-q", "-b", "main")
    (WORK / "app" / "uvmc-app.toml").write_text(MANIFEST)
    (WORK / "app" / "app.py").write_text(APP.replace("@VERSION@", "v1"))
    shutil.copy(ROOT / "client" / "python" / "uvmc_client.py", WORK / "app" / "uvmc_client.py")
    git("add", "-A")
    git("commit", "-q", "-m", "v1")
    try:
        run_checks()
    finally:
        try:
            api("/api/services/remove", {"name": "check-app"})
        except RuntimeError:
            pass
        shutil.rmtree(WORK, ignore_errors=True)
    print("ALL PASS" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


def run_checks():
    v = api("/api/services/add", {"url": f"file://{WORK}/app", "branch": "main"})
    check("adding only proposes", not v["installed"] and v["proposal"]["kind"] == "install", v)
    check("no unit before approval", subprocess.run(["systemctl", "cat", "uvmc-s-check-app.service"], capture_output=True).returncode != 0)
    v = api("/api/services/approve", {"name": "check-app", "commit": v["proposal"]["commit"]})
    check("approved and installed", v["installed"])
    check("needs its secret and host key", set(v["missing"]) == {"SSH key", "host key of self"}, v["missing"])
    api("/api/services/settings", {"name": "check-app", "values": {"greeting": "hi there", "self_key": (WORK / "key").read_text()}})
    scan = api("/api/services/keyscan", {"name": "check-app", "machine": "self"})
    check("reads the machine's host key", scan["lines"] and scan["fingerprints"], scan)
    api("/api/services/hostkey", {"name": "check-app", "machine": "self", "text": scan["lines"]})
    api("/api/services/enable", {"name": "check-app", "on": True})
    check("runs (after its planned first crash)", wait(lambda: "report" in api("/api/services/check-app")["log"], 90))
    d = api("/api/services/check-app")
    log = d["log"]
    check("restarted after the crash", d["status"]["restarts"] >= 1 and "exiting with 3" in log, d["status"])
    check("runs as its own user", "whoami uvmc-s-check-app" in log, log[-1500:])
    check("gets its setting", "greeting hi there" in log)
    check("gets its secret as a file, not in its environment", "secret file True" in log and "secret in env False" in log)
    check("reaches its declared site", "web https://example.com/ ok" in log)
    check("is refused an undeclared site", "web https://example.org/ refused" in log)
    check("is refused a direct connection", "direct refused" in log)
    check("reaches its SSH machine with a verified host key (login refused)",
          "Permission denied" in log and "Host key verification failed" not in log, log[-800:])
    net = d["net"]
    check("its connections are listed with why", any(n["host"] == "example.org" and n["verdict"] == "deny" for n in net)
          and any(n["host"] == VM_IP and n["reason"] == "declared machine" for n in net), net[:5])
    progs = api("/api/programs")
    me = next((p for p in progs if p["name"] == "check-app"), {})
    check("its report reached UVMC (its program key)", me.get("kind") == "service" and (me.get("last_report") or {}).get("title") == "Check app", me)
    port = json.loads(Path("/var/lib/uvmc/services.json").read_text())["check-app"]["port"]
    agent = subprocess.run(["sudo", "-n", "-u", "agent14", "curl", "-s", "-m", "5", "-x", f"http://127.0.0.1:{port}", "https://example.com/"],
                           capture_output=True, text=True)
    check("an agent can't use its proxy", agent.returncode != 0, agent.returncode)
    agent = subprocess.run(["sudo", "-n", "-u", "agent14", "ls", "/var/lib/uvmc-services/check-app/data"], capture_output=True, text=True)
    check("an agent can't read its data", agent.returncode != 0)
    # an update waits, then runs
    (WORK / "app" / "app.py").write_text(APP.replace("@VERSION@", "v2"))
    git("commit", "-q", "-am", "v2")
    found = api("/api/services/check", {"name": "check-app"})["found"]
    d = api("/api/services/check-app")
    check("a new version waits for approval", found == ["check-app"] and d["proposal"]["kind"] == "update"
          and "VERSION v1" in d["log"] and "VERSION v2" not in d["log"])
    api("/api/services/approve", {"name": "check-app", "commit": d["proposal"]["commit"]})
    check("the approved version runs", wait(lambda: "VERSION v2" in api("/api/services/check-app")["log"], 60))
    api("/api/services/remove", {"name": "check-app"})
    check("removal leaves nothing", subprocess.run(["id", "uvmc-s-check-app"], capture_output=True).returncode != 0
          and not Path("/etc/systemd/system/uvmc-s-check-app.service").exists()
          and not Path("/var/lib/uvmc-services/check-app").exists()
          and "check-app" not in [p["name"] for p in api("/api/programs")])


if __name__ == "__main__":
    main()
