"""Several conversations at the same time: each with its own timeline, approvals and Stop."""

import json
import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from pathlib import Path

os.environ["UVMC_NO_SUDO"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from anthropic.types import TextBlock, ToolUseBlock  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from test_service import ServiceBase, wait_for  # noqa: E402
from test_supervisor import reply  # noqa: E402
from uvmc.service import Busy, Controller, Unsaved, create_app  # noqa: E402
from uvmc import mcp_http  # noqa: E402

H = {"Authorization": "Bearer t"}


def say(text):
    return reply(TextBlock(type="text", text=text), stop="end_turn")


def ask_pip(cid):
    return reply(ToolUseBlock(type="tool_use", id=cid, name="run_command", input={"command": "pip install x"}))


def run(cid, command):
    return reply(ToolUseBlock(type="tool_use", id=cid, name="run_command", input={"command": command}))


class Router:
    """A fake model whose replies depend on the conversation's first task (its tag)."""

    def __init__(self, scripts):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.messages = self
        self.lock = threading.Lock()

    def create(self, **kwargs):
        first = json.dumps(kwargs["messages"][0])
        with self.lock:
            for tag, replies in self.scripts.items():
                if tag in first:
                    return replies.pop(0)
        raise AssertionError(f"no script for {first[:200]}")


class ConversationsTest(ServiceBase):
    def controller(self, scripts):
        self.router = Router(scripts)
        ctl = Controller(self.cfg, lambda *_: self.router)
        q = ctl.subscribe()

        def collect():
            while True:
                e = q.get()
                if "seq" in e:
                    self.events.append(e)
        threading.Thread(target=collect, daemon=True).start()
        return ctl

    def test_two_work_side_by_side(self):
        ctl = self.controller({
            "alpha": [run("a1", "sleep 0.5; echo A > a.txt"), say("Alpha done.")],
            "beta": [run("b1", "echo B > b.txt"), say("Beta done.")],
        })
        a = ctl.conv()
        b = ctl.new_conversation()
        ctl.start_task("alpha task", a.id)
        ctl.start_task("beta task", b.id)
        self.assertEqual(len(ctl.running()), 2)
        self.assertTrue(wait_for(lambda: not ctl.anything_running(), 10))
        self.assertEqual((a.session.last_text, b.session.last_text), ("Alpha done.", "Beta done."))
        # Each conversation has its own log, and every live event names its conversation.
        self.assertNotEqual(a.log.name, b.log.name)
        self.assertEqual({r["kind"] for r in a.log.read()} >= {"task", "result", "end"}, True)
        ends = {e["conv"]: e for e in self.events if e["kind"] == "end"}
        self.assertEqual(set(ends), {a.id, b.id})
        # Each worked in its own copy; the main folder is untouched until you bring them in.
        self.assertEqual(open(os.path.join(b.copy_dir, "b.txt")).read(), "B\n")
        self.assertFalse(os.path.exists(os.path.join(a.copy_dir, "b.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.cfg.workdir, "b.txt")))
        self.assertEqual(a.title, "alpha task")
        # The panel's list
        view = {c["id"]: c for c in ctl.conversations()}
        self.assertEqual(view[b.id]["title"], "beta task")
        self.assertTrue(view[b.id]["current"])

    def test_approval_and_stop_are_per_conversation(self):
        ctl = self.controller({
            "alpha": [ask_pip("a1"), say("A finished.")],
            "beta": [run("b1", "sleep 3"), say("B finished.")],
        })
        a = ctl.conv()
        b = ctl.new_conversation()
        ctl.start_task("alpha", a.id)
        ctl.start_task("beta", b.id)
        self.assertTrue(wait_for(lambda: a.state == "waiting"))
        self.assertEqual(b.state, "running")                       # only A waits for you
        self.assertEqual(len(ctl.snapshot(a.id)["pending"]), 1)
        self.assertEqual(ctl.snapshot(b.id)["pending"], [])
        view = {c["id"]: c for c in ctl.conversations()}
        self.assertEqual((view[a.id]["waiting"], view[b.id]["waiting"]), (1, 0))
        ctl.stop(b.id)                                             # stopping B leaves A's question
        self.assertTrue(wait_for(lambda: b.state == "idle", 10))
        self.assertEqual(a.state, "waiting")
        ctl.decide(ctl.snapshot(a.id)["pending"][0]["id"], "allow")
        self.assertTrue(wait_for(lambda: a.state == "idle", 10))
        self.assertEqual(a.session.last_text, "A finished.")
        b_end = [r for r in b.log.read() if r["kind"] == "end"][-1]
        self.assertEqual(b_end["reason"], "stopped by you")

    def test_limits(self):
        self.cfg.conv_max_running, self.cfg.conv_max_open = 1, 3
        ctl = self.controller({"alpha": [ask_pip("a1")]})
        a = ctl.conv()
        ctl.start_task("alpha", a.id)
        b = ctl.new_conversation()
        with self.assertRaises(Busy):                               # one at a time here
            ctl.start_task("beta", b.id)
        with self.assertRaises(Busy):
            ctl.close_conversation(a.id)                            # it's working
        c = ctl.new_conversation()
        d = ctl.new_conversation()                                  # over 3: the oldest idle one closes
        self.assertNotIn(b.id, ctl.convs)
        self.assertEqual(set(ctl.convs), {a.id, c.id, d.id})
        ctl.stop(a.id)
        self.assertTrue(wait_for(lambda: a.state == "idle", 10))
        for cid in list(ctl.convs):
            ctl.close_conversation(cid)
        self.assertEqual(len(ctl.convs), 1)                         # always one to type into
        self.assertEqual(ctl.current, next(iter(ctl.convs)))

    def test_a_new_site_is_asked_only_where_it_came_from(self):
        ctl = self.controller({"alpha": [run("a1", "sleep 3")], "beta": [run("b1", "sleep 3")]})
        a = ctl.conv()
        b = ctl.new_conversation()
        ctl.start_task("alpha", a.id)
        ctl.start_task("beta", b.id)
        self.assertTrue(wait_for(lambda: a.session.current_call and b.session.current_call))
        answer = {}
        # a's proxy got the connection: each conversation has its own
        t = threading.Thread(target=lambda: answer.setdefault("ok", ctl.decide_site(a, "example.org", 443)))
        t.start()
        self.assertTrue(wait_for(lambda: a.state == "waiting"))
        self.assertEqual(b.state, "running")                                  # b isn't asked
        self.assertEqual(ctl.snapshot(b.id)["pending"], [])
        card = ctl.snapshot(a.id)["pending"][0]
        self.assertEqual(card["call"], "a1")                                  # on a's own step
        ctl.decide(card["id"], "allow")
        t.join(5)
        self.assertTrue(answer["ok"])
        self.assertTrue(ctl.decide_site(a, "example.org", 443))               # allowed for a's task
        self.assertTrue(wait_for(lambda: a.state == "running"))
        t2 = threading.Thread(target=lambda: answer.setdefault("b", ctl.decide_site(b, "example.org", 443)))
        t2.start()                                                            # not for b's
        self.assertTrue(wait_for(lambda: b.state == "waiting"))
        ctl.decide(ctl.snapshot(b.id)["pending"][0]["id"], "deny")
        t2.join(5)
        self.assertFalse(answer["b"])
        ctl.stop(a.id)
        ctl.stop(b.id)
        self.assertTrue(wait_for(lambda: not ctl.anything_running(), 10))
        self.assertEqual((a.task_sites, b.task_sites), ({}, {}))           # "for this task" ends with it

    def test_api(self):
        ctl = self.controller({"alpha": [say("Hi from A.")], "beta": [say("Hi from B.")]})
        c = TestClient(create_app(ctl, "t"))
        first = c.get("/api/state", headers=H).json()["conv"]
        second = c.post("/api/conversations", headers=H, json={}).json()["conv"]
        self.assertNotEqual(first, second)
        r = c.post("/api/task", headers=H, json={"text": "alpha", "conv": first}).json()
        self.assertEqual(r["conv"], first)
        c.post("/api/task", headers=H, json={"text": "beta", "conv": second})
        self.assertTrue(wait_for(lambda: not ctl.anything_running()))
        events = c.get(f"/api/conversations/{first}/events", headers=H).json()
        self.assertIn("Hi from A.", json.dumps(events))
        self.assertNotIn("Hi from B.", json.dumps(events))
        self.assertEqual({e["conv"] for e in events}, {first})
        listed = c.get("/api/conversations", headers=H).json()
        self.assertEqual([x["id"] for x in listed], [first, second])
        self.assertEqual(c.post("/api/conversations/close", headers=H, json={"conv": second}).json()["current"], first)
        self.assertEqual(c.get("/api/state?conv=nope", headers=H).status_code, 404)
        self.assertEqual(c.post("/api/task", headers=H, json={"text": "x", "conv": second}).status_code, 404)
        # A script's own conversation: next to the panel's, which stays as it was.
        self.router.scripts["gamma"] = [say("Gamma.")]
        r = c.post("/api/run", headers=H, json={"text": "gamma", "new": True}).json()
        self.assertTrue(r["done"])
        self.assertNotEqual(r["conv"], first)
        self.assertEqual({x["origin"] for x in ctl.conversations()}, {"panel", "api"})

    def test_claude_apps_get_their_own_conversation(self):
        ctl = self.controller({"alpha": [ask_pip("a1")], "from the app": [say("App task done.")]})
        mine = ctl.conv()
        ctl.start_task("alpha", mine.id)                            # you are busy in the panel
        self.assertTrue(wait_for(lambda: mine.state == "waiting"))
        out = mcp_http.call(ctl, "run_task", {"task": "from the app", "wait_seconds": 10})
        self.assertIn("App task done.", out["content"][0]["text"])
        app = ctl.conv(ctl.mcp_conv)
        self.assertEqual(app.origin, "claude-app")
        self.assertEqual(ctl.current, mine.id)                      # your view isn't taken over
        status = mcp_http.call(ctl, "task_status", {})["content"][0]["text"]
        self.assertIn(f"Conversation {app.id}. State: idle", status)
        ctl.stop(mine.id)
        self.assertTrue(wait_for(lambda: mine.state == "idle", 10))

    def test_claude_code_calls_reach_their_own_conversation(self):
        ctl = self.controller({})
        a = ctl.conv()
        b = ctl.new_conversation()
        self.assertIn("run_command", json.dumps(ctl._bridge({"op": "list", "conv": b.key})))
        self.assertIn("closed", ctl._bridge({"op": "list", "conv": "not-a-key"})["text"])
        ans = ctl._bridge({"op": "call", "conv": a.key, "name": "run_command", "args": {"command": "echo"}})
        self.assertIn("No task is running", ans["text"])
        self.assertIn(a.key, json.dumps(a.session.bridge_key))

    def test_claude_code_conversations_dont_wait_for_each_other(self):
        """Found in a real test: a long command in one "my plan" conversation held the other's
        tool calls (the lock was for all conversations)."""
        ctl = self.controller({})
        a = ctl.conv()
        b = ctl.new_conversation()
        for c in (a, b):
            ctl.ensure_copy(c)
            c.session.brain = SimpleNamespace(match=lambda name, args: "call-" + name, idle_since=None,
                                              close=lambda: None)
            c.state = "running"
        slow = threading.Thread(target=ctl._bridge, args=({"op": "call", "conv": a.key, "name": "run_command",
                                                            "args": {"command": "sleep 2"}},))
        slow.start()
        time.sleep(0.3)
        t0 = time.time()
        ans = ctl._bridge({"op": "call", "conv": b.key, "name": "run_command", "args": {"command": "echo quick"}})
        self.assertIn("quick", ans["text"])
        self.assertLess(time.time() - t0, 1.5)
        slow.join(5)
        for c in (a, b):
            c.state = "idle"

    def test_undo_is_per_conversation(self):
        ctl = self.controller({"alpha": [ask_pip("a1")],
                               "beta": [run("b1", "echo B > b.txt"), say("ok")]})
        a = ctl.conv()
        b = ctl.new_conversation()
        ctl.start_task("alpha", a.id)
        ctl.start_task("beta", b.id)
        self.assertTrue(wait_for(lambda: a.state == "waiting" and b.state == "idle"))
        cp = [r for r in b.log.read() if r["kind"] == "checkpoint"][0]["id"]
        ctl.undo(cp, b.id)                                                    # while a works
        self.assertFalse(os.path.exists(os.path.join(b.copy_dir, "b.txt")))
        with self.assertRaises(Busy):
            ctl.undo(cp, a.id)                                                # a is working
        ctl.stop(a.id)
        self.assertTrue(wait_for(lambda: a.state == "idle", 10))


class WorkCopyTest(ServiceBase):
    """Bringing a conversation's work into the main folder, and the main folder's into it."""

    def setUp(self):
        super().setUp()
        self.shared = Path(self.cfg.workdir)
        (self.shared / "notes.txt").write_text("one\ntwo\nthree\nfour\nfive\n")
        (self.shared / "data.bin").write_bytes(b"\x00\x01")

    controller = ConversationsTest.controller

    def copies(self, ctl, *convs):
        for c in convs:
            ctl.ensure_copy(c)
        return [Path(c.copy_dir) for c in convs]

    def test_bring_in_get_and_merge(self):
        ctl = self.controller({})
        a, b = ctl.conv(), ctl.new_conversation()
        ca, cb = self.copies(ctl, a, b)
        self.assertEqual((ca / "notes.txt").read_text(), (self.shared / "notes.txt").read_text())
        (ca / "report.csv").write_text("x,y\n")                       # a adds a file
        (ca / "notes.txt").write_text("ONE\ntwo\nthree\nfour\nfive\n")   # and edits the top
        (cb / "notes.txt").write_text("one\ntwo\nthree\nfour\nFIVE\n")   # b edits the bottom
        plan = ctl.changes(a.id)
        self.assertEqual(sorted(x["path"] for x in plan["mine"]), ["notes.txt", "report.csv"])
        self.assertEqual(ctl.unsaved(a), 2)
        r = ctl.sync(a.id)
        self.assertEqual(sorted(r["brought_in"]), ["notes.txt", "report.csv"])
        self.assertTrue(r["undo"])
        self.assertEqual((self.shared / "report.csv").read_text(), "x,y\n")
        self.assertEqual(ctl.unsaved(a), 0)
        # b now sees a's file to get, and notes.txt changed on both sides in different places
        plan = ctl.changes(b.id)
        self.assertEqual([x["path"] for x in plan["theirs"]], ["report.csv"])
        self.assertEqual([x["path"] for x in plan["merged"]], ["notes.txt"])
        r = ctl.sync(b.id)
        merged = "ONE\ntwo\nthree\nfour\nFIVE\n"
        self.assertEqual((self.shared / "notes.txt").read_text(), merged)   # both changes kept
        self.assertEqual((cb / "notes.txt").read_text(), merged)
        self.assertEqual((cb / "report.csv").read_text(), "x,y\n")       # b got a's file
        self.assertEqual(ctl.changes(b.id)["theirs"] + ctl.changes(b.id)["mine"], [])
        # a gets b's merge next time; its own copy wasn't touched meanwhile
        self.assertEqual((ca / "notes.txt").read_text(), "ONE\ntwo\nthree\nfour\nfive\n")
        self.assertEqual([x["path"] for x in ctl.changes(a.id)["theirs"]], ["notes.txt"])

    def make_conflicts(self, ctl, a, b):
        ca, cb = self.copies(ctl, a, b)
        (ca / "notes.txt").write_text("one\nTWO from a\nthree\nfour\nfive\n")
        (cb / "notes.txt").write_text("one\nTWO from b\nthree\nfour\nfive\n")
        (ca / "data.bin").write_bytes(b"\x00a")
        (cb / "data.bin").write_bytes(b"\x00b")
        ctl.sync(a.id)
        return ca, cb

    def test_conflicts_go_to_the_agent(self):
        """No choices: every conflict goes to this conversation's agent, text or not, with the context."""
        ctl = self.controller({"These files": [run("m1", "cat .uvmc-merge/notes.txt.main > notes.txt && "
                                                          "echo 'TWO from b' >> notes.txt && "
                                                          "cp .uvmc-merge/data.bin.main data.bin"),
                                               say("Resolved.")]})
        a, b = ctl.conv(), ctl.new_conversation()
        a.title = "alpha work"
        ca, cb = self.make_conflicts(ctl, a, b)
        plan = {x["path"]: x for x in ctl.changes(b.id)["conflicts"]}
        self.assertEqual(plan["notes.txt"]["other"]["title"], "alpha work")          # it could resolve them instead
        self.assertNotIn("suggested", plan["data.bin"])                               # UVMC suggests nothing itself
        r = ctl.sync(b.id)
        self.assertEqual(sorted(r["agent_merging"]), ["data.bin", "notes.txt"])
        self.assertTrue(wait_for(lambda: b.state == "idle" and b.session.last_text == "Resolved.", 10))
        task = [x for x in b.log.read() if x["kind"] == "task"][0]["text"]
        self.assertIn("alpha work", task)                                             # what the other side was for
        self.assertNotIn(a.copy_dir, task)                                            # no agent opens another's copy
        self.assertIn(".uvmc-merge/notes.txt.main", task)                             # the versions are handed over
        self.assertTrue(any(x["kind"] == "resolution_ready" for x in b.log.read()))
        prop = {f["path"]: f for f in ctl.resolution(b.id)["files"]}
        self.assertEqual(set(prop), {"notes.txt", "data.bin"})
        self.assertTrue(prop["data.bin"]["same_as_main"])                             # it chose the main one
        self.assertEqual(prop["notes.txt"]["reassign"]["title"], "alpha work")        # or alpha could do it
        ctl.resolve(b.id, {"notes.txt": "accept", "data.bin": "yours"})               # you pick per file
        self.assertIn("TWO from a", (self.shared / "notes.txt").read_text())
        self.assertIn("TWO from b", (self.shared / "notes.txt").read_text())
        self.assertEqual((self.shared / "data.bin").read_bytes(), b"\x00b")         # b's own, as you chose
        self.assertEqual(ctl.unsaved(b), 0)

    def test_the_other_conversation_resolves(self):
        """Chosen on the card: the conversation whose version is in the main folder resolves it."""
        ctl = self.controller({"The user asked you to resolve": [
            run("o1", "cat .uvmc-merge/notes.txt.other >> notes.txt"), say("Resolved on my side.")]})
        a, b = ctl.conv(), ctl.new_conversation()
        a.title = "alpha work"
        ca, cb = self.make_conflicts(ctl, a, b)
        ctl.sync(b.id, {"notes.txt": f"agent:{a.id}", "data.bin": "later"})
        self.assertTrue(wait_for(lambda: a.state == "idle" and a.session.last_text == "Resolved on my side.", 10))
        task = [x for x in a.log.read() if x["kind"] == "task"][0]["text"]
        self.assertIn(".uvmc-merge/notes.txt.other", task)
        self.assertNotIn("notes.txt", [x["path"] for x in ctl.changes(b.id)["conflicts"]])   # b waits for a's result
        ch = ctl.changes(b.id)
        self.assertNotIn("notes.txt", [x["path"] for x in ch["theirs"]])                 # not the main folder's old one
        self.assertEqual(ch["handed"], [{"path": "notes.txt", "conv": a.id, "title": "alpha work"}])
        ctl.sync(b.id, {"data.bin": "later"})                                            # bringing in meanwhile
        self.assertIn("TWO from b", (cb / "notes.txt").read_text())                      # leaves b's version alone
        self.assertIn("review your proposal", task)                                      # it doesn't say "can't push"
        prop = ctl.resolution(a.id)["files"][0]
        self.assertTrue(prop["has"]["other"])                                            # b's version is an option
        self.assertEqual(prop["other_title"], "untitled")
        ctl.resolve(a.id, {"notes.txt": "accept"})                                       # you accept a's proposal
        both = (self.shared / "notes.txt").read_text()
        self.assertIn("TWO from a", both)
        self.assertIn("TWO from b", both)
        ctl.sync(b.id, {"data.bin": "later"})                                            # and b gets it
        self.assertEqual((cb / "notes.txt").read_text(), both)

    def test_handed_file_comes_back_as_a_conflict(self):
        """Decide later on the other's proposal, or close it: the conflict is open again where it was."""
        ctl = self.controller({"The user asked you to resolve": [say("Done."), say("Done again.")]})
        a, b = ctl.conv(), ctl.new_conversation()
        ca, cb = self.make_conflicts(ctl, a, b)
        ctl.sync(b.id, {"notes.txt": f"agent:{a.id}", "data.bin": "later"})
        self.assertTrue(wait_for(lambda: a.state == "idle" and a.session.last_text == "Done.", 10))
        ctl.resolve(a.id, {"notes.txt": "later"})
        self.assertEqual(ctl.changes(a.id)["conflicts"], [])                             # never a's conflict
        self.assertIn("notes.txt", [x["path"] for x in ctl.changes(b.id)["conflicts"]])  # b's again
        self.assertEqual(ctl.changes(b.id)["handed"], [])
        ctl.sync(b.id, {"notes.txt": f"agent:{a.id}", "data.bin": "later"})
        self.assertTrue(wait_for(lambda: a.state == "idle" and a.session.last_text == "Done again.", 10))
        ctl.close_conversation(a.id, discard=True)
        self.assertIn("notes.txt", [x["path"] for x in ctl.changes(b.id)["conflicts"]])
        self.assertIn("TWO from b", (cb / "notes.txt").read_text())

    def test_after_the_proposal_you_can_still_pick_later_or_the_other_conversation(self):
        ctl = self.controller({"The user asked you to resolve": [say("Mine is fine.")],   # matched first
                               "These files": [run("m1", "echo mixed > notes.txt"), say("Proposal."),
                                               say("Proposal again.")]})
        a, b = ctl.conv(), ctl.new_conversation()
        a.title = "alpha work"
        ca, cb = self.make_conflicts(ctl, a, b)
        ctl.sync(b.id, {"data.bin": "later"})
        self.assertTrue(wait_for(lambda: b.state == "idle" and b.session.last_text == "Proposal.", 10))
        ctl.resolve(b.id, {"notes.txt": "later"})
        self.assertEqual((cb / "notes.txt").read_text(), "one\nTWO from b\nthree\nfour\nfive\n")  # b's again
        self.assertEqual(sorted(x["path"] for x in ctl.changes(b.id)["conflicts"]), ["data.bin", "notes.txt"])
        ctl.sync(b.id, {"data.bin": "later", "notes.txt": "agent"})
        self.assertTrue(wait_for(lambda: b.state == "idle" and ctl.changes(b.id)["resolving"] == ["notes.txt"], 10))
        ctl.resolve(b.id, {"notes.txt": f"agent:{a.id}"})                             # let alpha do it instead
        self.assertTrue(wait_for(lambda: a.state == "idle" and a.session.last_text == "Mine is fine.", 10))
        self.assertEqual(ctl.changes(a.id)["resolving"], ["notes.txt"])               # alpha's proposal now
        self.assertEqual(ctl.changes(b.id)["resolving"], [])

    def test_the_resolving_agent_gets_the_other_conversations_log(self):
        ctl = self.controller({"These files": [say("Read their log.")],   # matched first: it quotes alpha's task
                               "alpha": [run("a1", "echo 'from alpha' > notes.txt"), say("Wrote the alpha line.")]})
        a, b = ctl.conv(), ctl.new_conversation()
        ctl.start_task("alpha: put the alpha line in notes", a.id)
        self.assertTrue(wait_for(lambda: a.state == "idle" and a.session.last_text == "Wrote the alpha line.", 10))
        (cb,) = self.copies(ctl, b)
        (cb / "notes.txt").write_text("from beta\n")
        ctl.sync(a.id)
        ctl.close_conversation(a.id)                              # even after it's closed
        ctl.sync(b.id)
        self.assertTrue(wait_for(lambda: b.state == "idle" and b.session.last_text == "Read their log.", 10))
        task = [x for x in b.log.read() if x["kind"] == "task"][0]["text"]
        self.assertIn(".uvmc-merge/conversation-alpha-put-the-alpha-line-in-notes.md", task)
        log = (cb / ".uvmc-merge" / "conversation-alpha-put-the-alpha-line-in-notes.md").read_text()
        self.assertIn("alpha: put the alpha line in notes", log)          # what it was asked
        self.assertIn("> run_command: echo 'from alpha' > notes.txt", log)  # what it did
        self.assertIn("Wrote the alpha line.", log)                         # what it answered
        self.assertIn("Brought into the main folder: notes.txt", log)

    def test_long_logs_are_shortened_but_keep_every_task(self):
        from uvmc.transcript import transcript
        records = [{"kind": "task", "text": f"task {i}", "t": 0} for i in range(3)]
        records += [{"kind": "result", "text": "x" * 500 + "\n" * 50, "is_error": False}] * 2000
        text = transcript(records, "big", limit=20000)
        self.assertLessEqual(len(text), 21000)
        for i in range(3):
            self.assertIn(f"task {i}", text)

    def test_your_choices_win(self):
        ctl = self.controller({})
        a, b = ctl.conv(), ctl.new_conversation()
        ca, cb = self.make_conflicts(ctl, a, b)
        r = ctl.sync(b.id, {"data.bin": "mine", "notes.txt": "later"})
        self.assertEqual(r["agent_merging"], [])
        self.assertEqual((self.shared / "data.bin").read_bytes(), b"\x00b")
        self.assertEqual(r["open_conflicts"], ["notes.txt"])
        self.assertIn("from a", (self.shared / "notes.txt").read_text())                    # untouched
        with self.assertRaises(Unsaved):                                                   # closing asks first
            ctl.close_conversation(b.id)
        ctl.sync(b.id, {"notes.txt": "main"})
        self.assertEqual((cb / "notes.txt").read_text(), (self.shared / "notes.txt").read_text())
        self.assertEqual(ctl.unsaved(b), 0)
        ctl.close_conversation(b.id)
        self.assertFalse(cb.exists())                                                      # its copy is gone

    def test_the_agent_merges_a_conflict(self):
        ctl = self.controller({"These files": [run("m1", "cat .uvmc-merge/notes.txt.main > notes.txt && "
                                                          "echo merged-by-agent >> notes.txt"),
                                               say("Merged.")]})
        a, b = ctl.conv(), ctl.new_conversation()
        ca, cb = self.copies(ctl, a, b)
        (ca / "notes.txt").write_text("from a\n")
        (cb / "notes.txt").write_text("from b\n")
        ctl.sync(a.id)
        ctl.merge_with_agent(b.id, ["notes.txt"])
        self.assertTrue(wait_for(lambda: b.state == "idle", 10))
        task = [r for r in b.log.read() if r["kind"] == "task"][0]["text"]
        self.assertIn(".uvmc-merge/notes.txt.main", task)
        self.assertIn("The main folder's version came from", task)   # what the other side was for
        plan = ctl.changes(b.id)
        self.assertEqual(plan["resolving"], ["notes.txt"])                 # a proposal, waiting for you
        self.assertEqual(plan["mine"] + plan["conflicts"], [])
        ctl.sync(b.id)                                                      # bringing in leaves it alone
        self.assertEqual((self.shared / "notes.txt").read_text(), "from a\n")
        prop = ctl.resolution(b.id)
        self.assertEqual(prop["explanation"], "Merged.")
        self.assertIn("+merged-by-agent", prop["files"][0]["diff"])        # its result, against the main folder
        ctl.resolve(b.id, {"notes.txt": "accept"})
        self.assertEqual((self.shared / "notes.txt").read_text(), "from a\nmerged-by-agent\n")
        self.assertEqual(ctl.unsaved(b), 0)
        self.assertFalse((cb / ".uvmc-merge").exists())

    def test_undo_a_bring_in(self):
        ctl = self.controller({})
        a = ctl.conv()
        (ca,) = self.copies(ctl, a)
        (ca / "notes.txt").write_text("changed\n")
        r = ctl.sync(a.id)
        ctl.undo_bring_in(r["undo"], a.id)
        self.assertEqual((self.shared / "notes.txt").read_text(), "one\ntwo\nthree\nfour\nfive\n")
        self.assertEqual((ca / "notes.txt").read_text(), "changed\n")        # the copy keeps it
        self.assertEqual([x["path"] for x in ctl.changes(a.id)["mine"]], ["notes.txt"])

    def test_after_a_restart_unsaved_work_is_still_there(self):
        ctl = self.controller({"alpha": [run("a1", "echo kept > kept.txt"), say("ok")]})
        a = ctl.conv()
        ctl.start_task("alpha", a.id)
        self.assertTrue(wait_for(lambda: a.state == "idle" and os.path.exists(os.path.join(a.copy_dir, "kept.txt"))))
        again = Controller(self.cfg, lambda *_: None)                     # as after a restart
        back = again.conv(a.id)
        self.assertEqual((back.title, back.slot), ("alpha", a.slot))
        self.assertEqual([x["path"] for x in again.changes(a.id)["mine"]], ["kept.txt"])


class BoxTest(unittest.TestCase):
    def test_each_conversation_has_its_own_box_inside_the_agents(self):
        from test_supervisor import make_cfg
        from uvmc.executor import SystemdExecutor, conversation_slice, slice_of_pid
        cfg = make_cfg("/tmp", "/tmp/logs")
        name = conversation_slice("ab12cd34")
        self.assertEqual(name, "uvmc-agent-ab12cd34.slice")      # systemd nests it in uvmc-agent.slice
        cmd = SystemdExecutor(cfg, name).command("true", (), 10)
        self.assertIn(f"--slice={name}", cmd)
        self.assertIsNone(slice_of_pid(os.getpid()))              # this test runs in no conversation's box


if __name__ == "__main__":
    unittest.main()
