"""An agent working inside a service's account (slice 2b part 2): a conversation whose commands
run as the service in its install session, never on a work copy; propose_profile; its walls on
closing (the service's data folder is never cleaned like a conversation user's home); the
service's privacy level. Offline, with a stand-in model."""

import os
import sys
import unittest
from pathlib import Path

os.environ["UVMC_NO_SUDO"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from anthropic.types import TextBlock, ToolUseBlock  # noqa: E402

from test_service import ServiceBase, wait_for  # noqa: E402
from test_services import LOCAL_PROFILE  # noqa: E402
from test_supervisor import reply  # noqa: E402
from uvmc.privacy import PrivacyError  # noqa: E402
from uvmc.services import ServiceError  # noqa: E402

WIDER = LOCAL_PROFILE.replace('sites = ["example.com"]', 'sites = ["example.com", "pypi.org"]')


def tool(cid, name, args):
    return reply(ToolUseBlock(type="tool_use", id=cid, name=name, input=args))


def say(text):
    return reply(TextBlock(type="text", text=text), stop="end_turn")


class InsideTest(ServiceBase):
    def make(self, replies):
        ctl = self.controller(replies)
        ctl.services.add_local(LOCAL_PROFILE)
        ctl.services.approve("random-app", "local")
        return ctl

    def test_it_works_in_the_app_folder_and_proposes_a_profile(self):
        ctl = self.make([
            tool("c1", "run_command", {"command": "pwd && echo hello > installed.txt"}),
            tool("c2", "propose_profile", {"profile": WIDER}),
            say("Installed."),
        ])
        app = ctl.services.app("random-app")
        data = ctl.services.home("random-app")
        (data / "keep.db").write_text("the app's data\n")
        r = ctl.new_conversation_inside("random-app")
        c = ctl.conv(r["conv"])
        self.assertEqual(c.service, "random-app")
        self.assertEqual(ctl.services.get("random-app")["session"]["who"], "agent")
        self.assertEqual(ctl.services.get("random-app")["session"]["conv"], c.id)
        with self.assertRaises(ServiceError):
            ctl.new_conversation_inside("random-app")                    # one session at a time
        ctl.start_task("install it", c.id)
        self.assertTrue(wait_for(lambda: ctl.conv(c.id).state == "idle"))
        self.assertEqual((app / "installed.txt").read_text(), "hello\n")   # in its app folder
        outs = [e for e in self.events if e["kind"] == "result" and e.get("tool") == "run_command"]
        self.assertIn(str(app), outs[0]["text"])                          # pwd: its app folder
        self.assertFalse(ctl.copies.exists(c.id))                         # no work copy, ever
        self.assertEqual(ctl.unsaved(c), 0)
        p = ctl.services.view("random-app")["proposal"]
        self.assertEqual((p["kind"], p["manifest"]["sites"]), ("manifest", ["example.com", "pypi.org"]))
        # only its four tools and propose_profile
        self.assertEqual(c.session.tool_names, ["run_command", "read_file", "write_file", "list_dir"])
        self.assertEqual([t["name"] for t in c.session.extra_tools], ["propose_profile"])
        self.assertIn("inside its own account", c.session.system)
        # closing ends the session and leaves the service's folders alone
        ctl.close_conversation(c.id)
        self.assertNotIn(c.id, ctl.convs)                                   # gone from the panel too
        self.assertIsNone(ctl.services.get("random-app")["session"])
        self.assertEqual((data / "keep.db").read_text(), "the app's data\n")
        self.assertTrue((app / "installed.txt").exists())

    def test_only_installed_in_place_services(self):
        ctl = self.make([])
        with self.assertRaises(KeyError):
            ctl.new_conversation_inside("nobody")
        ctl.services.start_session("random-app", "shell")
        with self.assertRaises(ServiceError):
            ctl.new_conversation_inside("random-app")                    # a shell's session is open
        ctl.services.end_session("random-app")
        n = len(ctl.convs)
        ctl.new_conversation_inside("random-app")
        self.assertEqual(len(ctl.convs), n + 1)

    def test_its_privacy_level(self):
        ctl = self.make([])
        ctl.services.set_level("random-app", "local")
        with self.assertRaises(PrivacyError):
            ctl.new_conversation_inside("random-app", "claude")         # an online model can't work inside it
        with self.assertRaises(ValueError):
            ctl.services.set_level("random-app", "secret-ish")
        self.assertIsNone(ctl.services.get("random-app").get("session"))   # nothing was opened

    def test_a_restart_ends_the_agents_session(self):
        ctl = self.make([])
        ctl.new_conversation_inside("random-app")
        from uvmc.services import Services
        again = Services(self.cfg, programs=ctl.programs)
        again.start()                                                     # the supervisor started again
        self.assertIsNone(again.get("random-app")["session"])


if __name__ == "__main__":
    unittest.main()
