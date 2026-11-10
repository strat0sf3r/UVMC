"""Tools the agent builds: proposal, approval, frozen copy, settings and secrets, runs, schedule."""

import datetime as dt
import json
import os
import queue
import sys
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
from test_supervisor import FakeClient, reply  # noqa: E402
from uvmc.made_tools import ToolError, next_run, schedule_text, validate  # noqa: E402
from uvmc.privacy import NeedsConfirm, PrivacyError  # noqa: E402
from uvmc.service import Controller, create_app  # noqa: E402

H = {"Authorization": "Bearer t"}

GOOD = {
    "name": "greeter", "title": "Greeter", "description": "Says hello and counts its runs.",
    "command": "python3 main.py", "schedule": {"every_minutes": 60}, "timeout_seconds": 30,
    "sites": ["api.example.com"],
    "settings": [{"name": "who", "label": "Who to greet", "type": "text", "required": True},
                 {"name": "token", "label": "API token", "type": "secret", "required": True}],
    "setup": "Make an API token first.",
}
MAIN = """import os, pathlib
home = pathlib.Path(os.environ["HOME"])
n = int((home / "count").read_text()) + 1 if (home / "count").exists() else 1
(home / "count").write_text(str(n))
print(f"hello {os.environ['UVMC_SETTING_WHO']}, run {n}, token ends {os.environ['UVMC_SETTING_TOKEN'][-3:]}")
print("proxy", os.environ["HTTPS_PROXY"])
"""


def propose(cid="t1", folder="tools/greeter"):
    return reply(ToolUseBlock(type="tool_use", id=cid, name="propose_tool",
                              input={"folder": folder, "reason": "You asked for a greeter; I ran it twice."}))


class ManifestTest(unittest.TestCase):
    def test_validate(self):
        m = validate(GOOD)
        self.assertEqual((m["name"], m["timeout_seconds"], m["sites"]), ("greeter", 30, ["api.example.com"]))
        self.assertEqual(m["settings"][1]["default"], "")         # secrets have no default
        bad = [({"name": "Bad Name"}, "name"), ({"command": ""}, "command"),
               ({"schedule": {"every_minutes": 1}}, "at least 5"), ({"schedule": {"daily_at": "25:00"}}, "daily_at"),
               ({"sites": ["not a site"]}, "sites"), ({"timeout_seconds": 99999}, "timeout"),
               ({"settings": [{"name": "x", "type": "file"}]}, "type")]
        for change, words in bad:
            with self.assertRaises(ToolError) as e:
                validate({**GOOD, **change})
            self.assertIn(words, str(e.exception))
        self.assertIsNone(validate({**GOOD, "schedule": None})["schedule"])

    def test_schedule(self):
        now = dt.datetime(2026, 11, 8, 10, 0).timestamp()
        self.assertEqual(schedule_text({"every_minutes": 120}), "every 2 h")
        self.assertEqual(schedule_text({"daily_at": "08:00"}), "every day at 08:00")
        self.assertEqual(next_run({"every_minutes": 60}, now - 600, now), now + 3000)
        self.assertLessEqual(next_run({"every_minutes": 60}, None, now), now)          # never ran: due now
        at8 = dt.datetime(2026, 11, 8, 8, 0).timestamp()
        self.assertEqual(next_run({"daily_at": "08:00"}, at8, now), at8 + 86400)         # ran today
        self.assertEqual(next_run({"daily_at": "12:00"}, None, now), at8 + 4 * 3600)     # later today
        self.assertIsNone(next_run(None, None, now))


