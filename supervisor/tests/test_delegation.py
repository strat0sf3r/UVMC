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
from uvmc.tools import Tools  # noqa: E402
from test_conversations import Router, run, say  # noqa: E402


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
        ctl = self.setup_ctl([],
            [reply(tool("w1", "write_file", path="hello.txt", content="hi")),
             reply(text("Created hello.txt with 'hi'."), stop="end_turn")],
        )
        cid = ctl.conv().id
        self.boss.replies = [
            reply(text("I'll have the worker write it."),
                  tool("d1", "delegate", instructions="Create hello.txt containing 'hi' in the work folder. Report when done.")),
            reply(tool("b1", "bring_in_worker", job=f"{cid}w1")),
            reply(tool("r1", "read_file", path="hello.txt")),
            reply(text("Done: hello.txt says hi."), stop="end_turn")]
        job = Path(self.cfg.conv_root) / f"{cid}w1"
        seen = {}
        real = ctl._bring_in_job

        def bring_in(c, args, call_id):   # just before: the file is in the job's folder, not the coordinator's
            seen["job"] = (job / "hello.txt").read_text()
            seen["mine"] = Path(c.copy_dir, "hello.txt").exists()
            return real(c, args, call_id)
        ctl._bring_in_job = bring_in
        ctl.start_task("make a hello file")
        self.assertTrue(wait_for(lambda: ctl.state == "idle" and any(e["kind"] == "end" and "parent" not in e for e in self.events)))

        boss_tools = [t["name"] for t in self.boss.sent[0]["tools"]]
        self.assertEqual(sorted(boss_tools), ["bring_in_worker", "delegate", "discard_worker", "list_dir",
                                              "propose_tool", "read_file"])
        self.assertEqual(seen, {"job": "hi", "mine": False})   # its own folder until brought in
        self.assertFalse(job.exists())                          # removed once brought in
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
        self.assertIn("hello.txt (added)", report["content"])
        self.assertIn("Your work folder is " + str(job), self.worker.sent[0]["system"])
        self.assertFalse(report["is_error"])
        brought = self.boss.sent[2]["messages"][-1]["content"][0]
        self.assertIn("brought in: hello.txt", brought["content"])
        self.assertEqual(Path(ctl.conv().copy_dir, "hello.txt").read_text(), "hi")   # the conversation's copy

        worker_events = [e for e in self.events if e.get("parent") == "d1"]
        self.assertEqual([e["kind"] for e in worker_events][:3], ["job", "task", "reply"])
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
        real = Tools.run

        def run(self_tools, name, args):   # each job runs commands in its own box
            ctl._on_exec({"t": time.time(), "pid": 5, "ppid": 1, "args": ["ls", "-la"], "exe": "/usr/bin/ls"})
            return real(self_tools, name, args)

        Tools.run = run
        self.addCleanup(setattr, Tools, "run", real)
        ctl.start_task("list")
        self.assertTrue(wait_for(lambda: ctl.state == "idle" and any(e["kind"] == "end" and "parent" not in e for e in self.events)))
        execs = [e for e in self.events if e["kind"] == "exec"]
        self.assertEqual(execs[0]["call"], "w1")


