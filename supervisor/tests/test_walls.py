"""Offline tests for the walls: network proxy, site approvals, activity log, checkpoints.

Run from the supervisor folder:  UVMC_NO_SUDO=1 python3 -m unittest discover tests
"""

import http.server
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ["UVMC_NO_SUDO"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from anthropic.types import TextBlock, ToolUseBlock  # noqa: E402

from test_service import ServiceBase, wait_for  # noqa: E402
from test_supervisor import FakeClient, reply  # noqa: E402
from uvmc.audit import ExecFilter, ExecParser, ExecWatcher  # noqa: E402
from uvmc.checkpoints import Checkpoints  # noqa: E402
from uvmc.netproxy import Proxy, SiteList  # noqa: E402
from uvmc.service import Controller  # noqa: E402
from uvmc.tools import Tools  # noqa: E402

UPSTREAM_HOST = "127.0.0.2"  # a loopback address that is not in NO_PROXY, so clients use the proxy


class Hello(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"hello from upstream"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def start_upstream():
    srv = http.server.ThreadingHTTPServer((UPSTREAM_HOST, 0), Hello)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def through_proxy(proxy_port, raw: bytes) -> bytes:
    s = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
    s.sendall(raw)
    data = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
            if b"hello from upstream" in data:
                break
    except socket.timeout:
        pass
    s.close()
    return data


class ProxyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.up = start_upstream()
        self.up_port = self.up.server_address[1]
        self.events, self.asked = [], []
        self.sites = SiteList([UPSTREAM_HOST], Path(self.tmp.name) / "learned.txt")
        self.proxy = Proxy(0, [80, 443, self.up_port], self.sites,
                           lambda h, p: self.asked.append(h) or False, self.events.append).start()

    def tearDown(self):
        self.proxy.stop()
        self.up.shutdown()
        self.tmp.cleanup()

    def test_allowed_http_and_connect(self):
        r = through_proxy(self.proxy.port, f"GET http://{UPSTREAM_HOST}:{self.up_port}/x HTTP/1.1\r\n"
                                           f"Host: {UPSTREAM_HOST}\r\n\r\n".encode())
        self.assertIn(b"200 OK", r)
        self.assertIn(b"hello from upstream", r)
        r = through_proxy(self.proxy.port, f"CONNECT {UPSTREAM_HOST}:{self.up_port} HTTP/1.1\r\n\r\n"
                                           f"GET / HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        self.assertIn(b"200 Connection established", r)
        self.assertIn(b"hello from upstream", r)
        self.assertTrue(wait_for(lambda: len(self.events) == 2))
        self.assertTrue(all(e["verdict"] == "allow" and e["received"] > 0 for e in self.events))

    def test_unknown_site_and_port(self):
        r = through_proxy(self.proxy.port, b"CONNECT example.org:443 HTTP/1.1\r\n\r\n")
        self.assertIn(b"403", r)
        self.assertEqual(self.asked, ["example.org"])
        r = through_proxy(self.proxy.port, f"CONNECT {UPSTREAM_HOST}:22 HTTP/1.1\r\n\r\n".encode())
        self.assertIn(b"port 22 is not allowed", r)
        self.assertEqual([e["verdict"] for e in self.events], ["deny", "deny"])

    def test_site_list(self):
        self.assertTrue(self.sites.allows(UPSTREAM_HOST))
        self.sites.add("Example.org.")
        self.assertTrue(self.sites.allows("files.example.org"))
        self.assertFalse(self.sites.allows("example.org.evil.net"))
        again = SiteList([], self.sites.file)
        self.assertEqual(again.learned, ["example.org"])
        with self.assertRaises(ValueError):
            self.sites.remove(UPSTREAM_HOST)  # set in config
        with self.assertRaises(ValueError):
            self.sites.add("bad host name")


class SiteApprovalTest(ServiceBase):
    def test_agent_command_asks_for_unknown_site(self):
        up = start_upstream()
        port = up.server_address[1]
        self.cfg.net_allow, self.cfg.net_ports, self.cfg.proxy_port = [], [port], 0
        url = f"http://{UPSTREAM_HOST}:{port}/"
        ctl = self.controller([
            reply(ToolUseBlock(type="tool_use", id="n1", name="run_command",
                               input={"command": f"python3 -c \"import urllib.request; print(urllib.request.urlopen('{url}').read().decode())\""})),
            reply(TextBlock(type="text", text="Fetched."), stop="end_turn"),
        ])
        ctl.start_background(audit=False)
        self.cfg.proxy_port = ctl.proxy.port
        try:
            ctl.start_task("fetch it")
            self.assertTrue(wait_for(lambda: ctl.state == "waiting"))
            pending = ctl.snapshot()["pending"][0]
            self.assertEqual((pending["type"], pending["host"], pending["call"]), ("site", UPSTREAM_HOST, "n1"))
            ctl.decide(pending["id"], "always")
            self.assertTrue(wait_for(lambda: ctl.state == "idle", 10))
            result = [e for e in self.events if e["kind"] == "result"][0]
            self.assertIn("hello from upstream", result["text"])
            net = [e for e in self.events if e["kind"] == "net"]
            self.assertEqual(net[0]["call"], "n1")
            self.assertIn(UPSTREAM_HOST, ctl.sites.learned)
            self.assertEqual(ctl.decide_site(ctl.conv(), "other.example", 443), False)  # idle: nothing new goes out
        finally:
            ctl.proxy.stop()
            ctl.conv().proxy.stop()
            up.shutdown()

    def test_unanswered_site_times_out(self):
        self.cfg.net_approval_timeout_s = 1
        ctl = self.controller([])
        ctl.state = "running"
        t0 = time.time()
        self.assertFalse(ctl.decide_site(ctl.conv(), "slow.example", 443))
        self.assertLess(time.time() - t0, 4)
        self.assertTrue(wait_for(lambda: any(e["kind"] == "approval_done" for e in self.events)))
        self.assertTrue([e for e in self.events if e["kind"] == "approval_done"][0]["timed_out"])


AUDIT_LINES = """\
type=SYSCALL msg=audit(1731000000.123:42): arch=c000003e syscall=59 success=yes exit=0 items=2 ppid=100 pid=101 auid=4294967295 uid=1001 comm="pip" exe="/usr/bin/python3.12" key="uvmc-agent"
type=EXECVE msg=audit(1731000000.123:42): argc=3 a0="pip" a1="install" a2=2D2D75736572
type=CWD msg=audit(1731000000.123:42): cwd="/home/agent/work"
type=PROCTITLE msg=audit(1731000000.123:42): proctitle=706970
type=EOE msg=audit(1731000000.123:42):
type=SYSCALL msg=audit(1731000001.000:43): arch=c000003e syscall=59 success=yes exit=0 ppid=1 pid=7 uid=0 comm="cron" exe="/usr/sbin/cron" key="other"
type=EXECVE msg=audit(1731000001.000:43): argc=1 a0="cron"
type=EOE msg=audit(1731000001.000:43):
"""


class AuditTest(unittest.TestCase):
    def test_parser(self):
        out = []
        p = ExecParser(out.append)
        for line in AUDIT_LINES.splitlines():
            p.feed(line)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["args"], ["pip", "install", "--user"])
        self.assertEqual((out[0]["pid"], out[0]["ppid"], out[0]["cwd"]), (101, 100, "/home/agent/work"))

    def test_watcher_follows_new_lines(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "audit.log"
            log.write_text("type=EOE msg=audit(1.0:1):\n")
            out = []
            w = ExecWatcher(out.append, log).start()
            self.assertTrue(wait_for(lambda: w.available))
            with log.open("a") as f:
                f.write(AUDIT_LINES)
            self.assertTrue(wait_for(lambda: len(out) == 1))
            w.stop()

    def test_filter_hides_plumbing(self):
        f = ExecFilter("/home/agent/work")
        ev = lambda pid, ppid, *args: {"pid": pid, "ppid": ppid, "args": list(args)}  # noqa: E731
        self.assertFalse(f.keep(ev(10, 1, "env", "HTTP_PROXY=http://127.0.0.1:3128", "timeout")))
        self.assertFalse(f.keep(ev(10, 1, "timeout", "-k", "5", "120", "bash")))
        self.assertFalse(f.keep(ev(11, 10, "bash", "-c", 'cd "/home/agent/work" && exec bash -lc "$1" 2>&1', "uvmc", "ls")))
        self.assertFalse(f.keep(ev(11, 10, "bash", "-lc", "ls")))
        self.assertTrue(f.keep(ev(12, 11, "ls", "-la")))
        self.assertFalse(f.keep(ev(21, 20, "bash", "-c", 'cd "/home/agent/work" && : uvmc-panel; find .', "uvmc")))
        self.assertFalse(f.keep(ev(22, 21, "find", ".")))
        self.assertTrue(f.keep(ev(23, 12, "python3", "script.py")))


class CheckpointTest(ServiceBase):
    def test_create_restore_prune(self):
        self.cfg.checkpoints_keep = 2
        cps = Checkpoints(self.cfg)
        work = Path(self.cfg.workdir)
        (work / "a.txt").write_text("first")
        c1 = cps.create()
        (work / "a.txt").write_text("changed")
        (work / "new.txt").write_text("added")
        cps.restore(c1)
        self.assertEqual((work / "a.txt").read_text(), "first")
        self.assertFalse((work / "new.txt").exists())
        time.sleep(1.1)
        cps.create()
        time.sleep(1.1)
        cps.create()
        self.assertEqual(len(cps.list()), 2)
        self.assertNotIn(c1, cps.list())
        with self.assertRaises(KeyError):
            cps.restore(c1)

    def test_undo_from_controller(self):
        ctl = self.controller([
            reply(ToolUseBlock(type="tool_use", id="w1", name="write_file", input={"path": "out.txt", "content": "made"})),
            reply(TextBlock(type="text", text="Done."), stop="end_turn"),
        ])
        ctl.start_task("make a file")
        self.assertTrue(wait_for(lambda: ctl.state == "idle" and any(e["kind"] == "end" for e in self.events)))
        cp = [e for e in self.events if e["kind"] == "checkpoint"][0]["id"]
        copy = ctl.conv().copy_dir   # the conversation's own copy of the work folder
        self.assertTrue(Path(copy, "out.txt").exists())
        ctl.undo(cp)
        self.assertFalse(Path(copy, "out.txt").exists())
        self.assertEqual(len(ctl.copies.checkpoints(ctl.current).list()), 2)  # the undo point and the safety copy


class SystemPackageTest(ServiceBase):
    def test_names_are_checked(self):
        t = Tools(self.cfg)
        r = t.run("install_system_package", {"packages": ["ok-name", "bad;rm -rf /"], "reason": "x"})
        self.assertTrue(r.is_error)
        self.assertIn("Not valid package names", r.text)
        self.assertTrue(t.run("install_system_package", {"packages": [], "reason": "x"}).is_error)


if __name__ == "__main__":
    unittest.main()
