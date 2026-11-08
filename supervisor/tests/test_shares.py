"""Offline tests for shared folders: read access, approved writes, backups, restore.

Run from the supervisor folder:  UVMC_NO_SUDO=1 python3 -m unittest discover tests
(The real read-only view is a mount inside the VM; here the "computer's folder" is a temp folder.)
"""

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

from test_service import ServiceBase, wait_for  # noqa: E402
from test_supervisor import FakeClient, reply  # noqa: E402
from uvmc.service import Controller  # noqa: E402
from uvmc.shares import ShareError, Shares  # noqa: E402


def tool(cid, name, **args):
    return ToolUseBlock(type="tool_use", id=cid, name=name, input=args)


class SharesBase(ServiceBase):
    def setUp(self):
        super().setUp()
        self.host = Path(self.tmp.name) / "host"           # stands in for /srv/uvmc-host
        self.folder = self.host / "testdata"                # the folder on "your computer"
        (self.folder / "config").mkdir(parents=True)
        (self.folder / "config" / "settings.json").write_text('{\n  "max_statues": 8\n}\n')
        (self.folder / "models").mkdir()
        (self.folder / "models" / "statue.bin").write_bytes(os.urandom(4096))

    def ctl(self, boss, worker=None):
        clients = {"claude": FakeClient(boss), "deepseek": FakeClient(worker or [])}
        ctl = Controller(self.cfg, lambda prof: clients[prof.name])
        ctl.shares = Shares(agent_home=self.tmp.name, rw_root=self.host)
        q = ctl.subscribe()
        threading.Thread(target=lambda: [self.events.append(q.get()) for _ in iter(int, 1)], daemon=True).start()
        return ctl

    def done(self, ctl):
        return wait_for(lambda: ctl.state == "idle" and any(e["kind"] == "end" and "parent" not in e for e in self.events), 10)


