"""Offline tests for orchestrator / worker delegation.

Run from the supervisor folder:  UVMC_NO_SUDO=1 python3 -m unittest discover tests
"""

import os
import sys
import time
import unittest
from pathlib import Path

os.environ["UVMC_NO_SUDO"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from anthropic.types import TextBlock, ToolUseBlock  # noqa: E402

from test_service import ServiceBase, wait_for  # noqa: E402
from test_supervisor import FakeClient, reply  # noqa: E402
from uvmc.providers import ProviderError  # noqa: E402
from uvmc.service import Controller  # noqa: E402


def tool(cid, name, **args):
    return ToolUseBlock(type="tool_use", id=cid, name=name, input=args)


def text(t):
    return TextBlock(type="text", text=t)


class DelegationBase(ServiceBase):
    def setup_ctl(self, boss_replies, worker_replies):
        self.boss = FakeClient(boss_replies)
        self.worker = FakeClient(worker_replies)
        clients = {"claude": self.boss, "deepseek": self.worker}

        def factory(profile):
            if profile.name not in clients:
                raise ProviderError(f"No API key for '{profile.name}'.")
            return clients[profile.name]

        self.fake = None
        ctl = Controller(self.cfg, factory)
        q = ctl.subscribe()
        import threading
        threading.Thread(target=lambda: [self.events.append(q.get()) for _ in iter(int, 1)], daemon=True).start()
        ctl.set_worker("deepseek")
        return ctl


class DelegationTest(DelegationBase):
    def test_coordinator_delegates_and_checks(self):
        ctl = self.setup_ctl(
            [reply(text("I'll have the worker write it."),
                   tool("d1", "delegate", instructions="Create hello.txt containing 'hi' in the work folder. Report when done.")),
             reply(tool("r1", "read_file", path="hello.txt")),
             reply(text("Done: hello.txt says hi."), stop="end_turn")],
            [reply(tool("w1", "write_file", path="hello.txt", content="hi")),
             reply(text("Created hello.txt with 'hi'."), stop="end_turn")],
        )
        ctl.start_task("make a hello file")
        self.assertTrue(wait_for(lambda: ctl.state == "idle" and any(e["kind"] == "end" and "parent" not in e for e in self.events)))

        boss_tools = [t["name"] for t in self.boss.sent[0]["tools"]]
        self.assertEqual(sorted(boss_tools), ["delegate", "list_dir", "propose_tool", "read_file"])
        self.assertIn("coordinator", self.boss.sent[0]["system"])
        worker_tools = [t["name"] for t in self.worker.sent[0]["tools"]]
        self.assertNotIn("delegate", worker_tools)
        self.assertIn("run_command", worker_tools)
        self.assertEqual(self.worker.sent[0]["messages"][0]["content"],
                         "Create hello.txt containing 'hi' in the work folder. Report when done.")
        # The worker gets the house rules (and a briefing as a worker), not the coordinator's rules.
        self.assertNotIn(self.cfg.orchestrator_rules.strip()[:60], self.worker.sent[0]["system"])
        self.assertIn(self.cfg.house_rules.strip()[:60], self.worker.sent[0]["system"])

        report = self.boss.sent[1]["messages"][-1]["content"][0]
        self.assertEqual(report["tool_use_id"], "d1")
        self.assertIn("Its final report:\nCreated hello.txt", report["content"])
        self.assertFalse(report["is_error"])
        self.assertEqual(Path(ctl.conv().copy_dir, "hello.txt").read_text(), "hi")   # the conversation's copy

        worker_events = [e for e in self.events if e.get("parent") == "d1"]
        self.assertEqual([e["kind"] for e in worker_events][:2], ["task", "reply"])
        self.assertTrue(all(e["agent"] == "deepseek" for e in worker_events))
        self.assertEqual(sum(1 for e in self.events if e["kind"] == "end"), 2)

    def test_worker_approval_and_stop(self):
        self.cfg.ask_patterns = [r"pip install"]
        ctl = self.setup_ctl(
            [reply(tool("d1", "delegate", instructions="Install requests."))],
            [reply(tool("w1", "run_command", command="pip install requests"))],
        )
        ctl.start_task("install requests")
        self.assertTrue(wait_for(lambda: ctl.state == "waiting"))
        pending = ctl.snapshot()["pending"][0]
        self.assertEqual(pending["call"], "w1")
        ctl.stop()
        self.assertTrue(wait_for(lambda: ctl.state == "idle"))
        ends = [e for e in self.events if e["kind"] == "end"]
        self.assertEqual([e["reason"] for e in ends], ["stopped by you", "stopped by you"])
        self.assertEqual(ends[0].get("parent"), "d1")  # the worker ends first
        self.assertEqual([m["role"] for m in ctl.session.messages], ["user", "assistant", "tool_results"])
        self.assertFalse(ctl.cancel.is_set())

    def test_bad_worker_choices(self):
        ctl = self.setup_ctl(
            [reply(tool("d1", "delegate", instructions="x", model="nope")),
             reply(tool("d2", "delegate", instructions="x", model="claude-opus")),
             reply(text("Both failed."), stop="end_turn")],
            [],
        )
        ctl.start_task("try")
        self.assertTrue(wait_for(lambda: ctl.state == "idle" and any(e["kind"] == "end" for e in self.events)))
        results = {e["id"]: e for e in self.events if e["kind"] == "result"}
        self.assertIn("not an allowed worker", results["d1"]["text"])
        self.assertIn("could not start", results["d2"]["text"])
        with self.assertRaises(KeyError):
            ctl.set_worker("nope")
        ctl.set_worker(None)
        self.assertIsNone(ctl.snapshot()["worker"])

    def test_activity_is_linked_to_the_workers_step(self):
        ctl = self.setup_ctl(
            [reply(tool("d1", "delegate", instructions="List files.")),
             reply(text("ok"), stop="end_turn")],
            [reply(tool("w1", "list_dir")), reply(text("Listed."), stop="end_turn")],
        )
        tools = ctl.conv().tools   # each conversation runs commands in its own box
        real = tools.run

        def run(name, args):
            ctl._on_exec({"t": time.time(), "pid": 5, "ppid": 1, "args": ["ls", "-la"], "exe": "/usr/bin/ls"})
            return real(name, args)

        tools.run = run
        ctl.start_task("list")
        self.assertTrue(wait_for(lambda: ctl.state == "idle" and any(e["kind"] == "end" and "parent" not in e for e in self.events)))
        execs = [e for e in self.events if e["kind"] == "exec"]
        self.assertEqual(execs[0]["call"], "w1")


if __name__ == "__main__":
    unittest.main()
