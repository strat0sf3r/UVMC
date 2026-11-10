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
from pathlib import Path

os.environ["UVMC_NO_SUDO"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from anthropic.types import TextBlock, ToolUseBlock  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from test_service import ServiceBase, wait_for  # noqa: E402
from test_supervisor import reply  # noqa: E402
from uvmc import audit, mcp_http, private  # noqa: E402
from uvmc.netproxy import Proxy, SiteList  # noqa: E402
from uvmc.privacy import NeedsConfirm, PrivacyError  # noqa: E402
from uvmc.service import Busy, create_app  # noqa: E402

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
        ])
        p = self.private_conv(ctl)
        ctl.net_recent.append({"host": f"{marker}.example", "conv": p.id})
        ctl.exec_recent.append({"args": [marker], "conv": p.id})
        ctl.start_task(f"Write the {marker} files", p.id)
        self.assertTrue(wait_for(lambda: p.state == "idle"))
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
