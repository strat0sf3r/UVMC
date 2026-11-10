"""Private conversations (uvmc/private.py): the model on the PC only, no internet, erased when closed.

The marker test (test_nothing_left) is the check that keeps the rule in CLAUDE.md honest: a
private conversation goes through UVMC with a marker text, and after it is closed the marker
must not be anywhere UVMC writes. When you add a place that stores what a conversation says
or does, make this test go through it too.
"""

import json
import os
import socket
import sys
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

os.environ["UVMC_NO_SUDO"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from anthropic.types import TextBlock, ToolUseBlock  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from test_service import ServiceBase, wait_for  # noqa: E402
from test_supervisor import reply  # noqa: E402
from uvmc import audit, mcp_http, pc, private  # noqa: E402
from uvmc.netproxy import Proxy, SiteList  # noqa: E402
from uvmc.privacy import NeedsConfirm, PrivacyError  # noqa: E402
from uvmc.service import Busy, Unsaved, create_app  # noqa: E402

H = {"Authorization": "Bearer t"}


def say(text):
    return reply(TextBlock(type="text", text=text), stop="end_turn")


def run(cid, command):
    return reply(ToolUseBlock(type="tool_use", id=cid, name="run_command", input={"command": command}))


class PrivateTest(ServiceBase):
    def setUp(self):
        super().setUp()
        self.root = Path(self.tmp.name)
        self.tmpdir = Path(self.cfg.clean_tmp[0])
        self.tmpdir.mkdir()

    def where_is(self, marker: str) -> list[str]:
        """Every file under the test's folder that holds the marker (in its name or its bytes)."""
        found = []
        for dirpath, dirs, files in os.walk(self.root):
            for name in dirs + files:
                p = Path(dirpath) / name
                if marker in name:
                    found.append(str(p))
                elif p.is_file() and not p.is_symlink():
                    try:
                        if marker.encode() in p.read_bytes():
                            found.append(str(p))
                    except OSError:
                        pass
        return found

    def private_conv(self, ctl):
        return ctl.new_conversation(private=True)

    # --- who may run one ----------------------------------------------------------------------

    def test_only_the_model_on_the_pc(self):
        ctl = self.controller([])
        with self.assertRaises(PrivacyError):
            ctl.new_conversation("claude", private=True)
        with self.assertRaises(PrivacyError):
            ctl.new_conversation("deepseek", private=True)
        p = self.private_conv(ctl)   # no model named: the one on the PC
        self.assertEqual(p.model, "local")
        self.assertTrue(p.private)
        self.assertEqual(p.level, "local")
        self.assertEqual(p.seen, "local")
        with self.assertRaises(PrivacyError):
            ctl.set_model("claude", p.id)
        with self.assertRaises(PrivacyError):
            ctl.set_worker("claude", p.id)
        self.assertEqual(ctl.private_view()["models"], ["local"])

    def test_off_when_the_check_failed(self):
        ctl = self.controller([])
        ctl.private_check = {"state": "failed", "problems": [private.JOURNAL_FIX]}
        with self.assertRaises(PrivacyError) as e:
            self.private_conv(ctl)
        self.assertIn("system log", str(e.exception))
        ctl.private_check = {"state": "checking", "problems": []}
        with self.assertRaises(PrivacyError):
            self.private_conv(ctl)

    def test_self_check_outside_the_vm(self):
        self.assertEqual(private.self_check(self.cfg, "agent9"), [])
        rules = audit.quiet_rules(1015, True)
        self.assertEqual([r[-1] for r in rules], ["uid=1015"] * 4)
        self.assertEqual({r[4] for r in rules}, {"msgtype=EXECVE", "msgtype=PROCTITLE", "msgtype=CWD", "msgtype=PATH"})
        self.assertTrue(all(r[1] == "-a" for r in rules))
        self.assertTrue(all(r[1] == "-d" for r in audit.quiet_rules(1015, False)))

    # --- nothing left ---------------------------------------------------------------------------

    def test_nothing_left(self):
        marker = "MARKER-7f3a91"
        ctl = self.controller([
            run("c1", f"printf {marker} > secret-{marker}.txt && printf {marker} > {self.tmpdir}/t-{marker}"),
            say(f"Done: {marker}."),
            say(f"Summary: wrote the {marker} files."),   # compacting (compact.py)
        ])
        p = self.private_conv(ctl)
        ctl.net_recent.append({"host": f"{marker}.example", "conv": p.id})
        ctl.exec_recent.append({"args": [marker], "conv": p.id})
        ctl.start_task(f"Write the {marker} files", p.id)
        self.assertTrue(wait_for(lambda: p.state == "idle"))
        ctl.compact(p.id, "trim")
        with mock.patch.object(pc, "address", return_value=""):   # never the real PC's model server
            ctl.compact(p.id, "summary")
            self.assertTrue(wait_for(lambda: p.state == "idle"))
        self.assertTrue(p.session.messages[0].get("summary"))
        self.assertTrue(self.where_is(marker), "the test itself must put the marker somewhere")
        self.assertNotIn(p.log.name, [r["name"] for r in ctl.history()])   # never under Runs
        with self.assertRaises(NeedsConfirm):   # erased only once you confirm
            ctl.close_conversation(p.id, discard=True)
        ctl.close_conversation(p.id, discard=True, erase=True)
        self.assertEqual(self.where_is(marker), [])
        self.assertFalse(any(marker in json.dumps(x) for x in list(ctl.net_recent) + list(ctl.exec_recent)))
        self.assertEqual(ctl.private_left, [])

    def test_bringing_in_names_no_task(self):
        marker = "MARKER-b2c4"
        # General at local: a private conversation has seen local files, so a bring-in into a less
        # private place would be held for review (tested in test_privacy.py).
        self.cfg.privacy_general = "local"
        ctl = self.controller([run("c1", "printf hello > keep.txt"), say("Done.")])
        other = ctl.new_conversation()
        p = self.private_conv(ctl)
        ctl.start_task(f"{marker}: make keep.txt", p.id)
        self.assertTrue(wait_for(lambda: p.state == "idle"))
        with self.assertRaises(PrivacyError):   # its files stay its own...
            ctl.sync(p.id)
        with self.assertRaises(Unsaved) as e:   # ...until you close it: keep them or erase everything
            ctl.close_conversation(p.id)
        self.assertTrue(e.exception.sealed)
        ctl.keep_files(p.id)
        self.assertFalse(p.sealed)
        r = ctl.sync(p.id)
        self.assertIn("keep.txt", r["brought_in"])
        origins = json.loads((self.cfg.state_dir / "main_origins.json").read_text())
        self.assertEqual(origins["keep.txt"]["title"], "a private conversation")
        self.assertEqual(origins["keep.txt"]["tasks"], [])
        self.assertIsNone(origins["keep.txt"]["log"])
        notices = [r["text"] for r in other.log.read() if r["kind"] == "notice"]
        self.assertTrue(any("a private conversation" in t for t in notices))
        self.assertIn("a private conversation", ctl._describe(ctl._about(p)))
        ctl.close_conversation(p.id, erase=True)
        self.assertEqual(self.where_is(marker), [])
        self.assertEqual((Path(self.cfg.workdir) / "keep.txt").read_text(), "hello")   # brought in: it stays

    def test_restart_erases_it(self):
        ctl = self.controller([run("c1", "printf x > a.txt"), say("Done.")])
        p = self.private_conv(ctl)
        ctl.start_task("make a.txt", p.id)
        self.assertTrue(wait_for(lambda: p.state == "idle"))
        self.assertTrue(ctl.copies.exists(p.id))
        meta = json.loads((ctl.copies.state / p.id / "meta.json").read_text())
        self.assertEqual(meta, {"private": True, "slot": p.slot})   # no title, model or projects
        # A restart (without closing it): it doesn't come back, and nothing of it stays.
        ctl2 = self.controller([])
        self.assertNotIn(p.id, ctl2.convs)
        self.assertFalse(ctl2.copies.exists(p.id))
        self.assertFalse((ctl2.copies.state / p.id).exists())
        self.assertEqual(list(Path(self.cfg.private_log_dir).glob("*.jsonl")), [])

    def test_transcript_never_handed_on(self):
        ctl = self.controller([])
        p = self.private_conv(ctl)
        with self.assertRaises(PrivacyError):
            ctl.new_conversation("local", transcript_from=p.id)
        about = ctl._about(p)
        self.assertIsNone(about["log"])
        self.assertIsNone(ctl._hand_transcript(ctl.conv(), about))
        # A normal conversation's transcript may go into a private one.
        n = ctl.new_conversation("local")
        q = ctl.new_conversation(private=True, transcript_from=n.id)
        self.assertTrue(q.private)

    def test_not_closed_to_make_room(self):
        self.cfg.conv_max_open = 2
        ctl = self.controller([])
        first = ctl.conv()
        p = self.private_conv(ctl)
        ctl.focus(first.id)
        with self.assertRaises(Busy):
            ctl.new_conversation()
        self.assertIn(p.id, ctl.convs)

    def test_tasks_from_the_panel_only(self):
        ctl = self.controller([])
        p = self.private_conv(ctl)
        with self.assertRaises(Busy):
            ctl.run_and_wait("hello", cid=p.id)
        self.assertIsNone(mcp_http._conv(ctl, {"conversation": p.id}))
        ctl.mcp_conv = p.id
        ctl.focus(p.id)
        self.assertIsNone(mcp_http._conv(ctl, {}))

    def test_briefing_says_no_internet(self):
        ctl = self.controller([])
        p = self.private_conv(ctl)
        self.assertIn("no internet", ctl._privacy_note(p))
        b = ctl.settings.briefing()
        text = ctl._live_briefing(b, c=p)
        self.assertIn("no internet", text)
        self.assertNotIn("Allowed sites", text)

    # --- no internet ------------------------------------------------------------------------------

    def test_proxy_refuses_every_site(self):
        sites = SiteList(["allowed.test"], self.root / "sites.txt")
        events = []
        asked = []
        proxy = Proxy(0, [443], sites, lambda h, p: asked.append(h) or True, events.append, offline=True).start()
        try:
            s = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
            s.sendall(b"CONNECT allowed.test:443 HTTP/1.1\r\nHost: allowed.test\r\n\r\n")
            answer = s.recv(200).decode()
            s.close()
        finally:
            proxy.stop()
        self.assertIn("403", answer)
        self.assertEqual(asked, [])   # not even asked
        self.assertTrue(wait_for(lambda: events))
        self.assertEqual(events[0]["verdict"], "deny")

    # --- every conversation: its user's leftovers -------------------------------------------------

    def test_close_cleans_home_and_tmp(self):
        ctl = self.controller([])
        c = ctl.new_conversation("claude")
        home = Path(self.cfg.homes_root) / c.user
        (home / ".cache").mkdir(parents=True)
        (home / ".cache" / "pip.txt").write_text("x")
        (home / ".bash_history").write_text("secret command")
        os.symlink(self.root, home / "shared")
        (self.tmpdir / "left.txt").write_text("x")
        ctl.close_conversation(c.id)
        self.assertEqual(sorted(os.listdir(home)), ["shared"])   # the ~/shared link setup makes stays
        self.assertEqual(os.listdir(self.tmpdir), [])

    # --- its restrictions, each can be lifted ------------------------------------------------------

    def test_defaults_are_all_on(self):
        ctl = self.controller([])
        p = self.private_conv(ctl)
        self.assertEqual(p.opts, {"offline": True, "pc_only": True, "sealed": True})
        self.assertTrue(p.offline and p.pc_only and p.sealed)
        note = ctl._privacy_note(p)
        self.assertIn("no internet", note)
        self.assertIn("keeps its files to itself", note)
        n = ctl.new_conversation("claude")
        self.assertFalse(n.offline or n.pc_only or n.sealed)

    def test_any_model_and_internet(self):
        ctl = self.controller([])
        p = ctl.new_conversation("claude", private=True,
                                 private_opts={"offline": False, "pc_only": False, "sealed": True})
        self.assertTrue(p.private)
        self.assertEqual(p.model, "claude")
        self.assertEqual(p.level, "paid")    # the model's, as for any conversation
        self.assertEqual(p.seen, "paid")
        self.assertFalse(p.offline)
        self.assertFalse(p.pc_only)
        ctl.set_model("claude-opus", p.id)    # another model is fine now
        self.assertNotIn("no internet", ctl._privacy_note(p))
        self.assertIn("Allowed sites", ctl._live_briefing(ctl.settings.briefing(), c=p))
        # Still never handed on, still erased.
        with self.assertRaises(PrivacyError):
            ctl.new_conversation("claude", transcript_from=p.id)
        with self.assertRaises(NeedsConfirm):
            ctl.close_conversation(p.id)
        ctl.close_conversation(p.id, erase=True)
        self.assertNotIn(p.id, ctl.convs)

    def test_sealed_blocks_what_leaves(self):
        ctl = self.controller([])
        p = self.private_conv(ctl)
        text, err = ctl._update_shared(p, {"share": "x", "path": "a.txt", "content": "y"}, "c1")
        self.assertTrue(err)
        self.assertIn("keeps its files to itself", text)
        text, err = ctl._propose_tool(p, {"folder": "tool"}, "c2")
        self.assertTrue(err)
        self.assertIn("tools can't be installed", text)
        with self.assertRaises(PrivacyError):
            ctl.resolve(p.id, {})
        with self.assertRaises(ValueError):   # keeping files is only for a sealed private one
            ctl.keep_files(ctl.new_conversation("claude").id)

    def test_unsealed_brings_in_directly(self):
        self.cfg.privacy_general = "local"
        ctl = self.controller([run("c1", "printf hi > out.txt"), say("Done.")])
        p = ctl.new_conversation(private=True, private_opts={"sealed": False})
        ctl.start_task("make out.txt", p.id)
        self.assertTrue(wait_for(lambda: p.state == "idle"))
        self.assertIn("out.txt", ctl.sync(p.id)["brought_in"])

    def test_erase_everything_with_changes(self):
        marker = "MARKER-e9d1"
        ctl = self.controller([run("c1", f"printf {marker} > {marker}.txt"), say("Done.")])
        p = self.private_conv(ctl)
        ctl.start_task("write it", p.id)
        self.assertTrue(wait_for(lambda: p.state == "idle"))
        ctl.close_conversation(p.id, discard=True, erase=True)   # the card's "Erase everything"
        self.assertEqual(self.where_is(marker), [])
        self.assertFalse((Path(self.cfg.workdir) / f"{marker}.txt").exists())

    # --- the panel's API -------------------------------------------------------------------------

    def test_api_options_and_keep(self):
        self.cfg.privacy_general = "local"
        ctl = self.controller([run("c1", "printf hi > k.txt"), say("Done.")])
        client = TestClient(create_app(ctl, "t"))
        r = client.post("/api/conversations", json={"private": True, "private_options": {"offline": False}}, headers=H)
        self.assertEqual(r.status_code, 200, r.text)
        cid = r.json()["conv"]
        row = {c["id"]: c for c in r.json()["conversations"]}[cid]
        self.assertEqual(row["private_opts"], {"offline": False, "pc_only": True, "sealed": True})
        self.assertTrue(row["sealed"])
        ctl.start_task("make k.txt", cid)
        self.assertTrue(wait_for(lambda: ctl.conv(cid).state == "idle"))
        r = client.post("/api/conversations/close", json={"conv": cid}, headers=H)
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()["unsaved"] and r.json()["sealed"])
        self.assertEqual(client.post("/api/conversations/keep", json={"conv": cid}, headers=H).status_code, 200)
        r = client.post("/api/conversations/sync", json={"conv": cid}, headers=H)
        self.assertEqual(r.status_code, 200, r.text)
        r = client.post("/api/conversations/close", json={"conv": cid, "erase": True}, headers=H)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((Path(self.cfg.workdir) / "k.txt").read_text(), "hi")

    # --- the panel's API -------------------------------------------------------------------------

    def test_api(self):
        ctl = self.controller([])
        client = TestClient(create_app(ctl, "t"))
        r = client.post("/api/conversations", json={"private": True}, headers=H)
        self.assertEqual(r.status_code, 200, r.text)
        cid = r.json()["conv"]
        listed = {c["id"]: c for c in r.json()["conversations"]}
        self.assertTrue(listed[cid]["private"])
        self.assertEqual(r.json()["private"]["state"], "ok")
        r = client.post("/api/conversations", json={"private": True, "model": "claude"}, headers=H)
        self.assertGreaterEqual(r.status_code, 400)
        r = client.post("/api/conversations/close", json={"conv": cid}, headers=H)
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()["confirm"])
        r = client.post("/api/conversations/close", json={"conv": cid, "erase": True}, headers=H)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(cid, [c["id"] for c in r.json()["conversations"]])


if __name__ == "__main__":
    unittest.main()
