"""Deleting from the panel: a past run in Runs, a file in a conversation's copy or the main folder."""

import os
import sys
import unittest
from pathlib import Path

os.environ["UVMC_NO_SUDO"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient  # noqa: E402

import test_conversations  # noqa: E402  (the module, so its tests aren't collected here a second time)
from test_conversations import say  # noqa: E402
from test_service import ServiceBase, wait_for  # noqa: E402
from uvmc.service import Busy, create_app  # noqa: E402

H = {"Authorization": "Bearer t"}


class DeleteTest(ServiceBase):
    controller = test_conversations.ConversationsTest.controller

    def setUp(self):
        super().setUp()
        self.shared = Path(self.cfg.workdir)
        (self.shared / "notes.txt").write_text("one\n")
        (self.shared / "old").mkdir()
        (self.shared / "old" / "a.txt").write_text("a\n")

    def test_delete_a_closed_run(self):
        ctl = self.controller({"alpha": [say("Hi.")]})
        c = TestClient(create_app(ctl, "t"))
        a = ctl.conv()
        b = ctl.new_conversation()
        ctl.start_task("alpha", b.id)
        self.assertTrue(wait_for(lambda: not ctl.anything_running()))
        name = b.log.name
        self.assertIn(name, [r["name"] for r in ctl.history()])
        r = c.post("/api/history/delete", headers=H, json={"name": name})
        self.assertEqual(r.status_code, 409)                                  # still open: close it first
        ctl.close_conversation(b.id)
        self.assertEqual(c.post("/api/history/delete", headers=H, json={"name": name}).json(), {"ok": True})
        self.assertNotIn(name, [r["name"] for r in ctl.history()])
        self.assertFalse((self.cfg.log_dir / f"{name}.jsonl").exists())
        self.assertEqual(c.get(f"/api/history/{name}", headers=H).status_code, 404)
        for bad in ("../x", name, "", a.log.name):                         # gone, outside, nothing, open
            self.assertIn(c.post("/api/history/delete", headers=H, json={"name": bad}).status_code, (404, 409))
        self.assertTrue((self.cfg.log_dir / f"{a.log.name}.jsonl").exists())

    def test_delete_in_a_copy_is_a_change_to_bring_in(self):
        ctl = self.controller({})
        a = ctl.conv()
        ctl.ensure_copy(a)
        copy = Path(a.copy_dir)
        r = ctl.delete_file("notes.txt", a.id, "copy")
        self.assertEqual((r["path"], r["where"], r["undo"]), ("notes.txt", "copy", None))
        self.assertFalse((copy / "notes.txt").exists())
        self.assertTrue((self.shared / "notes.txt").exists())                 # the main folder waits for the bring-in
        ctl.delete_file(str(copy / "old"), a.id, "copy")                       # a folder, by its full path
        self.assertFalse((copy / "old").exists())
        removed = sorted(x["path"] for x in ctl.changes(a.id)["mine"])
        self.assertEqual(removed, ["notes.txt", "old/a.txt"])
        ctl.sync(a.id)
        self.assertFalse((self.shared / "notes.txt").exists())

    def test_delete_refuses_outside_and_uvmc_folders(self):
        ctl = self.controller({})
        a = ctl.conv()
        ctl.ensure_copy(a)
        copy = Path(a.copy_dir)
        (copy / ".uvmc-merge").mkdir()
        outside = Path(self.cfg.workdir).parent / "outside.txt"
        outside.write_text("keep\n")
        os.symlink(str(outside.parent), str(copy / "link"))
        for path in ("..", "../outside.txt", str(outside), ".", "", ".uvmc-merge", "link/outside.txt"):
            with self.assertRaises((ValueError, FileNotFoundError), msg=path):
                ctl.delete_file(path, a.id, "copy")
        self.assertTrue(outside.exists())
        ctl.delete_file("link", a.id, "copy")                                 # the link goes, not what it points to
        self.assertTrue(outside.exists())
        with self.assertRaises(FileNotFoundError):
            ctl.delete_file("nothing.txt", a.id, "copy")
        a.state = "running"
        with self.assertRaises(Busy):
            ctl.delete_file("notes.txt", a.id, "copy")
        a.state = "idle"

    def test_delete_in_the_main_folder_can_be_undone(self):
        ctl = self.controller({})
        c = TestClient(create_app(ctl, "t"))
        r = c.post("/api/files/delete", headers=H, json={"path": "old", "where": "shared"}).json()
        self.assertEqual(r["where"], "shared")
        self.assertTrue(r["undo"])
        self.assertFalse((self.shared / "old").exists())
        self.assertEqual(c.post("/api/files/undo-delete", headers=H, json={"checkpoint": r["undo"]}).json(), {"ok": True})
        self.assertEqual((self.shared / "old" / "a.txt").read_text(), "a\n")
        self.assertEqual(c.post("/api/files/delete", headers=H, json={"path": "../x", "where": "shared"}).status_code, 400)
        self.assertEqual(c.post("/api/files/delete", headers=H, json={"path": "gone", "where": "shared"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
