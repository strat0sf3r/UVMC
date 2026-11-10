"""Compacting a conversation for the model on the PC (uvmc/compact.py): trim, summary, recall."""

import json
import os
import sys
import unittest
import unittest.mock
from pathlib import Path

os.environ["UVMC_NO_SUDO"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from anthropic.types import TextBlock, ToolUseBlock  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from test_service import ServiceBase, wait_for  # noqa: E402
from test_supervisor import reply  # noqa: E402
from uvmc import compact, pc  # noqa: E402
from uvmc.log import RunLog  # noqa: E402
from uvmc.service import Busy, create_app  # noqa: E402

H = {"Authorization": "Bearer t"}
BIG = "x" * 5000


def say(text):
    return reply(TextBlock(type="text", text=text), stop="end_turn")


def run(cid, command):
    return reply(ToolUseBlock(type="tool_use", id=cid, name="run_command", input={"command": command}))


def write(cid, path, content):
    return reply(ToolUseBlock(type="tool_use", id=cid, name="write_file", input={"path": path, "content": content}))


class CompactTest(ServiceBase):
    def setUp(self):
        super().setUp()
        self.cfg.max_output_chars = 20000   # the shared test config cuts outputs at 400 characters
        # Never the real PC (the VM can reach its model server, whose context size would change the pieces).
        patcher = unittest.mock.patch.object(pc, "address", return_value="")
        patcher.start()
        self.addCleanup(patcher.stop)

    def busy_conversation(self, extra=()):
        """A conversation on the PC's model that printed four long outputs and wrote a long file."""
        ctl = self.controller([
            *[run(f"c{i}", f"python3 -c \"print('{i}' * 3000)\"") for i in range(4)],
            write("w1", "big.txt", BIG),
            say("All done."),
            *extra,
        ])
        c = ctl.new_conversation("local")
        ctl.start_task("Print a lot", c.id)
        self.assertTrue(wait_for(lambda: c.state == "idle"))
        return ctl, c

    def test_only_for_the_model_on_the_pc(self):
        ctl = self.controller([say("hi")])
        c = ctl.new_conversation("claude")
        self.assertFalse(ctl.context_view(c)["compact"])
        with self.assertRaises(ValueError):
            ctl.compact(c.id, "trim")
        p = ctl.new_conversation("local")
        self.assertTrue(ctl.context_view(p)["compact"])
        with self.assertRaises(ValueError):   # nothing in it yet
            ctl.compact(p.id, "trim")

    def test_trim_keeps_the_latest_and_numbers_the_rest(self):
        ctl, c = self.busy_conversation()
        s = c.session
        before = compact.estimate(s)
        r = ctl.compact(c.id, "trim")
        self.assertEqual(r["shortened"], 2)   # 2 of 5 outputs: the latest 3 stay (with the file written)
        results = [r for m in s.messages if m["role"] == "tool_results" for r in m["results"]]
        trimmed = [r for r in results if r.get("trimmed")]
        self.assertEqual(len(trimmed), 2)
        for r in trimmed:
            self.assertIn(f"recall(id={r['seq']})", r["content"])
        self.assertFalse(any(r.get("trimmed") for r in results[-3:]))
        self.assertLess(compact.estimate(s), before)
        self.assertTrue(s.compacted and s.context_estimated)
        events = [e for e in c.log.read() if e["kind"] == "compact"]
        self.assertEqual(events[-1]["shortened"], 2)
        # The long file the agent wrote is among the latest 3 calls: kept. Trim once more with keep=0.
        compact.trim(s, keep=0)
        call = next(tc for m in s.messages if m["role"] == "assistant" for tc in m.get("tool_calls") or [] if tc.name == "write_file")
        self.assertIn("recall(id=", call.input["content"])

    def test_recall_reads_what_was_shortened(self):
        ctl, c = self.busy_conversation()
        ctl.compact(c.id, "trim")
        first = next(r for m in c.session.messages if m["role"] == "tool_results" for r in m["results"] if r.get("trimmed"))
        text, err = compact.recall(c.log, {"id": first["seq"]})
        self.assertFalse(err)
        self.assertIn("0" * 3000, text.replace("\n", "")[:7000])
        text, err = compact.recall(c.log, {"search": "big.txt"})
        self.assertFalse(err)
        self.assertIn("write_file", text)
        _, err = compact.recall(c.log, {"id": 99999})
        self.assertTrue(err)
        # A long entry comes in parts.
        log = RunLog(Path(self.tmp.name) / "l")
        e = log.event("result", tool="run_command", is_error=False, text="y" * 15000)
        part1, _ = compact.recall(log, {"id": e["seq"]})
        self.assertIn("offset=6000", part1)

    def test_recall_offered_after_compacting(self):
        ctl, c = self.busy_conversation(extra=[
            reply(ToolUseBlock(type="tool_use", id="r1", name="recall", input={"search": "big.txt"})),
            say("Found it."),
        ])
        ctl.compact(c.id, "trim")
        ctl.start_task("What did you write?", c.id)
        self.assertTrue(wait_for(lambda: c.state == "idle"))
        sent = self.fake.sent[-1]
        self.assertIn("recall", [t["name"] for t in sent["tools"]])
        result = [e for e in c.log.read() if e["kind"] == "result" and e["tool"] == "recall"][-1]
        self.assertFalse(result["is_error"])
        self.assertIn("write_file", result["text"])

    def test_summary_replaces_the_history(self):
        ctl, c = self.busy_conversation(extra=[say("The user printed digits (#3, #5) and wrote big.txt (#12).")])
        ctl.compact(c.id, "summary")
        self.assertTrue(wait_for(lambda: c.state == "idle"))
        m = c.session.messages
        self.assertEqual([x["role"] for x in m], ["user", "assistant", "user", "assistant"])
        self.assertIn("wrote big.txt", m[0]["text"])
        self.assertTrue(m[0]["summary"])
        self.assertEqual(m[2]["text"], "Print a lot")   # your last task stays word for word
        self.assertEqual(m[3]["text"], "All done.")
        asked = self.fake.sent[-1]
        self.assertEqual(asked["tools"], [])
        self.assertIn("#", asked["messages"][0]["content"])   # the entries carry their numbers
        ev = [e for e in c.log.read() if e["kind"] == "compact"][-1]
        self.assertEqual(ev["mode"], "summary")
        self.assertLess(ev["after"], ev["before"])

    def test_summary_in_pieces(self):
        replies = [say("Summary part one."), say("Summary of everything.")]
        ctl, c = self.busy_conversation(extra=replies)
        with unittest.mock.patch.object(compact, "budget_for", return_value=6000):
            ctl.compact(c.id, "summary")
            self.assertTrue(wait_for(lambda: c.state == "idle"))
        summaries = [s for s in self.fake.sent if not s["tools"]]
        self.assertGreaterEqual(len(summaries), 2)
        self.assertIn("The summary of the earlier entries", summaries[-1]["messages"][0]["content"])

    def test_failed_summary_leaves_it_as_it_was(self):
        ctl, c = self.busy_conversation(extra=[say("   ")])
        before = json.dumps(c.session.messages, default=str)
        ctl.compact(c.id, "summary")
        self.assertTrue(wait_for(lambda: c.state == "idle"))
        self.assertEqual(json.dumps(c.session.messages, default=str), before)
        self.assertIn("Compacting failed", [e for e in c.log.read() if e["kind"] == "error"][-1]["text"])

    def test_not_while_working(self):
        ctl, c = self.busy_conversation()
        c.state = "running"
        with self.assertRaises(Busy):
            ctl.compact(c.id, "trim")
        c.state = "idle"

    def test_route(self):
        ctl, c = self.busy_conversation()
        client = TestClient(create_app(ctl, "t"))
        r = client.post("/api/conversations/compact", headers=H, json={"conv": c.id, "mode": "trim"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["context"]["estimated"])
        r = client.post("/api/conversations/compact", headers=H, json={"conv": c.id, "mode": "bogus"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("context", client.get(f"/api/state?conv={c.id}", headers=H).json())


if __name__ == "__main__":
    unittest.main()
