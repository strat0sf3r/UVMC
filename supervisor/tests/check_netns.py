"""Each agent user's own network for real (design.md 7t), in the VM, as the supervisor's user
(it has sudo):

    cd supervisor && /opt/uvmc/venv/bin/python3 tests/check_netns.py

Two of the agent's users (the last two job users; this empties their homes) run commands the
way the supervisor runs them (SystemdExecutor, in a test box), each with a proxy listening in
its own network the way a conversation's does. Checked: a user's server on 127.0.0.1 is
reachable by that user across commands, not by the other one and not from the VM; the VM's own
local servers (the panel, a stand-in program) and DNS are out of reach; the only interface is
loopback; the proxy works (a site through it, allowed by this check's own stand-in decider);
the headless browser works, on the user's own server and through the proxy.
Prints PASS or FAIL per check.
"""

import os
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from uvmc import browser, config, netns  # noqa: E402
from uvmc.executor import SystemdExecutor, agent_users, clean_user, kill_slice  # noqa: E402
from uvmc.netproxy import Proxy, SiteList  # noqa: E402

cfg = config.load(Path("/etc/uvmc/config.toml") if Path("/etc/uvmc/config.toml").exists() else ROOT / "config.toml")
A, B = agent_users(cfg)[-2:]   # the last two job users: no conversation uses them, and jobs rarely do
SA, SB = "uvmc-agent-check7ta.slice", "uvmc-agent-check7tb.slice"
PORT = 18766                   # B's server
HOST_PORT = 18767              # a program of the VM's own (stand-in for a dashboard)
ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))


def proxy_port(user):
    return cfg.conv_proxy_port + int(user[len(cfg.conv_user_prefix):])


def run(user, slice_name, script, timeout=30):
    ex = SystemdExecutor(cfg, slice_name, user=user, workdir="/", proxy_port=proxy_port(user))
    p = ex.run(script, timeout=timeout)
    return p.returncode, p.stdout.decode(errors="replace").strip(), p.stderr.decode(errors="replace").strip()


def reach(port):
    s = socket.socket()
    s.settimeout(2)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


print(f"users {A} and {B}")
for u in (A, B):
    clean_user(cfg, u)
check("their networks are made", netns.ensure([A, B]) == "")

# each one's proxy, in its own network; this check allows every site (the real one asks you)
proxies = []
for u in (A, B):
    try:
        proxies.append(Proxy(proxy_port(u), cfg.net_ports, SiteList([], Path(os.devnull)), lambda h, p: True,
                             lambda info: None, listener=netns.listen(u, proxy_port(u))).start())
    except OSError as e:
        check(f"{u}'s proxy opens in its network", False, str(e))
check("the proxies listen in the users' networks, not on the VM's loopback",
      len(proxies) == 2 and not reach(proxy_port(A)) and not reach(proxy_port(B)))

# a program of the VM's own on its loopback
host = socket.socket()
host.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
host.bind(("127.0.0.1", HOST_PORT))
host.listen(4)

# B runs a server for a while (in a thread: the command lasts 25 s)
bg = threading.Thread(target=run, args=(B, SB, f"cd /usr/share/doc && exec timeout 25 python3 -m http.server {PORT} "
                                               "--bind 127.0.0.1 > /dev/null 2>&1", 40))
bg.start()
time.sleep(2.5)

code, out, err = run(B, SB, f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 5 http://127.0.0.1:{PORT}/")
check("B reaches its own server from another command", out == "200", f"{code} {out} {err}")

code, out, err = run(A, SA, f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 5 http://127.0.0.1:{PORT}/; "
                            f"curl -s -o /dev/null -w ' %{{http_code}}' --max-time 5 http://localhost:{PORT}/; true")
check("A can't reach B's server", out.split() == ["000", "000"], f"{code} {out} {err}")
check("the VM can't reach B's server either", not reach(PORT))

code, out, err = run(A, SA, f"for p in 8080 {HOST_PORT} {cfg.proxy_port} {cfg.brain_proxy_port} {proxy_port(B)}; do "
                            "curl -s -o /dev/null -w '%{http_code} ' --max-time 3 http://127.0.0.1:$p/; done; true")
check("A can't reach the panel, the VM's programs or other proxies", set(out.split()) == {"000"}, out)

code, out, err = run(A, SA, "getent ahosts example.com > /dev/null && echo resolved || echo no-dns; "
                            "ip -o link | awk -F': ' '{print $2}' | tr '\\n' ' '; echo; ip route; ip -6 route; true")
lines = out.splitlines()
check("A has no DNS", lines[:1] == ["no-dns"], out)
check("A's only interface is loopback", lines[1:2] and lines[1].split() == ["lo"], out)
check("A has no route anywhere", lines[2:] == [] or all("lo" in ln and "::1" in ln for ln in lines[2:]), out)

code, out, err = run(A, SA, "curl -s -o /dev/null -w '%{http_code}' --max-time 20 https://example.com/")
check("A reaches a site through its proxy", out == "200", f"{code} {out} {err}")

if browser.version():
    script = (f"cd /tmp && {browser.VENV}/bin/python - <<'PY'\n"
              "import os\n"
              "from playwright.sync_api import sync_playwright\n"
              "with sync_playwright() as p:\n"
              "    b = p.chromium.launch(proxy={'server': os.environ['HTTPS_PROXY'], 'bypass': '127.0.0.1,localhost'})\n"
              "    pg = b.new_page()\n"
              f"    pg.goto('http://127.0.0.1:{PORT}/'); print('own', 'Directory listing' in pg.content())\n"
              "    pg.goto('https://example.com/'); print('site', pg.title())\n"
              "    b.close()\n"
              "PY")
    code, out, err = run(B, SB, script, timeout=60)
    check("the browser opens B's own server", "own True" in out, f"{code} {out} {err[-400:]}")
    check("the browser reaches a site through B's proxy", "site Example Domain" in out, f"{code} {out} {err[-400:]}")
else:
    print("SKIP the browser (not installed)")

bg.join(30)
kill_slice(SA), kill_slice(SB)
for p in proxies:
    p.stop()
host.close()
for u in (A, B):
    clean_user(cfg, u)
check("the namespaces stay for the next commands", netns.ensure([A, B]) == "" and
      all(os.path.ismount(f"{netns.NETNS_DIR}/uvmc-{u}") for u in (A, B)))

print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
