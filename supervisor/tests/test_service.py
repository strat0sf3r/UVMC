"""Offline tests for the service: task flow, approvals, stop, and the API's access check.

Run from the supervisor folder:  UVMC_NO_SUDO=1 python3 -m unittest discover tests
"""

import json
import os
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
from fastapi.testclient import TestClient  # noqa: E402

from test_supervisor import FakeClient, make_cfg, reply  # noqa: E402
from uvmc.service import Controller, create_app  # noqa: E402


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class ServiceBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        work = os.path.join(self.tmp.name, "work")
        os.makedirs(work)
        self.cfg = make_cfg(work, os.path.join(self.tmp.name, "logs"))
        self.events = []

    def tearDown(self):
        self.tmp.cleanup()

    def controller(self, replies):
        self.fake = FakeClient(replies)
        ctl = Controller(self.cfg, lambda *_: self.fake)
        q = ctl.subscribe()
        def collect():
            while True:
                e = q.get()
                if "seq" in e:   # log events; not the "conversations" list updates
                    self.events.append(e)
        threading.Thread(target=collect, daemon=True).start()
        return ctl

    def kinds(self):
        return [e["kind"] for e in self.events]


class ControllerTest(ServiceBase):
    def test_task_with_approval(self):
        ctl = self.controller([
            reply(ToolUseBlock(type="tool_use", id="c1", name="run_command", input={"command": "mkdir -p old/sub && rm -r old && echo removed"})),
            reply(TextBlock(type="text", text="All done."), stop="end_turn"),
        ])
        ctl.start_task("clean up")   # rm -r matches an ask rule (make_cfg)
        self.assertTrue(wait_for(lambda: ctl.state == "waiting"))
        pending = ctl.snapshot()["pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["call"], "c1")
        ctl.decide(pending[0]["id"], "allow")
        self.assertTrue(wait_for(lambda: ctl.state == "idle"))
        results = [e for e in self.events if e["kind"] == "result"]
        self.assertEqual(results[0]["text"], "exit code 0\nremoved")
        self.assertIn("approval_done", self.kinds())
        self.assertEqual(self.events[-1]["kind"], "status")
        seqs = [e["seq"] for e in self.events]
        self.assertEqual(seqs, sorted(seqs))

    def test_stop_while_waiting(self):
        ctl = self.controller([
            reply(ToolUseBlock(type="tool_use", id="c1", name="run_command", input={"command": "rm -r stuff"})),
        ])
        ctl.start_task("clean up")
        self.assertTrue(wait_for(lambda: ctl.state == "waiting"))
        ctl.stop()
        self.assertTrue(wait_for(lambda: ctl.state == "idle"))
        end = [e for e in self.events if e["kind"] == "end"][-1]
        self.assertEqual(end["reason"], "stopped by you")
        roles = [m["role"] for m in ctl.session.messages]
        self.assertEqual(roles, ["user", "assistant", "tool_results"])  # history stays valid

    def test_busy_and_new_session(self):
        ctl = self.controller([
            reply(ToolUseBlock(type="tool_use", id="c1", name="run_command", input={"command": "pip install x"})),
        ])
        first = ctl.log.name
        ctl.start_task("one")
        self.assertTrue(wait_for(lambda: ctl.state == "waiting"))
        with self.assertRaises(Exception):
            ctl.start_task("two")
        busy = ctl.current
        time.sleep(0.05)
        ctl.new_session()   # opens a second conversation; the first keeps working
        self.assertNotEqual(ctl.log.name, first)
        self.assertEqual(ctl.conv(busy).state, "waiting")
        self.assertEqual(ctl.state, "idle")
        ctl.stop(busy)
        self.assertTrue(wait_for(lambda: ctl.conv(busy).state == "idle"))
        runs = {r["name"]: r for r in ctl.history()}
        self.assertTrue(runs[ctl.log.name]["current"])
        self.assertEqual(runs[first]["conv"], busy)   # still open

    def test_api_error_keeps_service_up(self):
        ctl = Controller(self.cfg, lambda *_: (_ for _ in ()).throw(RuntimeError("network down")))
        ctl.start_task("anything")
        self.assertTrue(wait_for(lambda: ctl.state == "idle"))
        kinds = [e["kind"] for e in ctl.log.read()]
        self.assertIn("error", kinds)

    def test_files_view(self):
        ctl = self.controller([])
        Path(self.cfg.workdir, "notes.md").write_text("hello")
        os.makedirs(Path(self.cfg.workdir, "data"))
        listing = ctl.list_files(".")
        self.assertEqual([e["name"] for e in listing["entries"]], ["data", "notes.md"])
        self.assertTrue(listing["entries"][0]["dir"])
        self.assertEqual(ctl.read_file("notes.md")["text"], "hello")


class ApiTest(ServiceBase):
    def test_access_token(self):
        ctl = self.controller([reply(TextBlock(type="text", text="Hi."), stop="end_turn")])
        app = create_app(ctl, "secret-token")
        c = TestClient(app)
        self.assertEqual(c.get("/api/state").status_code, 401)
        self.assertEqual(c.get("/?token=wrong", follow_redirects=False).status_code, 401)
        # The right key: a page (not a redirect) that sets the Strict cookie and moves on by itself,
        # keeping the part after "#". Through the PC helper's link a redirect would lose the cookie
        # (the browser counts the whole chain as cross-site), and the Sec-Fetch-Site header can't be
        # relied on to spot that (browsers send it to HTTPS only).
        for headers in ({}, {"Sec-Fetch-Site": "cross-site"}):
            r = TestClient(app).get("/?token=secret-token", headers=headers, follow_redirects=False)
            self.assertEqual(r.status_code, 200, headers)
            self.assertIn("uvmc_token", r.headers["set-cookie"])
            self.assertIn("samesite=strict", r.headers["set-cookie"].lower())
            self.assertIn('location.replace("/" + location.hash)', r.text)
            self.assertNotIn("secret-token", r.text)
            self.assertEqual(TestClient(app).get("/?token=wrong", headers=headers).status_code, 401)
        c.get("/?token=secret-token")   # signs this client in (the cookie)
        # Back to the same page (deep links), but never to another site: "//host", "/\host" and tabs
        # or newlines (which browsers drop) all end up as a page of the panel.
        # (An encoded backslash or tab, %5C or %09, is an ordinary character for browsers, not a slash.)
        def a_panel_page(loc):
            return loc.startswith("/") and loc[1:2] not in ("/", "\\") and not any(c in loc for c in "\t\n\r")
        for asked, lands in [("/docs", "/docs"), ("//evil.example/x", "/evil.example/x"), ("///evil.example/", "/evil.example/"),
                             ("/%5Cevil.example/", None), ("/%5C%5Cevil.example/", None), ("/%09/evil.example/", None),
                             ("/%0A%0D//evil.example/", None), ("/a%3Cb%3E", None), ('/a%22b', None)]:
            x = TestClient(app).get(f"http://testserver{asked}?token=secret-token", follow_redirects=False)
            self.assertEqual(x.status_code, 200, asked)
            link = x.text.split('<noscript><a href="')[1].split('"')[0]
            self.assertTrue(a_panel_page(link), (asked, link))   # the no-script link: the same rule
            script = x.text.split("<script>")[1].split("</script>")[0]
            target = json.loads(script[len("location.replace("):script.index(" + location.hash)")])
            self.assertTrue(a_panel_page(target), (asked, target))
            self.assertNotIn("<", script, asked)   # nothing that could end the script early
            if lands:
                self.assertEqual(target, lands, asked)
        # And when the path arrives decoded: literal backslashes, tabs and newlines.
        from uvmc.service import signin_target
        for raw, want in [("/\\evil.example/", "/evil.example/"), ("/\t/evil.example/", "/evil.example/"),
                          ("/\\/\\evil.example/", "/evil.example/"), ("/\n\r//evil.example/x", "/evil.example/x"),
                          ("", "/"), (None, "/"), ("/reports/7", "/reports/7")]:
            self.assertEqual(signin_target(raw), want, raw)
        self.assertEqual(c.get("/api/state").json()["state"], "idle")
        self.assertIn("UVMC", c.get("/").text)
        self.assertEqual(c.post("/api/task", content="text=hi").status_code, 415)  # JSON only
        self.assertEqual(c.post("/api/task", json={"text": "say hi"}).status_code, 200)
        self.assertTrue(wait_for(lambda: ctl.state == "idle" and "end" in self.kinds()))
        self.assertEqual(c.post("/api/approve", json={"id": "nope", "allow": True}).status_code, 404)
        self.assertEqual(c.get("/api/history/../../etc/passwd").status_code, 404)
        self.assertEqual(len(c.get("/api/history").json()), 1)
        self.assertIn("cpu_percent", c.get("/api/machine").json())
        # Scripts: the token as a bearer header, no cookie
        s = TestClient(app)
        self.assertEqual(s.get("/api/state", headers={"Authorization": "Bearer wrong"}).status_code, 401)
        self.assertEqual(s.get("/api/state", headers={"Authorization": "Bearer secret-token"}).json()["state"], "idle")

    def test_no_token_configured(self):
        app = create_app(self.controller([]), None)
        self.assertEqual(TestClient(app).get("/").status_code, 503)


if __name__ == "__main__":
    unittest.main()
