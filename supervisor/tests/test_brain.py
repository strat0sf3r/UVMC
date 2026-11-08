"""Claude Code on your plan as the brain, end to end with a stand-in for `claude`.

The stand-in (fake_claude.py) talks stream-json and calls tools through the real MCP
server and bridge, so this covers the process, the protocol, approvals and the panel log.
"""

import json
import os
import socket
import sys
import unittest
from pathlib import Path

os.environ["UVMC_NO_SUDO"] = "1"
HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from test_service import ServiceBase, wait_for  # noqa: E402
from uvmc import config, mcp_server  # noqa: E402
from uvmc.providers import make  # noqa: E402
from uvmc.service import Controller  # noqa: E402

TOKEN = "sk-ant-oat01-test-token"


class BrainTest(ServiceBase):
    def setUp(self):
        super().setUp()
        t = self.tmp.name
        # A small launcher in the temp folder (the repo folder may not allow running files).
        launcher = os.path.join(t, "claude")
        with open(launcher, "w") as f:
            f.write(f'#!/bin/sh\nexec "{sys.executable}" "{HERE / "fake_claude.py"}" "$@"\n')
        os.chmod(launcher, 0o755)
        self.cfg.claude_bin = launcher
        self.cfg.mcp_python = sys.executable
        self.cfg.mcp_script = str(ROOT / "uvmc" / "mcp_server.py")
        self.cfg.bridge_socket = os.path.join(t, "run", "bridge.sock")
        self.cfg.brain_dir = t
        self.cfg.brain_proxy_port = 0
        self.cfg.model = "claude-plan"
        self.log = os.path.join(t, "fake.json")

    def tearDown(self):
        if getattr(self, "ctl", None):
            self.ctl.session.close()
            self.ctl.bridge.stop()
        super().tearDown()

    def controller(self, token=TOKEN):
        ctl = Controller(self.cfg, lambda profile: make(profile, token if profile.provider == "claude-code" else None))
        q = ctl.subscribe()
        import threading
        threading.Thread(target=lambda: [self.events.append(q.get()) for _ in iter(int, 1)], daemon=True).start()
        ctl.start_background(proxy=False, audit=False)
        self.ctl = ctl
        return ctl

    def run_task(self, ctl, text):
        ctl.start_task(text)
        self.assertTrue(wait_for(lambda: ctl.state == "idle", 20), "task did not finish")
        # the panel's copy of the events arrives through a queue, a moment later
        self.assertTrue(wait_for(lambda: any(e["kind"] == "status" and e.get("state") == "idle"
                                             for e in self.events[-3:]), 5))

    def of(self, kind):
        return [e for e in self.events if e["kind"] == kind]

    def test_tool_call_goes_through_the_supervisor(self):
        ctl = self.controller()
        self.run_task(ctl, "run: echo from-the-plan")
        replies = self.of("reply")
        uses = [b for e in replies for b in e["content"] if b["type"] == "tool_use"]
        self.assertEqual(uses[0]["name"], "run_command")          # prefix stripped for the panel
        result = self.of("result")[0]
        self.assertEqual(result["id"], uses[0]["id"])            # the MCP call is linked to its tool_use
        self.assertEqual(result["text"], "exit code 0\nfrom-the-plan")
        self.assertEqual(self.of("decision")[0]["verdict"], "allow")
        self.assertIn("Result (ok)", replies[-1]["content"][0]["text"])
        self.assertEqual(self.of("end")[-1]["reason"], "done")
        # one model reply split over two messages is counted once
        self.assertEqual([e["usage"]["in"] for e in replies], [100, 0, 100])
        seen = json.load(open(self.log))
        self.assertEqual(seen["token"], TOKEN)
        self.assertIsNone(seen["api_key"])
        argv = seen["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "")     # none of its own tools
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertNotIn(TOKEN, " ".join(argv))                  # never on the command line

    def test_rules_and_approvals_apply(self):
        ctl = self.controller()
        ctl.start_task("run: rm -r stuff")                        # an ask pattern in the test config
        self.assertTrue(wait_for(lambda: ctl.state == "waiting", 20))
        pid = ctl.snapshot()["pending"][0]["id"]
        ctl.decide(pid, "deny")
        self.assertTrue(wait_for(lambda: ctl.state == "idle", 20))
        result = self.of("result")[0]
        self.assertTrue(result["is_error"])
        self.assertIn("Denied by the supervisor", result["text"])

    def test_conversation_continues_in_one_process(self):
        ctl = self.controller()
        self.run_task(ctl, "hello")
        first = ctl.session.brain.proc.pid
        self.run_task(ctl, "write: note.txt")
        self.assertEqual(ctl.session.brain.proc.pid, first)
        self.assertEqual(open(os.path.join(ctl.conv().copy_dir, "note.txt")).read(), "hello\n")   # its own copy
        ctl.new_session()
        self.assertIsNone(ctl.session.brain)

    def test_switching_from_an_api_model_carries_the_conversation(self):
        ctl = self.controller()
        ctl.session.messages = [{"role": "user", "text": "my name is Ada"},
                                {"role": "assistant", "text": "Nice to meet you, Ada."}]
        self.run_task(ctl, "hello")
        sent = open(self.log + ".msgs").read()
        self.assertIn("my name is Ada", sent)
        self.assertIn("(Now the user says:)", sent)
        self.assertEqual(ctl.session.messages[-1], {"role": "assistant", "text": "Hi from the plan."})

    def test_stop_interrupts(self):
        ctl = self.controller()
        ctl.start_task("wait")
        self.assertTrue(wait_for(lambda: any(e["kind"] == "reply" for e in self.events), 20))
        ctl.stop()
        self.assertTrue(wait_for(lambda: ctl.state == "idle", 20))
        self.assertEqual(self.of("end")[-1]["reason"], "stopped by you")

    def test_usage_limit_is_reported(self):
        ctl = self.controller()
        self.run_task(ctl, "limit")
        self.assertIn("usage limit", self.of("error")[-1]["text"])
        self.assertEqual(self.of("end")[-1]["reason"], "ended with an error")

    def test_not_signed_in(self):
        ctl = self.controller(token=None)
        self.run_task(ctl, "hello")
        self.assertIn("Sign in with your Claude plan", self.of("error")[-1]["text"])

    def test_bridge_refuses_outside_a_task_and_strangers(self):
        ctl = self.controller()
        ans = mcp_server.ask_supervisor(self.cfg.bridge_socket, {"op": "call", "name": "run_command",
                                                                 "args": {"command": "echo hi"}}, timeout=5)
        self.assertTrue(ans["is_error"])
        self.assertIn("No task is running", ans["text"])
        ctl.bridge.allowed = {os.getuid() + 12345}               # as if the agent user knocked
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(self.cfg.bridge_socket)
            s.sendall(b'{"op": "list"}\n')
            self.assertIn(b"Not allowed", s.recv(1000))

    def test_plan_models_are_not_workers(self):
        ctl = self.controller()
        self.assertNotIn("claude-plan", ctl.allowed_workers())
        self.assertIn("claude", ctl.allowed_workers())
        self.assertTrue(config.load(ROOT / "config.toml").models["claude-plan"].key == "claude-subscription")

    def test_closed_when_idle_and_restarted_with_the_conversation(self):
        ctl = self.controller()
        self.run_task(ctl, "hello")
        runner = ctl.session.brain
        self.assertFalse(ctl.close_idle_brain())                  # just used: stays
        runner.idle_since -= self.cfg.brain_idle_minutes * 60 + 1
        self.assertTrue(ctl.close_idle_brain())
        self.assertIsNone(ctl.session.brain)
        self.assertIsNotNone(runner.proc.poll())                  # the process is gone
        self.assertTrue(wait_for(lambda: self.of("notice")))
        self.assertIn("unused minutes", self.of("notice")[-1]["text"])
        self.run_task(ctl, "hello again")
        sent = open(os.path.join(self.tmp.name, "fake.json.msgs")).read()
        self.assertIn("Hi from the plan.", sent)                  # the earlier reply came along

    def test_runs_in_its_own_box_as_its_own_user(self):
        from unittest import mock
        from uvmc import brain
        ctl = self.controller()
        prov = make(self.cfg.profile("claude-plan"), TOKEN)
        runner = brain.BrainRunner(self.cfg, prov, "rules", ())
        with mock.patch.object(brain, "no_sudo", return_value=False), \
             mock.patch.object(brain.os.path, "isdir", return_value=True):
            cmd = runner.command()
        self.assertEqual(cmd[:3], ["sudo", "-n", "--preserve-env=" + ",".join(runner.env())])
        self.assertIn("--slice=uvmc-brain.slice", cmd)
        self.assertIn("--reuid=uvmc-brain", cmd)
        self.assertIn("HOME=/var/lib/uvmc-brain", cmd)
        self.assertNotIn(TOKEN, " ".join(cmd))
        ctl.session.close()

    def test_plan_usage_reaches_the_header(self):
        ctl = self.controller()
        self.run_task(ctl, "hello")
        u = ctl.snapshot()["plan_usage"]
        self.assertEqual(u["status"], "allowed_warning")
        self.assertEqual(u["windows"]["five_hour"], {"used": 0.81, "resets_at": 1794150000.0})
        self.assertEqual(u["windows"]["seven_day"]["resets_at"], 1794600000.0)   # milliseconds understood
        self.assertTrue(self.of("plan_usage"))
        # kept for the next start of the supervisor
        again = Controller(self.cfg, lambda p: None)
        self.assertEqual(again.plan_usage["windows"]["five_hour"]["used"], 0.81)

    def test_plan_usage_tolerates_odd_shapes(self):
        from uvmc.brain import plan_usage
        self.assertEqual(plan_usage({}), {"status": "", "limit": "", "windows": {}, "overage": False})
        u = plan_usage({"status": "rejected", "rateLimitType": "seven_day", "utilization": "x", "resetsAt": None,
                        "unifiedWindows": {"five_hour": "nonsense"}})
        self.assertEqual(u["windows"], {"seven_day": {"used": None, "resets_at": None}})

    def test_update_claude_code(self):
        from unittest import mock
        from uvmc import service
        ctl = self.controller()
        self.assertEqual(ctl.claude_version(), "9.9.9 (Claude Code)")
        self.run_task(ctl, "hello")
        self.assertIsNotNone(ctl.session.brain)
        script = os.path.join(self.tmp.name, "install-claude")
        with open(script, "w") as f:
            f.write("#!/bin/sh\necho '  Installing...'\necho '  Claude Code 9.9.10 installed (was 9.9.9).'\n")
        os.chmod(script, 0o755)
        with mock.patch.object(service, "INSTALL_CLAUDE", Path(script)):
            result = ctl.update_claude()
        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "Claude Code 9.9.10 installed (was 9.9.9).")
        self.assertIsNone(ctl.session.brain)                     # restarted with the new version next time
        # not while a task runs, and no task while updating
        ctl.claude_updating = True
        with self.assertRaises(service.Busy):
            ctl.start_task("hello")
        ctl.claude_updating = False
        with mock.patch.object(service, "INSTALL_CLAUDE", Path("/nonexistent/install-claude")):
            result = ctl.update_claude()
        self.assertFalse(result["ok"])
        self.assertIn("Run update on your PC", result["message"])


if __name__ == "__main__":
    unittest.main()