class SharedFileTest(SharesBase):
    def test_approved_change_is_written_with_backup_and_can_be_restored(self):
        ctl = self.ctl([
            reply(tool("w1", "write_file", path="settings.json", content='{\n  "max_statues": 12\n}\n')),
            reply(tool("u1", "update_shared_file", share="testdata", path="config/settings.json",
                       source="settings.json", reason="Raise max_statues to 12, as asked.")),
            reply(TextBlock(type="text", text="Updated."), stop="end_turn"),
        ])
        ctl.start_task("raise max_statues to 12")
        self.assertTrue(wait_for(lambda: ctl.state == "waiting"))
        card = ctl.snapshot()["pending"][0]
        self.assertEqual((card["type"], card["share"], card["path"], card["exists"]), ("file", "testdata", "config/settings.json", True))
        self.assertIn('-  "max_statues": 8', card["diff"])
        self.assertIn('+  "max_statues": 12', card["diff"])
        self.assertIn("8", (self.folder / "config" / "settings.json").read_text())  # nothing written before approval
        ctl.decide(card["id"], "allow")
        self.assertTrue(self.done(ctl))

        self.assertIn("12", (self.folder / "config" / "settings.json").read_text())
        upd = [e for e in self.events if e["kind"] == "file_update"][0]
        self.assertEqual(upd["call"], "u1")
        self.assertIn("8", (self.folder / upd["backup"]).read_text())
        result = [e for e in self.events if e["kind"] == "result" and e["id"] == "u1"][0]
        self.assertFalse(result["is_error"])
        self.assertIn("backed up", result["text"])

        ctl.restore_shared(upd["id"])
        self.assertIn("8", (self.folder / "config" / "settings.json").read_text())
        before_restore = list((self.folder / "_uvmc_backups").glob("*-before-restore/config/settings.json"))
        self.assertEqual(len(before_restore), 1)  # the agent's version was kept too

    def test_denied_change_writes_nothing(self):
        ctl = self.ctl([
            reply(tool("w1", "write_file", path="new.bin", content="x" * 100)),
            reply(tool("u1", "update_shared_file", share="testdata", path="models/statue.bin",
                       source="new.bin", reason="Replace it.")),
            reply(TextBlock(type="text", text="ok"), stop="end_turn"),
        ])
        original = (self.folder / "models" / "statue.bin").read_bytes()
        ctl.start_task("replace the statue")
        self.assertTrue(wait_for(lambda: ctl.state == "waiting"))
        card = ctl.snapshot()["pending"][0]
        self.assertNotIn("diff", card)  # binary: sizes only
        ctl.decide(card["id"], "deny")
        self.assertTrue(self.done(ctl))
        self.assertEqual((self.folder / "models" / "statue.bin").read_bytes(), original)
        self.assertFalse((self.folder / "_uvmc_backups").exists())
        result = [e for e in self.events if e["kind"] == "result" and e["id"] == "u1"][0]
        self.assertTrue(result["is_error"])
        self.assertIn("declined", result["text"])

    def test_new_file_and_unsafe_requests(self):
        ctl = self.ctl([
            reply(tool("w1", "write_file", path="marble.csv", content="name\nmarble\n")),
            reply(tool("u1", "update_shared_file", share="testdata", path="../outside.txt", source="marble.csv", reason="x"),
                  tool("u2", "update_shared_file", share="testdata", path="ok.txt", source="/etc/hostname", reason="x"),
                  tool("u3", "update_shared_file", share="other", path="a.txt", source="marble.csv", reason="x"),
                  tool("u4", "update_shared_file", share="testdata", path="textures/marble.csv", source="marble.csv", reason="Add marble.")),
            reply(TextBlock(type="text", text="ok"), stop="end_turn"),
        ])
        ctl.start_task("add a marble texture")
        self.assertTrue(wait_for(lambda: ctl.state == "waiting"))
        results = {e["id"]: e for e in self.events if e["kind"] == "result"}
        self.assertIn("not a valid path", results["u1"]["text"])
        self.assertIn("home or work folder", results["u2"]["text"])
        self.assertIn("no shared folder called 'other'", results["u3"]["text"])
        card = ctl.snapshot()["pending"][0]
        self.assertFalse(card["exists"])
        ctl.decide(card["id"], "allow")
        self.assertTrue(self.done(ctl))
        self.assertEqual((self.folder / "textures" / "marble.csv").read_text(), "name\nmarble\n")
        self.assertFalse((Path(self.tmp.name) / "host" / "outside.txt").exists())
        upd = [e for e in self.events if e["kind"] == "file_update"][0]
        self.assertIsNone(upd["backup"])
        ctl.restore_shared(upd["id"])  # undoing a new file removes it (a copy is kept)
        self.assertFalse((self.folder / "textures" / "marble.csv").exists())

    def test_who_gets_the_tool(self):
        ctl = self.ctl([reply(TextBlock(type="text", text="hi"), stop="end_turn")])
        ctl.start_task("hi")
        self.assertTrue(self.done(ctl))
        self.assertIn("update_shared_file", ctl.session.handlers)
        ctl.set_worker("deepseek")
        ctl._prepare_session()
        self.assertNotIn("update_shared_file", ctl.session.handlers)  # the coordinator can't write
        self.assertEqual(ctl.snapshot()["shares"][0]["name"], "testdata")

        empty = Controller(self.cfg, lambda prof: None)
        empty.shares = Shares(agent_home=self.tmp.name, rw_root=Path(self.tmp.name) / "nothing")
        empty._prepare_session()
        self.assertEqual(list(empty.session.handlers), ["propose_tool"])  # nothing shared, no file tool

    def test_target_checks(self):
        sh = Shares(agent_home=self.tmp.name, rw_root=self.host)
        for share, rel in [("testdata", ""), ("testdata", "_uvmc_backups/a"), ("testdata", "a/../../b"), ("Bad", "a")]:
            with self.assertRaises(ShareError):
                sh.target(share, rel)
        self.assertEqual(sh.target("testdata", "/config/settings.json"), (self.folder / "config" / "settings.json").resolve())


if __name__ == "__main__":
    unittest.main()