class MadeToolsTest(ServiceBase):
    def setUp(self):
        super().setUp()
        folder = Path(self.cfg.workdir) / "tools" / "greeter"
        folder.mkdir(parents=True)
        (folder / "uvmc-tool.json").write_text(json.dumps(GOOD))
        (folder / "main.py").write_text(MAIN)
        self.folder = folder

    def ctl(self, replies):
        self.fake = FakeClient(replies)
        ctl = Controller(self.cfg, lambda *_: self.fake)
        self.queue = ctl.subscribe()
        return ctl

    def collect(self):
        """Takes the events published so far, here and now. (A thread collected them before; it could
        still be behind when the conversation already said idle, and a test then missed the last
        results: one full run in five or six failed, 2026-11-10.)"""
        while True:
            try:
                e = self.queue.get_nowait()
            except queue.Empty:
                return
            if "seq" in e:   # log events; not the "conversations" list updates
                self.events.append(e)

    def wait_idle(self, ctl, timeout=5.0):
        """Until the conversation is idle, then its events: every result is published before that."""
        ok = wait_for(lambda: ctl.state == "idle", timeout)
        self.collect()
        return ok

    def approve_next(self, ctl, answer="allow"):
        self.assertTrue(wait_for(lambda: ctl.state == "waiting"))
        card = ctl.snapshot()["pending"][0]
        ctl.decide(card["id"], answer)
        return card

    def test_propose_approve_configure_run(self):
        ctl = self.ctl([propose(), reply(TextBlock(type="text", text="Proposed."), stop="end_turn")])
        ctl.start_task("make me a greeter")
        card = self.approve_next(ctl)
        self.assertEqual(card["type"], "made_tool")
        self.assertEqual(card["manifest"]["name"], "greeter")
        self.assertFalse(card["update"])
        self.assertIn("count", card["shown"][0]["text"] + card["shown"][1]["text"])   # the code is on the card
        self.assertTrue(self.wait_idle(ctl))
        result = [e for e in self.events if e["kind"] == "result"][0]
        self.assertFalse(result["is_error"])
        self.assertIn("version 1", result["text"])
        self.assertIn("Who to greet", result["text"])                 # what the user still has to do

        view = ctl.made.view("greeter")
        self.assertEqual((view["version"], view["enabled"]), (1, False))
        self.assertEqual(view["missing"], ["Who to greet", "API token"])
        with self.assertRaises(ToolError):
            ctl.made.set_enabled("greeter", True)                     # settings first
        with self.assertRaises(ToolError):
            ctl.made.run("greeter")

        ctl.made.save_settings("greeter", {"who": "Vincent", "token": "secret-abc"})
        view = ctl.made.view("greeter")
        self.assertEqual(view["missing"], [])
        self.assertTrue([s for s in view["settings"] if s["name"] == "token"][0]["set"])
        self.assertNotIn("secret-abc", json.dumps(view))              # never shown again
        self.assertNotIn("secret-abc", (Path(self.cfg.made_tools_dir) / "registry.json").read_text())
        ctl.made.save_settings("greeter", {"token": ""})              # empty keeps the stored secret
        self.assertEqual(ctl.made.missing("greeter"), [])

        run = ctl.made.run("greeter")
        self.assertTrue(wait_for(lambda: not ctl.made.running, 10))
        last = ctl.made.view("greeter")["last_run"]
        self.assertTrue(last["ok"], last)
        log = ctl.made.log("greeter", run["id"])
        self.assertIn("hello Vincent, run 1, token ends abc", log)
        self.assertIn(f"127.0.0.1:{ctl.made.registry['greeter']['port']}", log)   # its own proxy
        time.sleep(1.1)   # run ids are per second
        ctl.made.run("greeter")
        self.assertTrue(wait_for(lambda: not ctl.made.running, 10))
        self.assertIn("run 2", ctl.made.log("greeter", ctl.made.view("greeter")["last_run"]["id"]))  # its data stays

        # The frozen copy doesn't follow the agent's folder.
        (Path(ctl.conv().copy_dir) / "tools/greeter/main.py").write_text("print('changed by the agent')\n")
        self.assertIn("count", ctl.made.file("greeter", "main.py")["text"])
        # Only the declared site goes through its proxy.
        proxy = ctl.made.proxies["greeter"]
        self.assertTrue(proxy.sites.allows("api.example.com"))
        self.assertFalse(proxy.sites.allows("example.org"))
        self.assertFalse(proxy.decide("example.org", 443))           # nobody is asked: refused
        for p in ctl.made.proxies.values():
            p.stop()

    def test_new_version_shows_the_changes_and_can_be_declined(self):
        ctl = self.ctl([propose("t1"), reply(TextBlock(type="text", text="ok"), stop="end_turn"),
                        propose("t2"), reply(TextBlock(type="text", text="ok"), stop="end_turn")])
        ctl.start_task("make it")
        self.approve_next(ctl)
        self.assertTrue(self.wait_idle(ctl))
        (Path(ctl.conv().copy_dir) / "tools/greeter/main.py").write_text(MAIN.replace("hello", "good morning"))
        ctl.start_task("change the greeting")
        card = self.approve_next(ctl, "deny")
        self.assertTrue(card["update"])
        self.assertEqual(card["current_version"], 1)
        changed = [f for f in card["shown"] if f["path"] == "main.py"][0]
        self.assertEqual(changed["change"], "changed")
        self.assertIn("-print(f\"hello", changed["text"])
        self.assertIn("+print(f\"good morning", changed["text"])
        self.assertTrue(self.wait_idle(ctl))
        self.assertEqual(ctl.made.view("greeter")["version"], 1)      # declined: nothing changed
        result = [e for e in self.events if e["kind"] == "result"][-1]
        self.assertTrue(result["is_error"])
        self.assertIn("declined", result["text"])
        for p in ctl.made.proxies.values():
            p.stop()

    def test_bad_proposals_are_refused_before_asking_you(self):
        (self.folder / "uvmc-tool.json").write_text(json.dumps({**GOOD, "sites": ["not a site!"]}))
        ctl = self.ctl([propose(), reply(TextBlock(type="text", text="ok"), stop="end_turn")])
        ctl.start_task("make it")
        self.assertTrue(self.wait_idle(ctl))
        self.assertFalse([e for e in self.events if e["kind"] == "approval"])
        result = [e for e in self.events if e["kind"] == "result"][0]
        self.assertIn("sites", result["text"])
        os.remove(Path(ctl.conv().copy_dir) / "tools/greeter/uvmc-tool.json")
        with self.assertRaises(ToolError) as e:
            ctl.made.read_folder(ctl.conv().tools._as_agent, "tools/greeter")
        self.assertIn('"name": "inbox-digest"', str(e.exception))      # it explains the format

    def test_schedule_runs_and_switches_off_when_settings_go_missing(self):
        ctl = self.ctl([])
        ctl.ensure_copy(ctl.conv())   # the conversation's copy of the work folder, as at its first task
        manifest, files = ctl.made.read_folder(ctl.conv().tools._as_agent, "tools/greeter")
        ctl.made.install(manifest, files)
        ctl.made.save_settings("greeter", {"who": "you", "token": "t0k"})
        self.assertEqual(ctl.made.run_due(), [])                        # off: nothing runs
        ctl.made.set_enabled("greeter", True)
        self.assertEqual(ctl.made.run_due(), ["greeter"])               # never ran: due now
        self.assertTrue(wait_for(lambda: not ctl.made.running, 10))
        self.assertEqual(ctl.made.run_due(), [])                        # next one in an hour
        self.assertEqual(ctl.made.run_due(time.time() + 3601), ["greeter"])
        self.assertTrue(wait_for(lambda: not ctl.made.running, 10))
        ctl.made.save_settings("greeter", {"token": "\0clear"})
        self.assertEqual(ctl.made.run_due(time.time() + 7300), [])
        e = ctl.made.view("greeter")
        self.assertFalse(e["enabled"])
        self.assertIn("switched off", e["last_run"]["note"])
        for p in ctl.made.proxies.values():
            p.stop()

    def test_time_limit_and_api(self):
        slow = {**GOOD, "name": "sleeper", "command": "sleep 30", "timeout_seconds": 5, "settings": []}
        ctl = self.ctl([])
        ctl.made.install(validate(slow), {"uvmc-tool.json": json.dumps(slow).encode()})
        c = TestClient(create_app(ctl, "t"))
        listed = c.get("/api/made-tools", headers=H).json()
        self.assertEqual(listed[0]["name"], "sleeper")
        self.assertEqual(listed[0]["schedule_text"], "every 1 h")
        c.post("/api/made-tools/run", headers=H, json={"name": "sleeper"})
        self.assertEqual(c.post("/api/made-tools/run", headers=H, json={"name": "sleeper"}).status_code, 409)
        self.assertTrue(wait_for(lambda: not ctl.made.running, 20))
        last = c.get("/api/made-tools/sleeper", headers=H).json()["last_run"]
        self.assertFalse(last["ok"])
        self.assertIn("time limit", last["note"])
        self.assertEqual(c.post("/api/made-tools/enable", headers=H, json={"name": "sleeper", "on": True}).json()["enabled"], True)
        self.assertEqual(c.get("/api/made-tools/nope", headers=H).status_code, 404)
        self.assertEqual(c.get("/api/made-tools/sleeper/file?path=../../registry.json", headers=H).status_code, 404)
        c.post("/api/made-tools/remove", headers=H, json={"name": "sleeper"})
        self.assertEqual(c.get("/api/made-tools", headers=H).json(), [])
        self.assertFalse((Path(self.cfg.made_tools_dir) / "sleeper").exists())

    def test_in_the_machine_secrets_stay_off_the_command_line(self):
        ctl = self.ctl([])
        ctl.ensure_copy(ctl.conv())   # the conversation's copy of the work folder, as at its first task
        manifest, files = ctl.made.read_folder(ctl.conv().tools._as_agent, "tools/greeter")
        ctl.made.install(manifest, files)
        ctl.made.save_settings("greeter", {"who": "you", "token": "s3cret"})
        e = ctl.made.registry["greeter"]
        env = ctl.made.env("greeter", e)
        os.environ["UVMC_NO_SUDO"] = "0"
        try:
            cmd = ctl.made.command("greeter", e, env)
        finally:
            os.environ["UVMC_NO_SUDO"] = "1"
        self.assertNotIn("s3cret", " ".join(cmd))                     # other users can see command lines
        self.assertEqual(env["UVMC_SETTING_TOKEN"], "s3cret")         # it gets it in its environment
        self.assertIn("UVMC_SETTING_TOKEN", next(a for a in cmd if a.startswith("--preserve-env=")))
        self.assertIn("uvmc-t-greeter", " ".join(cmd))                 # as its own user (systemd or plain sudo)
        self.assertIn(f"HOME={ctl.made.home('greeter')}", cmd)
        self.assertEqual(env["PLAYWRIGHT_BROWSERS_PATH"], "/opt/uvmc/ms-playwright")   # the shared browser
        self.assertIn("PLAYWRIGHT_BROWSERS_PATH", next(a for a in cmd if a.startswith("--preserve-env=")))
        for p in ctl.made.proxies.values():
            p.stop()

    def test_the_agent_is_told_about_its_tools(self):
        ctl = self.ctl([])
        ctl.ensure_copy(ctl.conv())   # the conversation's copy of the work folder, as at its first task
        manifest, files = ctl.made.read_folder(ctl.conv().tools._as_agent, "tools/greeter")
        ctl.made.install(manifest, files)
        text = ctl.briefing_view()["preview"]
        self.assertIn("greeter (version 1, off, every 1 h)", text)
        self.assertIn("propose_tool", json.dumps(ctl.session.tools.definitions(None) + [  # offered to the agent
            t for t in ctl._share_tools(ctl.conv())[0]]))
        for p in ctl.made.proxies.values():
            p.stop()

    def test_a_run_leaves_a_report_shown_as_a_card(self):
        tool = {**GOOD, "name": "reporter", "settings": [], "schedule": None}
        main = REPORTER
        ctl = self.ctl([])
        ctl.made.install(validate(tool), {"uvmc-tool.json": json.dumps(tool).encode(), "main.py": main.encode()})
        runs = []
        q = ctl.subscribe()
        import queue
        c = TestClient(create_app(ctl, "t"))
        rid = c.post("/api/made-tools/run", headers=H, json={"name": "reporter"}).json()["id"]
        self.assertTrue(wait_for(lambda: not ctl.made.running, 10))
        try:
            while True:
                e = q.get(timeout=2)
                if e.get("kind") == "tool_run":
                    runs.append(e)
                    break
        except queue.Empty:
            pass
        self.assertEqual(len(runs), 1)                                   # a card for the panel
        card = runs[0]
        self.assertEqual((card["name"], card["title"], card["run"]["id"]), ("reporter", "Greeter", rid))
        self.assertEqual(card["report"]["title"], "Run 1")
        self.assertEqual(card["run"]["report"]["stats"], [{"label": "Runs", "value": "1", "note": "", "trend": ""}])  # for the overview card
        self.assertEqual(card["report"]["blocks"][2]["n"], 1)             # its image, copied out
        self.assertEqual(card["problems"], ["image missing.png: not found."])
        self.assertNotIn("text", card)                                    # a report: the log stays in Tools
        img = c.get(f"/api/made-tools/reporter/runs/{rid}/images/1", headers=H)
        self.assertEqual(img.headers["content-type"], "image/png")
        self.assertIn("sandbox", img.headers["content-security-policy"])
        self.assertEqual(c.get(f"/api/made-tools/reporter/runs/{rid}/images/2", headers=H).status_code, 404)
        # New in Reports until you look at it there.
        self.assertEqual(c.get("/api/made-tools", headers=H).json()[0]["unseen"], 1)
        c.post("/api/made-tools/seen", headers=H, json={"name": "reporter", "id": rid})
        self.assertEqual(c.get("/api/made-tools", headers=H).json()[0]["unseen"], 0)
        # Notifications: by default when it asks, fails or reports a problem; your choice per tool.
        self.assertEqual(card["notify"], "asks")
        self.assertEqual(c.post("/api/made-tools/notify", headers=H, json={"name": "reporter", "mode": "loud"}).status_code, 409)
        c.post("/api/made-tools/notify", headers=H, json={"name": "reporter", "mode": "never"})
        self.assertEqual(c.get("/api/made-tools/reporter", headers=H).json()["notify"], "never")
        self.assertEqual(ctl.made.result("reporter", rid)["notify"], "never")
        self.assertEqual(c.get(f"/api/made-tools/reporter/runs/{rid}/result", headers=H).json()["report"]["title"], "Run 1")

        # The next run removes the old report first; a link it plants is not followed.
        home = ctl.made.home("reporter")
        (home / "plant").write_text("1")
        time.sleep(1.1)
        rid2 = ctl.made.run("reporter")["id"]
        self.assertTrue(wait_for(lambda: not ctl.made.running, 10))
        self.assertTrue((home / "uvmc-report.json").is_symlink())
        res = ctl.made.result("reporter", rid2)
        self.assertIsNone(res["report"])
        self.assertIn("planted", res["text"])                            # no report: what it printed instead
        for p in ctl.made.proxies.values():
            p.stop()

    def test_the_sample_report_is_checked_and_shown_on_the_card(self):
        (self.folder / "uvmc-report.json").write_text(json.dumps({"title": "Sample", "blocks": [{"type": "nope"}]}))
        ctl = self.ctl([propose("t1"), reply(TextBlock(type="text", text="ok"), stop="end_turn"),
                        propose("t2"), reply(TextBlock(type="text", text="ok"), stop="end_turn")])
        ctl.start_task("make it")
        self.assertTrue(self.wait_idle(ctl))
        self.assertFalse([e for e in self.events if e["kind"] == "approval"])   # back to the agent first
        result = [e for e in self.events if e["kind"] == "result"][-1]
        self.assertIn("unknown block type 'nope'", result["text"])
        copy = Path(ctl.conv().copy_dir) / "tools/greeter"
        (copy / "uvmc-report.json").write_text(json.dumps(
            {"title": "Sample", "blocks": [{"type": "image", "file": "p.png"}]}))
        (copy / "p.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 20)
        ctl.start_task("try again")
        card = self.approve_next(ctl, "deny")
        self.assertEqual(card["sample"]["title"], "Sample")
        self.assertTrue(card["sample"]["blocks"][0]["src"].startswith("data:image/png;base64,"))
        self.assertTrue(self.wait_idle(ctl))


class WorkOnToolTest(MadeToolsTest):
    """Changing an approved tool from any conversation: its code comes into the agent's folder."""
    test_propose_approve_configure_run = test_new_version_shows_the_changes_and_can_be_declined = None
    test_bad_proposals_are_refused_before_asking_you = test_schedule_runs_and_switches_off_when_settings_go_missing = None
    test_time_limit_and_api = test_in_the_machine_secrets_stay_off_the_command_line = None
    test_the_agent_is_told_about_its_tools = test_a_run_leaves_a_report_shown_as_a_card = None
    test_the_sample_report_is_checked_and_shown_on_the_card = None

    def install(self, ctl):
        ctl.ensure_copy(ctl.conv())
        manifest, files = ctl.made.read_folder(ctl.conv().tools._as_agent, "tools/greeter")
        files["run.sh"], files["run.sh\0x"] = b"#!/bin/sh\necho hi\n", b""
        ctl.made.install(manifest, files)
        ctl.made.save_settings("greeter", {"who": "you", "token": "s3cret-token"})

    def test_work_on_this_tool_opens_a_conversation_with_its_code(self):
        ctl = self.ctl([])
        self.install(ctl)
        first = ctl.conv().id
        import shutil
        shutil.rmtree(self.folder)   # built in a conversation whose work never went into the main folder
        c = TestClient(create_app(ctl, "t"))
        w = c.post("/api/made-tools/greeter/work", headers=H, json={}).json()
        self.assertNotEqual(w["conv"], first)                            # a fresh conversation
        self.assertEqual((w["folder"], w["version"], w["title"]), ("tools/greeter", 1, "Greeter"))
        folder = Path(ctl.convs[w["conv"]].copy_dir) / "tools/greeter"
        self.assertIn("count", (folder / "main.py").read_text())
        self.assertTrue(os.access(folder / "run.sh", os.X_OK))           # executables stay executable
        self.assertEqual(json.loads((folder / "uvmc-tool.json").read_text())["name"], "greeter")
        everything = "".join(p.read_text(errors="replace") for p in folder.rglob("*") if p.is_file())
        self.assertNotIn("s3cret-token", everything)                     # code only, no secrets
        self.assertEqual(c.post("/api/made-tools/nope/work", headers=H, json={}).status_code, 404)
        for p in ctl.made.proxies.values():
            p.stop()

    def test_the_agent_gets_the_code_without_losing_its_own(self):
        get = lambda cid: reply(ToolUseBlock(type="tool_use", id=cid, name="get_tool_code", input={"name": "greeter"}))
        ctl = self.ctl([get("g1"), get("g2"), reply(TextBlock(type="text", text="ok"), stop="end_turn")])
        self.install(ctl)
        ctl.start_task("make the greeter friendlier")
        self.assertTrue(self.wait_idle(ctl))
        results = [e for e in self.events if e["kind"] == "result"]
        # This conversation built it: its own folder tools/greeter stays; the copy goes next to it.
        self.assertIn("tools/greeter-v1", results[0]["text"])
        self.assertFalse(results[0]["is_error"])
        copy = Path(ctl.conv().copy_dir)
        self.assertTrue((copy / "tools/greeter-v1/main.py").exists())
        self.assertIn("tools/greeter-v1", results[1]["text"])            # asked again: still there
        self.assertIn("get_tool_code", json.dumps(ctl._share_tools(ctl.conv())[0]))
        for p in ctl.made.proxies.values():
            p.stop()

    def test_a_tool_has_a_privacy_level(self):
        ctl = self.ctl([propose(), reply(TextBlock(type="text", text="Proposed."), stop="end_turn")])
        ctl.conv().seen = "paid"   # this conversation has seen paid files: its code may carry them
        ctl.start_task("make me a greeter")
        self.assertTrue(wait_for(lambda: ctl.state == "waiting"))
        card = ctl.snapshot()["pending"][0]
        self.assertEqual(card["level"], "paid")                           # suggested on the card
        ctl.decide(card["id"], "allow", "local")                          # you may choose another
        self.assertTrue(self.wait_idle(ctl))
        self.assertEqual(ctl.made.view("greeter")["level"], "local")
        # Changing it later: raising is done, lowering asks first.
        with self.assertRaises(NeedsConfirm):
            ctl.set_tool_level("greeter", "open")
        self.assertEqual(ctl.set_tool_level("greeter", "open", confirm=True)["level"], "open")
        ctl.set_tool_level("greeter", "local")
        # Its code goes only to a conversation at its level: Claude (paid) may not work on it.
        with self.assertRaises(PrivacyError):
            ctl.work_on_tool("greeter")
        text, err = ctl._get_tool_code(ctl.conv(), {"name": "greeter"})
        self.assertTrue(err)
        self.assertIn("local", text)
        ctl.set_tool_level("greeter", "paid", confirm=True)
        w = ctl.work_on_tool("greeter")
        self.assertEqual(ctl.convs[w["conv"]].seen, "paid")              # having its code counts as seeing it
        c = TestClient(create_app(ctl, "t"))
        r = c.post("/api/made-tools/level", headers=H, json={"name": "greeter", "level": "open"})
        self.assertEqual((r.status_code, r.json()["confirm"]), (409, True))
        for p in ctl.made.proxies.values():
            p.stop()


REPORTER = """import json, os, pathlib, sys
home = pathlib.Path(os.environ["HOME"])
report = pathlib.Path(os.environ["UVMC_REPORT"])
assert not report.exists(), "the old report should be gone"
if (home / "plant").exists():
    report.symlink_to("/etc/hostname")
    print("planted a link")
    sys.exit(0)
(home / "plot.png").write_bytes(b"\\x89PNG\\r\\n\\x1a\\n" + b"\\0" * 20)
report.write_text(json.dumps({"title": "Run 1", "blocks": [
    {"type": "columns", "columns": [[{"type": "stats", "items": [{"label": "Runs", "value": 1}]}], []]},
    {"type": "text", "md": "hello"}, {"type": "image", "file": "plot.png"}, {"type": "image", "file": "missing.png"}]}))
print("wrote a report")
"""


if __name__ == "__main__":
    unittest.main()
