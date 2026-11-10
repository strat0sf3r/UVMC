"""The lock (uvmc/lock.py): the VM closed to the PC, only the model on the PC, a PIN both ways.

Outside the VM the lock's state is kept in memory; setup.sh's SSH side (uvmc-lock) is checked
in the VM by hand (guide.md, "The lock").
"""

import json
import os
import sys
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
from uvmc.service import Busy, create_app  # noqa: E402

H = {"Authorization": "Bearer t"}


def say(text):
    return reply(TextBlock(type="text", text=text), stop="end_turn")


def run(cid, command):
    return reply(ToolUseBlock(type="tool_use", id=cid, name="run_command", input={"command": command}))


class LockTest(ServiceBase):
    def test_pin_both_ways(self):
        lock = Lock(self.cfg.state_dir)
        self.assertFalse(lock.locked)
        with self.assertRaises(LockError):   # nothing to unlock with yet
            lock.set(False, "1234")
        with self.assertRaises(LockError):   # too short
            lock.set(True, "12")
        lock.set(True, "1234")               # the first lock sets the PIN
        self.assertTrue(lock.locked and lock.pin_set)
        stored = json.loads((self.cfg.state_dir / "lock.json").read_text())
        self.assertNotIn("1234", json.dumps(stored))   # only a hash
        with self.assertRaises(LockError):
            lock.set(False, "9999")
        self.assertTrue(lock.locked)
        lock.set(False, "1234")
        self.assertFalse(lock.locked)
        with self.assertRaises(LockError):   # locking needs it too, once set
            lock.set(True, "0000")
        self.assertFalse(lock.locked)

    def test_wrong_pins_slow_down(self):
        lock = Lock(self.cfg.state_dir)
        lock.set(True, "1234")
        for _ in range(3):
            with self.assertRaises(LockError):
                lock.set(False, "0000")
        with self.assertRaises(LockError) as e:   # even the right one waits now
            lock.set(False, "1234")
        self.assertIn("Too many", str(e.exception))
        self.assertGreater(lock.view()["wait"], 0)
        lock.wait_until = 0   # the wait is over
        lock.set(False, "1234")
        self.assertFalse(lock.locked)

    def test_restart_releases_it(self):
        lock = Lock(self.cfg.state_dir)
        lock.set(True, "1234")
        again = Lock(self.cfg.state_dir)   # the VM restarted: its key file is full again
        self.assertFalse(again.locked)
        self.assertTrue(again.released_by_restart)
        self.assertFalse(json.loads((self.cfg.state_dir / "lock.json").read_text())["locked"])
        self.assertTrue(again.pin_set)    # the PIN stays
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
        ctl.set_lock(True, "1234")
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
        ctl.set_lock(False, "1234")
        self.assertTrue(ctl.model_allowed(ctl.cfg.profile("claude")))

    def test_locking_stops_other_models(self):
        ctl = self.controller([run("c1", "sleep 2"), say("Done.")])
        online = ctl.new_conversation("claude")
        ctl.start_task("wait a bit", online.id)
        self.assertTrue(wait_for(lambda: online.state == "running"))
        ctl.set_lock(True, "1234")
        self.assertTrue(wait_for(lambda: online.state == "idle", timeout=10))
        notices = [r.get("text", "") for r in online.log.read() if r["kind"] == "notice"]
        self.assertTrue(any("Stopped: UVMC was locked" in t for t in notices))
        self.assertTrue(any("UVMC is locked" in t for t in notices))

    # --- the panel's API -------------------------------------------------------------------------

    def test_api(self):
        ctl = self.controller([])
        client = TestClient(create_app(ctl, "t"))
        r = client.get("/api/lock", headers=H)
        self.assertEqual(r.json(), {"available": True, "locked": False, "pin_set": False,
                                    "released_by_restart": False, "wait": 0})
        r = client.post("/api/lock", json={"on": True, "pin": "2468"}, headers=H)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["locked"])
        self.assertTrue(client.get("/api/state", headers=H).json()["lock"]["locked"])
        r = client.post("/api/lock", json={"on": False, "pin": "1111"}, headers=H)
        self.assertEqual(r.status_code, 403)
        self.assertIn("Wrong PIN", r.json()["error"])
        r = client.post("/api/lock", json={"on": False, "pin": "2468"}, headers=H)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["locked"])
        self.assertEqual(client.post("/api/lock/seen", json={}, headers=H).status_code, 200)


if __name__ == "__main__":
    unittest.main()
