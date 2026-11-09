"""A full memory box (the system stopped a process in it) is told to you in the panel.

Run from the supervisor folder:  UVMC_NO_SUDO=1 python3 -m unittest discover tests
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ["UVMC_NO_SUDO"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).parent))

from test_service import ServiceBase, wait_for  # noqa: E402
from uvmc import service  # noqa: E402


class MemoryWatchTest(ServiceBase):
    def setUp(self):
        super().setUp()
        self.counts = {}
        p1 = mock.patch.object(service, "oom_kills", lambda path: self.counts.get(path))
        p2 = mock.patch.object(service, "slice_usage", lambda name="": {"memory_max": 1000 * 1024 * 1024})
        p1.start(), p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def alerts(self):
        return [e for e in self.events if e["kind"] == "memory_full"]

    def test_nothing_outside_the_vm_and_nothing_old(self):
        ctl = self.controller([])
        self.assertEqual(ctl.check_memory(), [])                # no counters at all
        self.counts = {"uvmc-brain.slice": 3, "uvmc-agent.slice": 2}
        self.assertEqual(ctl.check_memory(), [])                # what happened before we looked isn't news
        self.assertEqual(ctl.check_memory(), [])                # unchanged

    def test_a_conversations_program_and_claude_code(self):
        ctl = self.controller([])
        a = ctl.conv()
        b = ctl.new_conversation()
        agent = "uvmc-agent.slice"
        self.counts = {agent: 0, f"{agent}/{a.slice}": 0, f"{agent}/{b.slice}": 0, "uvmc-brain.slice": 0}
        ctl.check_memory()
        self.counts.update({agent: 1, f"{agent}/{b.slice}": 1})
        self.assertEqual(ctl.check_memory(), [{"conv": b.id, "box": "agent"}])   # only the one it happened in
        self.assertTrue(wait_for(lambda: self.alerts()))
        self.assertIn("1000 MB", self.alerts()[0]["text"])
        self.assertIn("[limits] memory", self.alerts()[0]["text"])

        a.session.brain = object()                              # a has a Claude Code running
        self.counts["uvmc-brain.slice"] = 1
        self.assertEqual(ctl.check_memory(), [{"conv": a.id, "box": "claude-code"}])
        self.assertTrue(wait_for(lambda: len(self.alerts()) == 2))
        self.assertIn("[subscription] memory", self.alerts()[1]["text"])
        a.session.brain = None


if __name__ == "__main__":
    unittest.main()
