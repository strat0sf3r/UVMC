"""What the agent is told: house rules, the live briefing and your notes, kept apart."""

import os
import sys
import unittest
from pathlib import Path

os.environ["UVMC_NO_SUDO"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient  # noqa: E402

from test_delegation import DelegationBase, text, tool  # noqa: E402
from test_service import ServiceBase, wait_for  # noqa: E402
from test_supervisor import reply  # noqa: E402
from uvmc import briefing  # noqa: E402
from uvmc.service import create_app  # noqa: E402
from uvmc.settings import SettingsError  # noqa: E402


def system_of(fake, i=-1):
    return fake.sent[i]["system"]


class BriefingTest(ServiceBase):
    def run_one(self, ctl, task="hi"):
        ctl.start_task(task)
        self.assertTrue(wait_for(lambda: ctl.state == "idle"))

    def test_three_parts_in_order(self):
        open(os.path.join(self.cfg.workdir, "report.md"), "w").close()
        os.makedirs(os.path.join(self.cfg.workdir, "src"))
        ctl = self.controller([reply(text("Hi."), stop="end_turn")])
        ctl.settings.save_briefing({"notes": "Answer in French."})
        self.run_one(ctl)
        system = system_of(self.fake)
        rules_at = system.index(self.cfg.house_rules.strip()[:60])
        live_at = system.index("# Right now")
        notes_at = system.index("# Notes from the user")
        self.assertTrue(rules_at < live_at < notes_at)
        self.assertEqual(system.count(briefing.SEPARATOR.strip()), 2)       # clearly separated
        self.assertIn("Answer in French.", system)
        self.assertIn("This is UVMC", system)
        self.assertIn("report.md", system)
        self.assertIn("src/", system)
        self.assertIn("No folders are shared", system)
        self.assertIn("pypi.org", system)

    def test_fixed_for_the_conversation_then_updated(self):
        ctl = self.controller([reply(text("1"), stop="end_turn"), reply(text("2"), stop="end_turn"),
                               reply(text("3"), stop="end_turn")])
        self.run_one(ctl)
        first = system_of(self.fake)
        ctl.settings.save_briefing({"notes": "Be brief.", "parts": {"work": False}})
        self.run_one(ctl)
        self.assertEqual(system_of(self.fake), first)                      # same conversation: unchanged
        ctl.new_session()
        self.run_one(ctl)
        now = system_of(self.fake)
        self.assertIn("Be brief.", now)
        self.assertNotIn("In the work folder", now)
        self.assertNotIn("work folder (", now)

    def test_switched_off_keeps_rules_and_notes(self):
        ctl = self.controller([reply(text("Hi."), stop="end_turn")])
        ctl.settings.save_briefing({"enabled": False, "notes": "Hello from me."})
        self.run_one(ctl)
        system = system_of(self.fake)
        self.assertNotIn("# Right now", system)
        self.assertIn("Hello from me.", system)
        self.assertTrue(system.startswith(self.cfg.house_rules.strip()[:40]))

    def test_house_rules_edit_and_reset(self):
        ctl = self.controller([reply(text("1"), stop="end_turn"), reply(text("2"), stop="end_turn")])
        repo_rules = self.cfg.house_rules
        ctl.settings.save_house_rules("Only use Python.")
        self.assertEqual(ctl.settings.house_rules_source(), "panel")
        self.run_one(ctl)
        self.assertTrue(system_of(self.fake).startswith("Only use Python."))
        with self.assertRaises(SettingsError):
            ctl.settings.save_house_rules("   ")
        ctl.settings.save_house_rules(repo_rules)                            # same as the repo: no override
        self.assertEqual(ctl.settings.house_rules_source(), "repo")
        ctl.settings.save_house_rules("x")
        ctl.settings.reset_house_rules()
        self.assertEqual(self.cfg.house_rules, repo_rules)
        with self.assertRaises(SettingsError):
            ctl.settings.save_briefing({"notes": "x" * (briefing.NOTES_MAX + 1)})

    def test_api(self):
        ctl = self.controller([])
        c = TestClient(create_app(ctl, "t"))
        h = {"Authorization": "Bearer t"}
        d = c.get("/api/briefing", headers=h).json()
        self.assertTrue(d["enabled"])
        self.assertEqual(set(d["parts"]), set(briefing.PARTS))
        self.assertEqual(d["house_rules_source"], "repo")
        self.assertIn("# Right now", d["preview"])
        d = c.post("/api/briefing", headers=h, json={"notes": "Use metric units.", "parts": {"date": False}}).json()
        self.assertIn("Use metric units.", d["preview"])
        self.assertFalse(d["parts"]["date"])
        d = c.post("/api/house-rules", headers=h, json={"text": "Rule one."}).json()
        self.assertEqual(d["house_rules_source"], "panel")
        self.assertTrue(d["preview"].startswith("Rule one."))
        d = c.post("/api/house-rules/reset", headers=h, json={}).json()
        self.assertEqual(d["house_rules_source"], "repo")
        self.assertEqual(c.post("/api/house-rules", headers=h, json={"text": ""}).status_code, 400)


class WorkerBriefingTest(DelegationBase):
    def test_coordinator_and_worker_each_get_their_role(self):
        ctl = self.setup_ctl(
            [reply(tool("d1", "delegate", instructions="Say hi.")), reply(text("Done."), stop="end_turn")],
            [reply(text("hi"), stop="end_turn")])
        ctl.settings.save_briefing({"notes": "Keep it short."})
        ctl.start_task("go")
        self.assertTrue(wait_for(lambda: ctl.state == "idle"))
        boss, worker = system_of(self.boss, 0), system_of(self.worker, 0)
        self.assertIn("You are the coordinator", boss)
        self.assertIn(self.cfg.orchestrator_rules.strip()[:40], boss)
        self.assertIn("You are a worker", worker)
        self.assertIn(self.cfg.house_rules.strip()[:40], worker)
        self.assertIn("Keep it short.", boss)
        self.assertIn("Keep it short.", worker)


if __name__ == "__main__":
    unittest.main()