class ParallelJobsTest(DelegationBase):
    """Several jobs at once, each in its own copy of the coordinator's folder."""

    def start(self, scripts, files=None):
        for name, data in (files or {}).items():
            Path(self.cfg.workdir, name).write_text(data)
        self.boss = FakeClient([])
        self.router = Router(scripts)
        clients = {"claude": self.boss, "deepseek": self.router}
        ctl = Controller(self.cfg, lambda p: clients[p.name])
        q = ctl.subscribe()
        import threading
        threading.Thread(target=lambda: [self.events.append(q.get()) for _ in iter(int, 1)], daemon=True).start()
        ctl.set_worker("deepseek")
        return ctl

    def run_until_idle(self, ctl, text="go"):
        ctl.start_task(text)
        self.assertTrue(wait_for(lambda: ctl.state == "idle" and any(
            e["kind"] == "end" and "parent" not in e for e in self.events), 20))

    def result(self, call):
        return next(e for e in self.events if e["kind"] == "result" and e["id"] == call)

    def test_two_jobs_run_together_and_are_brought_in(self):
        ctl = self.start({
            "alpha job": [run("a1", "sleep 0.6; echo A > a.txt"), say("Alpha written.")],
            "beta job": [run("b1", "sleep 0.6; echo B > b.txt"), say("Beta written.")],
        })
        cid = ctl.conv().id
        self.boss.replies = [
            reply(tool("d1", "delegate", instructions="alpha job: write a.txt"),
                  tool("d2", "delegate", instructions="beta job: write b.txt")),
            reply(tool("g1", "bring_in_worker", job=f"{cid}w1"), tool("g2", "bring_in_worker", job=f"{cid}w2")),
            reply(text("Both are in."), stop="end_turn")]
        t0 = time.time()
        self.run_until_idle(ctl)
        self.assertLess(time.time() - t0, 1.15)   # together: about 0.6 s, not 1.2
        tasks = [e["seq"] for e in self.events if e["kind"] == "task" and e.get("parent")]
        ends = [e["seq"] for e in self.events if e["kind"] == "end" and e.get("parent")]
        self.assertEqual(len(tasks), 2)
        self.assertLess(max(tasks), min(ends))    # both started before either ended
        jobs = {e["parent"]: e for e in self.events if e["kind"] == "job"}
        self.assertNotEqual(jobs["d1"]["folder"], jobs["d2"]["folder"])
        report = self.boss.sent[1]["messages"][-1]["content"]
        self.assertEqual([r["tool_use_id"] for r in report], ["d1", "d2"])   # in the calls' order
        mine = Path(ctl.conv().copy_dir)
        self.assertEqual((mine / "a.txt").read_text(), "A\n")
        self.assertEqual((mine / "b.txt").read_text(), "B\n")
        self.assertEqual(ctl.conv().jobs, {})
        self.assertFalse(any(Path(self.cfg.conv_root, f"{cid}w{i}").exists() for i in (1, 2)))
        self.assertEqual(sorted(ctl.job_slots), list(range(self.cfg.conv_max_open + 1,
                                                           self.cfg.conv_max_open + self.cfg.delegate_slots + 1)))

    def test_parallel_limit(self):
        self.cfg.delegate_parallel = 1
        ctl = self.start({
            "alpha job": [run("a1", "sleep 0.3; echo A > a.txt"), say("A.")],
            "beta job": [run("b1", "echo B > b.txt"), say("B.")],
        })
        self.boss.replies = [
            reply(tool("d1", "delegate", instructions="alpha job"), tool("d2", "delegate", instructions="beta job")),
            reply(text("ok"), stop="end_turn")]
        self.run_until_idle(ctl)
        order = [(e["kind"], e["parent"]) for e in self.events if e.get("parent") and e["kind"] in ("task", "end")]
        first = order[0][1]
        self.assertEqual(order[:2], [("task", first), ("end", first)])   # one after the other

    def test_merge_and_conflict_between_jobs(self):
        ctl = self.start({
            # lines far enough apart: git merge-file counts changes to neighbouring lines as a conflict
            "first job": [run("a1", "printf 'ONE\\n2\\n3\\n4\\nfive\\n' > list.txt; echo A > same.txt"), say("1.")],
            "second job": [run("b1", "printf 'one\\n2\\n3\\n4\\nFIVE\\n' > list.txt; echo B > same.txt"), say("2.")],
        }, files={"list.txt": "one\n2\n3\n4\nfive\n", "same.txt": "x\n"})
        cid = ctl.conv().id
        self.boss.replies = [
            reply(tool("d1", "delegate", instructions="first job"), tool("d2", "delegate", instructions="second job")),
            reply(tool("g1", "bring_in_worker", job=f"{cid}w1")),
            reply(tool("g2", "bring_in_worker", job=f"{cid}w2")),
            reply(tool("g3", "bring_in_worker", job=f"{cid}w2", choices={"same.txt": "worker"})),
            reply(text("Done."), stop="end_turn")]
        self.run_until_idle(ctl)
        mine = Path(ctl.conv().copy_dir)
        self.assertEqual((mine / "list.txt").read_text(), "ONE\n2\n3\n4\nFIVE\n")   # merged line by line
        second = self.result("g2")["text"]
        self.assertIn("merged line by line with your changes: list.txt", second)
        self.assertIn("Still open, changed both in its folder and in yours: same.txt", second)
        self.assertEqual((mine / "same.txt").read_text(), "B\n")                    # the worker's, as chosen
        self.assertIn("Its folder is removed", self.result("g3")["text"])
        self.assertEqual(ctl.conv().jobs, {})

    def test_discard_unknown_and_nothing_changed(self):
        ctl = self.start({
            "edit job": [run("a1", "echo new > keep.txt"), say("Edited.")],
            "look job": [run("b1", "ls"), say("Looked.")],
        }, files={"keep.txt": "old\n"})
        cid = ctl.conv().id
        self.boss.replies = [
            reply(tool("d1", "delegate", instructions="edit job"), tool("d2", "delegate", instructions="look job")),
            reply(tool("g1", "bring_in_worker", job="nope"), tool("x1", "discard_worker", job=f"{cid}w1")),
            reply(text("Dropped."), stop="end_turn")]
        self.run_until_idle(ctl)
        self.assertIn("It changed no files", self.result("d2")["text"])
        self.assertIn("keep.txt (changed)", self.result("d1")["text"])
        self.assertIn(f"Jobs waiting to be brought in: {cid}w1", self.result("g1")["text"])
        self.assertIn("dropped", self.result("x1")["text"])
        self.assertEqual(Path(ctl.conv().copy_dir, "keep.txt").read_text(), "old\n")
        self.assertEqual(ctl.conv().jobs, {})
        self.assertFalse(Path(self.cfg.conv_root, f"{cid}w1").exists())

    def test_a_job_sees_the_conflict_files(self):
        """A coordinator resolving a conflict delegates the merge: the worker finds .uvmc-merge/ where
        the task says, and the merged file comes back with bring_in_worker."""
        ctl = self.start({"merge job": [run("a1", "cat .uvmc-merge/notes.txt.main >> notes.txt"), say("Merged.")]},
                         files={"notes.txt": "mine\n"})
        c = ctl.conv()
        ctl.ensure_copy(c)
        Path(c.copy_dir, ".uvmc-merge").mkdir()
        Path(c.copy_dir, ".uvmc-merge", "notes.txt.main").write_text("theirs\n")
        self.boss.replies = [reply(tool("d1", "delegate", instructions="merge job")),
                             reply(tool("g1", "bring_in_worker", job=f"{c.id}w1")),
                             reply(text("Merged."), stop="end_turn")]
        self.run_until_idle(ctl)
        self.assertEqual(Path(c.copy_dir, "notes.txt").read_text(), "mine\ntheirs\n")

    def test_closing_the_conversation_drops_waiting_jobs(self):
        ctl = self.start({"edit job": [run("a1", "echo x > x.txt"), say("ok")]})
        cid = ctl.conv().id
        self.boss.replies = [reply(tool("d1", "delegate", instructions="edit job")), reply(text("Later."), stop="end_turn")]
        self.run_until_idle(ctl)
        self.assertTrue(Path(self.cfg.conv_root, f"{cid}w1").exists())
        ctl.new_conversation()
        ctl.close_conversation(cid, discard=True)
        self.assertFalse(Path(self.cfg.conv_root, f"{cid}w1").exists())


if __name__ == "__main__":
    unittest.main()
