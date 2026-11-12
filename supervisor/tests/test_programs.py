"""Programs that use UVMC with a key of their own (uvmc/programs.py), their routes and walls
(service.py), and the client library (client/python/uvmc_client.py)."""

import base64
import json
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
sys.path.insert(0, str(ROOT.parent / "client" / "python"))

from fastapi.testclient import TestClient  # noqa: E402

from test_service import ServiceBase, wait_for  # noqa: E402
from uvmc.programs import ProgramError, Programs, split_key  # noqa: E402
from uvmc.service import create_app  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
PANEL = {"Authorization": "Bearer t"}


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def report(**extra):
    return json.dumps({"title": "Queue", "status": {"state": "ok", "text": "3 running"},
                       "blocks": [{"type": "stats", "items": [{"label": "Running", "value": "3"}]},
                                  {"type": "text", "md": "fine"}], **extra}).encode()


class ProgramsTest(unittest.TestCase):
    """The registry itself, without HTTP."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = type("Cfg", (), {"state_dir": Path(self.tmp.name), "made_tools_keep_runs": 3})()
        self.events = []
        self.p = Programs(self.cfg, on_report=lambda n, r: self.events.append(("report", n, r)),
                          on_silent=lambda n, i: self.events.append(("silent", n, i)))

    def tearDown(self):
        self.tmp.cleanup()

    def test_key_shown_once_only_its_hash_kept(self):
        key = self.p.add("dispatcher", "Runs the queue")
        kid, secret = split_key(key)
        stored = (Path(self.tmp.name) / "programs" / "registry.json").read_text()
        self.assertNotIn(secret, stored)
        self.assertIn(kid, stored)
        self.assertEqual(self.p.identify(key), "dispatcher")
        self.assertNotIn("key_hash", json.dumps(self.p.view()))
        self.assertEqual(oct((Path(self.tmp.name) / "programs").stat().st_mode & 0o777), "0o700")

    def test_wrong_and_old_keys(self):
        key = self.p.add("dispatcher")
        self.assertIsNone(self.p.identify(key[:-1] + ("A" if key[-1] != "A" else "B")))
        self.assertIsNone(self.p.identify("t"))
        self.assertIsNone(self.p.identify("uvmcp_short_x"))
        new = self.p.new_key("dispatcher")
        self.assertIsNone(self.p.identify(key))
        self.assertEqual(self.p.identify(new), "dispatcher")

    def test_names(self):
        for bad in ("", "A", "x", "Has Space", "self", "1abc", "a" * 40, "../etc"):
            with self.assertRaises(ProgramError, msg=bad):
                self.p.add(bad)
        self.p.add("ok-name")
        with self.assertRaises(ProgramError):
            self.p.add("ok-name")

    def test_update_checks_its_values(self):
        self.p.add("dispatcher")
        with self.assertRaises(ProgramError):
            self.p.update("dispatcher", permissions=["reports", "root"])
        with self.assertRaises(ProgramError):
            self.p.update("dispatcher", silent_after=-1)
        with self.assertRaises(ProgramError):
            self.p.update("dispatcher", notify="loud")
        self.p.update("dispatcher", permissions=[], silent_after=15, notify="always", description="d")
        v = self.p.view("dispatcher")
        self.assertEqual((v["permissions"], v["silent_after"], v["notify"], v["description"]), ([], 15, "always", "d"))
        with self.assertRaises(KeyError):
            self.p.update("nobody", notify="never")

    def test_reports_kept_checked_and_dropped(self):
        self.p.add("dispatcher")
        for i in range(5):
            got = self.p.receive_report("dispatcher", report(title=f"r{i}"))
            self.assertEqual(got["problems"], [])
        v = self.p.view("dispatcher")
        self.assertEqual([r["title"] for r in v["reports"]], ["r4", "r3", "r2"])   # keep = 3
        self.assertEqual(v["unseen"], 3)
        files = {f.name.split(".")[0] for f in (Path(self.tmp.name) / "programs" / "dispatcher").iterdir()}
        self.assertEqual(files, {r["id"] for r in v["reports"]})
        res = self.p.result("dispatcher", v["reports"][0]["id"])
        self.assertEqual(res["report"]["status"]["text"], "3 running")
        self.assertEqual(self.events[-1][0], "report")
        self.p.mark_seen("dispatcher", v["reports"][0]["id"])
        self.assertEqual(self.p.view("dispatcher")["unseen"], 0)

    def test_inline_images(self):
        self.p.add("dispatcher")
        body = report(blocks=[{"type": "image", "data": b64(PNG), "alt": "plot"},
                              {"type": "image", "data": b64(SVG)},
                              {"type": "image", "data": "not base64!"},
                              {"type": "image", "file": "plot.png"}])
        got = self.p.receive_report("dispatcher", body)
        joined = " ".join(got["problems"])
        self.assertIn("not a PNG", joined)          # the SVG, by its bytes
        self.assertIn("not valid base64", joined)
        self.assertIn("base64", joined)             # a file name means nothing from a program
        blocks = self.p.result("dispatcher", got["id"])["report"]["blocks"]
        self.assertEqual(blocks[0]["n"], 1)
        self.assertIsNone(blocks[1]["n"])
        data, kind = self.p.image("dispatcher", got["id"], 1)
        self.assertEqual((data, kind), (PNG, "image/png"))
        with self.assertRaises(KeyError):
            self.p.image("dispatcher", got["id"], 2)
        with self.assertRaises(KeyError):
            self.p.image("dispatcher", "../../registry", 1)

    def test_too_many_images_and_empty_reports(self):
        self.p.add("dispatcher")
        got = self.p.receive_report("dispatcher", report(blocks=[{"type": "image", "data": b64(PNG)}] * 12))
        self.assertIn("at most 10 images", " ".join(got["problems"]))
        with self.assertRaises(ProgramError):
            self.p.receive_report("dispatcher", b"{}")
        with self.assertRaises(ProgramError):
            self.p.receive_report("dispatcher", b"not json")

    def test_rate_limit(self):
        self.p.add("dispatcher")
        for _ in range(30):
            self.p.receive_report("dispatcher", report())
        with self.assertRaises(ProgramError) as e:
            self.p.receive_report("dispatcher", report())
        self.assertIn("a minute", str(e.exception))

    def test_silent_and_back(self):
        self.p.add("dispatcher")
        self.p.update("dispatcher", silent_after=10)
        self.assertEqual(self.p.check_silent(), [])           # never called yet: nothing to miss
        self.p.called("dispatcher", "/api/programs/self/heartbeat", 200)
        self.assertEqual(self.p.check_silent(time.time() + 5 * 60), [])
        self.assertEqual(self.p.check_silent(time.time() + 11 * 60), ["dispatcher"])
        self.assertEqual(self.p.check_silent(time.time() + 20 * 60), [])   # told once
        self.assertTrue(self.p.view("dispatcher")["silent"])
        self.assertEqual(self.events[-1][:2], ("silent", "dispatcher"))
        self.assertFalse(self.events[-1][2]["back"])
        self.p.called("dispatcher", "/api/programs/self/heartbeat", 200)
        self.assertTrue(self.events[-1][2]["back"])
        self.assertFalse(self.p.view("dispatcher")["silent"])
        self.assertEqual(self.p.view("dispatcher")["calls"][0]["route"], "/api/programs/self/heartbeat")

    def test_remove(self):
        self.p.add("dispatcher")
        key = self.p.new_key("dispatcher")
        self.p.receive_report("dispatcher", report())
        self.p.remove("dispatcher")
        self.assertIsNone(self.p.identify(key))
        self.assertFalse((Path(self.tmp.name) / "programs" / "dispatcher").exists())
        self.assertEqual(Programs(self.cfg).view(), [])     # gone from the file too


class ProgramRoutesTest(ServiceBase):
    """The walls: a program key reaches only /api/programs/self/..., which takes nothing else."""

    def setUp(self):
        super().setUp()
        self.ctl = self.controller([])
        self.app = create_app(self.ctl, "t")
        self.c = TestClient(self.app)
        r = self.c.post("/api/programs/add", json={"name": "dispatcher", "description": "Runs the queue"},
                        headers=PANEL)
        self.assertEqual(r.status_code, 200, r.text)
        self.key = r.json()["key"]
        self.H = {"Authorization": f"Bearer {self.key}"}

    def test_a_program_key_reaches_nothing_else(self):
        tried = 0
        for route in self.app.routes:
            path = getattr(route, "path", "")
            if path.startswith("/api/programs/self"):
                continue
            url = path.replace("{", "").replace("}", "") or "/"
            for method in sorted(getattr(route, "methods", None) or {"GET"}):
                if method == "HEAD":
                    continue
                r = self.c.request(method, url, headers=self.H, json={})
                self.assertEqual(r.status_code, 403, f"{method} {url}: {r.status_code}")
                tried += 1
        self.assertGreater(tried, 50)
        self.assertEqual(self.c.get("/?token=" + self.key).status_code, 401)   # not as the panel's token

    def test_program_routes_take_only_program_keys(self):
        for headers in (PANEL, {}, {"Cookie": "uvmc_token=t"}):
            self.assertEqual(self.c.get("/api/programs/self", headers=headers).status_code, 403)
            self.assertEqual(self.c.post("/api/programs/self/reports", content=report(),
                                         headers={**headers, "Content-Type": "application/json"}).status_code, 403)
        self.assertEqual(self.c.get("/api/programs/self", headers={"Authorization": "Bearer uvmcp_abcdefgh_" + "x" * 43}).status_code, 401)

    def test_whoami_report_and_panel(self):
        self.drained()   # listen from here on
        r = self.c.get("/api/programs/self", headers=self.H)
        self.assertEqual(r.json()["name"], "dispatcher")
        self.assertEqual(r.json()["permissions"], ["reports"])
        body = report(blocks=[{"type": "image", "data": b64(PNG)}], notify="job x failed")
        r = self.c.post("/api/programs/self/reports", content=body, headers={**self.H, "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 200, r.text)
        rid = r.json()["id"]
        self.assertTrue(wait_for(lambda: any(e.get("kind") == "program_report" for e in self.drained())))
        v = self.c.get("/api/programs/dispatcher", headers=PANEL).json()
        self.assertEqual(v["reports"][0]["id"], rid)
        self.assertEqual(v["calls"][0]["route"], "/api/programs/self/reports")
        res = self.c.get(f"/api/programs/dispatcher/reports/{rid}", headers=PANEL).json()
        self.assertEqual(res["report"]["notify"], "job x failed")
        img = self.c.get(f"/api/programs/dispatcher/reports/{rid}/images/1", headers=PANEL)
        self.assertEqual(img.headers["content-type"], "image/png")
        self.assertIn("sandbox", img.headers["content-security-policy"])
        # the panel's list never shows the key or its hash
        self.assertNotIn(split_key(self.key)[1], json.dumps(self.c.get("/api/programs", headers=PANEL).json()))

    def drained(self):
        if not hasattr(self, "_q"):
            self._q, self._seen = self.ctl.subscribe(), []
        while not self._q.empty():
            self._seen.append(self._q.get())
        return self._seen

    def test_permissions_size_and_type(self):
        self.drained()
        self.c.post("/api/programs/update", json={"name": "dispatcher", "permissions": []}, headers=PANEL)
        r = self.c.post("/api/programs/self/reports", content=report(), headers={**self.H, "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 403)
        self.c.post("/api/programs/update", json={"name": "dispatcher", "permissions": ["reports"]}, headers=PANEL)
        r = self.c.post("/api/programs/self/reports", content=report(), headers={**self.H, "Content-Type": "text/plain"})
        self.assertEqual(r.status_code, 415)
        big = json.dumps({"title": "x", "blocks": [{"type": "code", "text": "a" * 8_300_000}]}).encode()
        r = self.c.post("/api/programs/self/reports", content=big, headers={**self.H, "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 413)
        r = self.c.post("/api/programs/self/reports", content=b"{}", headers={**self.H, "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 422)

    def test_refused_while_locked(self):
        self.ctl.vm_lock.set(True)
        try:
            self.assertEqual(self.c.post("/api/programs/self/heartbeat", json={}, headers=self.H).status_code, 423)
        finally:
            self.ctl.vm_lock.set(False)
        self.assertEqual(self.c.post("/api/programs/self/heartbeat", json={}, headers=self.H).status_code, 200)

    def test_new_key_and_remove(self):
        new = self.c.post("/api/programs/key", json={"name": "dispatcher"}, headers=PANEL).json()["key"]
        self.assertEqual(self.c.get("/api/programs/self", headers=self.H).status_code, 401)
        self.assertEqual(self.c.get("/api/programs/self", headers={"Authorization": f"Bearer {new}"}).status_code, 200)
        self.c.post("/api/programs/remove", json={"name": "dispatcher"}, headers=PANEL)
        self.assertEqual(self.c.get("/api/programs/self", headers={"Authorization": f"Bearer {new}"}).status_code, 401)
        self.assertEqual(self.c.get("/api/programs/dispatcher", headers=PANEL).status_code, 404)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ClientTest(ServiceBase):
    """client/python/uvmc_client.py against a real server, and without one."""

    def setUp(self):
        super().setUp()
        import uvicorn
        self.ctl = self.controller([])
        app = create_app(self.ctl, "t")
        self.port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        threading.Thread(target=self.server.run, daemon=True).start()
        self.assertTrue(wait_for(lambda: self.server.started))
        self.key = self.ctl.programs.add("dispatcher")
        self.data = Path(self.tmp.name) / "data"
        for k in ("UVMC_URL", "UVMC_KEY", "UVMC_KEY_FILE", "UVMC_REPORT", "UVMC_DATA", "CREDENTIALS_DIRECTORY"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.server.should_exit = True
        time.sleep(0.2)
        super().tearDown()

    def client(self, **kw):
        from uvmc_client import Uvmc
        return Uvmc(**{"url": f"http://127.0.0.1:{self.port}", "key": self.key, "data": str(self.data), **kw})

    def test_report_to_uvmc(self):
        u = self.client()
        self.assertEqual(u.whoami()["name"], "dispatcher")
        got = u.report("Queue", ("warn", "1 failed"), [u.stats({"Running": 3}), u.table(["Job"], [["a"]]),
                                                       u.section("More", [u.image(PNG, "plot")])],
                       notify="job a failed")
        self.assertIsNotNone(got, u.last_error)
        res = self.ctl.programs.result("dispatcher", got["id"])
        self.assertEqual(res["report"]["status"]["state"], "warn")
        self.assertEqual(res["report"]["blocks"][2]["blocks"][0]["n"], 1)
        self.assertTrue(u.heartbeat())

    def test_kept_while_locked_then_sent(self):
        u = self.client()
        self.ctl.vm_lock.set(True)
        self.assertIsNone(u.report("Queue", ("ok", "fine")))
        self.assertIn("423", u.last_error)
        self.assertTrue((self.data / "uvmc-pending.json").exists())
        self.ctl.vm_lock.set(False)
        self.assertTrue(u.heartbeat())
        self.assertFalse((self.data / "uvmc-pending.json").exists())
        self.assertEqual(self.ctl.programs.view("dispatcher")["last_report"]["title"], "Queue")

    def test_unreachable_never_raises(self):
        u = self.client(url=f"http://127.0.0.1:{free_port()}", timeout=1)
        self.assertIsNone(u.report("Queue"))
        self.assertIn("can't be reached", u.last_error)
        self.assertTrue((self.data / "uvmc-pending.json").exists())
        self.assertFalse(u.heartbeat())

    def test_wrong_key_is_not_kept(self):
        u = self.client(key="uvmcp_abcdefgh_" + "x" * 43)
        self.assertIsNone(u.report("Queue"))
        self.assertIn("401", u.last_error)
        self.assertFalse((self.data / "uvmc-pending.json").exists())

    def test_key_from_a_file(self):
        f = Path(self.tmp.name) / "key"
        f.write_text(self.key + "\n")
        os.environ["UVMC_KEY_FILE"] = str(f)
        try:
            from uvmc_client import Uvmc
            u = Uvmc(url=f"http://127.0.0.1:{self.port}", data=str(self.data))
            self.assertEqual(u.whoami()["name"], "dispatcher")
        finally:
            os.environ.pop("UVMC_KEY_FILE")

    def test_local_fallback(self):
        from uvmc_client import Uvmc
        u = Uvmc(url="", key="", data=str(self.data))
        self.assertEqual(u.mode, "local")
        got = u.report("Queue", ("ok", "fine"), [u.image(PNG)])
        latest = json.loads(Path(got["file"]).read_text())
        self.assertEqual(latest["title"], "Queue")
        img = latest["blocks"][0]["file"]
        self.assertTrue((self.data / "reports" / img).exists())
        self.assertTrue(u.heartbeat())   # nothing to tell, nothing fails

    def test_as_a_tool(self):
        from uvmc import reports
        from uvmc_client import Uvmc
        home = Path(self.tmp.name) / "toolhome"
        home.mkdir()
        os.environ["UVMC_REPORT"] = str(home / "uvmc-report.json")
        try:
            u = Uvmc(url="", key="")
            self.assertEqual(u.mode, "tool")
            u.report("Queue", ("ok", "fine"), [u.image(PNG, "plot")])
        finally:
            os.environ.pop("UVMC_REPORT")
        rep, problems = reports.parse((home / "uvmc-report.json").read_bytes())   # as UVMC reads a tool's
        self.assertEqual(problems, [])
        self.assertTrue((home / rep["images"][0]).exists())


if __name__ == "__main__":
    unittest.main()
