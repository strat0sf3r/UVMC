"""The lock (uvmc/lock.py): the VM closed to the PC, only the model on the PC, no PIN:
locking gives the panel a new token instead, so programs holding the old one are out.

Outside the VM the lock's state is kept in memory; setup.sh's SSH side (uvmc-lock) is checked
in the VM by hand (guide.md, "The lock").
"""

import json
import os
import sys
import threading
import unittest
from pathlib import Path

os.environ["UVMC_NO_SUDO"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from anthropic.types import TextBlock, ToolUseBlock  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from test_service import ServiceBase, wait_for  # noqa: E402
from test_supervisor import reply  # noqa: E402
from uvmc import privacy  # noqa: E402
from uvmc.lock import Lock, LockError  # noqa: E402
from uvmc.service import Busy, PanelToken, create_app  # noqa: E402

H = {"Authorization": "Bearer t"}


def say(text):
    return reply(TextBlock(type="text", text=text), stop="end_turn")


def run(cid, command):
    return reply(ToolUseBlock(type="tool_use", id=cid, name="run_command", input={"command": command}))


class LockTest(ServiceBase):
    def test_lock_and_unlock(self):
        (self.cfg.state_dir).mkdir(parents=True, exist_ok=True)
        (self.cfg.state_dir / "lock.json").write_text(json.dumps({"pin": {"salt": "00", "hash": "11"}}))
        lock = Lock(self.cfg.state_dir)
        self.assertFalse(lock.locked)
        lock.set(True)
        self.assertTrue(lock.locked)
        stored = json.loads((self.cfg.state_dir / "lock.json").read_text())
        self.assertEqual(stored, {"locked": True})   # an old PIN is dropped
        lock.set(False)
        self.assertFalse(lock.locked)
        self.assertEqual(lock.view(), {"available": True, "locked": False, "released_by_restart": False})

    def test_restart_releases_it(self):
        lock = Lock(self.cfg.state_dir)
        lock.set(True)
        again = Lock(self.cfg.state_dir)   # the VM restarted: its key file is full again
        self.assertFalse(again.locked)
        self.assertTrue(again.released_by_restart)
        self.assertFalse(json.loads((self.cfg.state_dir / "lock.json").read_text())["locked"])
        again.forget_restart()
        self.assertFalse(again.view()["released_by_restart"])

    def test_pc_model(self):
        self.assertTrue(privacy.pc_model(self.cfg.profile("local")))
        for name in ("claude", "claude-plan", "deepseek"):
            self.assertFalse(privacy.pc_model(self.cfg.profile(name)))

    # --- only the model on the PC while locked ---------------------------------------------------

    def test_only_the_pc_model_works(self):
        ctl = self.controller([say("Hello from the PC.")])
        online = ctl.new_conversation("claude")
        local = ctl.new_conversation("local")
        ctl.set_lock(True)
        with self.assertRaises(Busy) as e:
            ctl.start_task("hello", online.id)
        self.assertIn("locked", str(e.exception))
        self.assertTrue({c["id"]: c for c in ctl.conversations()}[online.id]["waits_for_unlock"])
        self.assertFalse({c["id"]: c for c in ctl.conversations()}[local.id]["waits_for_unlock"])
        ctl.start_task("hello", local.id)
        self.assertTrue(wait_for(lambda: local.state == "idle"))
        self.assertEqual(local.session.last_text, "Hello from the PC.")
        text, err = ctl._delegate(local, {"instructions": "do it", "model": "claude"}, "c1")
        self.assertTrue(err)
        self.assertIn("locked", text)
        ctl.set_lock(False)
        self.assertTrue(ctl.model_allowed(ctl.cfg.profile("claude")))

    def test_locking_stops_other_models(self):
        ctl = self.controller([run("c1", "sleep 2"), say("Done.")])
        online = ctl.new_conversation("claude")
        ctl.start_task("wait a bit", online.id)
        self.assertTrue(wait_for(lambda: online.state == "running"))
        ctl.set_lock(True)
        self.assertTrue(wait_for(lambda: online.state == "idle", timeout=10))
        notices = [r.get("text", "") for r in online.log.read() if r["kind"] == "notice"]
        self.assertTrue(any("Stopped: UVMC was locked" in t for t in notices))
        self.assertTrue(any("UVMC is locked" in t for t in notices))

    # --- the panel's API -------------------------------------------------------------------------

    def test_api(self):
        ctl = self.controller([])
        client = TestClient(create_app(ctl, "t"))
        r = client.get("/api/lock", headers=H)
        self.assertEqual(r.json(), {"available": True, "locked": False, "released_by_restart": False})
        r = client.post("/api/lock", json={"on": True}, headers=H)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["locked"])
        new = client.cookies.get("uvmc_token")
        self.assertTrue(new and new != "t")
        self.assertTrue(client.get("/api/state").json()["lock"]["locked"])   # the new cookie works
        r = client.post("/api/lock", json={"on": False})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["locked"])
        self.assertEqual(client.post("/api/lock/seen", json={}).status_code, 200)

    def test_locking_shuts_out_the_old_token(self):
        ctl = self.controller([])
        client = TestClient(create_app(ctl, "t"))
        self.assertEqual(client.post("/api/lock", json={"on": True}, headers=H).status_code, 200)
        other = TestClient(client.app)   # the same app, without the locking panel's cookie
        for kw in ({"headers": H}, {"cookies": {"uvmc_token": "t"}}):   # a program, or a browser, with the old token
            other.cookies.clear()
            self.assertEqual(other.get("/api/lock", **kw).status_code, 401)
            self.assertEqual(other.post("/api/lock", json={"on": False}, **kw).status_code, 401)
        self.assertTrue(ctl.vm_lock.locked)
        self.assertEqual(client.get("/api/lock").status_code, 200)   # the panel that locked still can
        r = client.post("/api/lock", json={"on": False})
        self.assertFalse(r.json()["locked"])
        self.assertEqual(other.get("/api/lock", headers=H).status_code, 401)   # unlocking keeps the new one

    def test_token_file(self):
        f = self.cfg.state_dir / "panel_token"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("old")
        tok = PanelToken("old", f)
        new = tok.rotate()
        self.assertEqual(f.read_text(), new)
        self.assertEqual(tok.value, new)
        self.assertEqual(len(new), 40)
        self.assertTrue(new.isalnum())

    def test_event_stream_ends_on_lock(self):
        ctl = self.controller([])
        tok = PanelToken("t")
        client = TestClient(create_app(ctl, tok))
        lines = []

        def listen():
            with client.stream("GET", "/api/events", headers=H) as r:
                for line in r.iter_lines():
                    lines.append(line)

        th = threading.Thread(target=listen, daemon=True)
        th.start()
        self.assertTrue(wait_for(lambda: any(l.startswith("event: hello") for l in lines)))
        tok.rotate()
        ctl._broadcast({"kind": "lock"})   # wakes the stream, which sees the new token
        th.join(5)
        self.assertFalse(th.is_alive())


if __name__ == "__main__":
    unittest.main()
