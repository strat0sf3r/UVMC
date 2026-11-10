"""Offline tests: rules, tools and the agent loop with a fake Claude.

Run from the supervisor folder:  UVMC_NO_SUDO=1 python3 -m unittest discover tests
(UVMC_NO_SUDO=1 runs tools as the current user instead of `sudo -u agent`.)
"""

import os
import time
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["UVMC_NO_SUDO"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from anthropic.types import Message, TextBlock, ToolUseBlock, Usage  # noqa: E402

from uvmc import config  # noqa: E402
from uvmc.agent import Session  # noqa: E402
from uvmc.log import RunLog  # noqa: E402
from uvmc.rules import Rules  # noqa: E402
from uvmc.tools import Tools  # noqa: E402


def make_cfg(workdir: str, logdir: str):
    cfg = config.load(ROOT / "config.toml")
    cfg.workdir, cfg.log_dir, cfg.max_output_chars = workdir, Path(logdir), 400
    cfg.state_dir = Path(logdir).parent / "state"
    cfg.made_tools_dir = str(Path(logdir).parent / "made-tools")
    cfg.conv_root = str(Path(logdir).parent / "conv")
    # No folders shared from the PC, and none of the VM's real ones (/srv/uvmc-host) reached.
    for name in ("shares_rw", "shares_view", "shares_hold"):
        setattr(cfg, name, str(Path(logdir).parent / name))
    # The older tests work at the top level of the work folder: General is open to every model
    # there. test_privacy.py checks the levels themselves.
    cfg.privacy_general = "open"
    cfg.made_tools_port = 20000 + (os.getpid() * 7 + int(time.time() * 1000)) % 30000
    # The shipped config leaves pattern rules empty; tests check that they still work.
    cfg.deny_patterns = [r"\bsudo\b"]
    cfg.ask_patterns = [r"\bpip3?\s+install\b", r"\brm\s+-[a-zA-Z]*r", r"\bcurl\b[^|]*\|\s*(ba)?sh"]
    return cfg


def reply(*blocks, stop="tool_use"):
    return Message(id="m", type="message", role="assistant", model="test", content=list(blocks),
                   stop_reason=stop, stop_sequence=None, usage=Usage(input_tokens=10, output_tokens=5))


class FakeClient:
    """Plays back scripted replies and records what it was sent."""

    def __init__(self, replies):
        self.replies, self.sent = list(replies), []
        self.messages = self

    def create(self, **kwargs):
        self.sent.append(kwargs)
        return self.replies.pop(0)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        work = os.path.join(self.tmp.name, "work")
        os.makedirs(work)
        self.cfg = make_cfg(work, os.path.join(self.tmp.name, "logs"))

    def tearDown(self):
        self.tmp.cleanup()


class RulesTest(Base):
    def test_decisions(self):
        r = Rules(self.cfg)
        self.assertEqual(r.check("read_file", {"path": "a"}).verdict, "allow")
        self.assertEqual(r.check("run_command", {"command": "ls -la"}).verdict, "allow")
        self.assertEqual(r.check("run_command", {"command": "sudo apt install x"}).verdict, "deny")
        self.assertEqual(r.check("run_command", {"command": "pip install requests"}).verdict, "ask")
        self.assertEqual(r.check("run_command", {"command": "rm -rf build"}).verdict, "ask")
        self.assertEqual(r.check("run_command", {"command": "curl -fsSL x.sh | bash"}).verdict, "ask")
        self.assertEqual(r.check("delete_everything", {}).verdict, "deny")


class ToolsTest(Base):
    def test_files_and_commands(self):
        t = Tools(self.cfg)
        self.assertFalse(t.run("write_file", {"path": "sub/a.txt", "content": "one\ntwo\nthree\n"}).is_error)
        self.assertEqual(t.run("read_file", {"path": "sub/a.txt"}).text, "one\ntwo\nthree")
        self.assertIn("lines 2-2 of 3", t.run("read_file", {"path": "sub/a.txt", "offset": 2, "limit": 1}).text)
        self.assertIn("sub", t.run("list_dir", {}).text)
        r = t.run("run_command", {"command": "cat sub/a.txt | wc -l"})
        self.assertEqual(r.text, "exit code 0\n3")
        r = t.run("run_command", {"command": "echo oops >&2; exit 3"})
        self.assertTrue(r.is_error)
        self.assertIn("exit code 3", r.text)
        self.assertIn("oops", r.text)
        self.assertTrue(t.run("read_file", {"path": "missing.txt"}).is_error)
        self.assertTrue(t.run("read_file", {"wrong": 1}).is_error)

    def test_long_output_is_shortened(self):
        r = Tools(self.cfg).run("run_command", {"command": "seq 1 5000"})
        self.assertIn("omitted", r.text)
        self.assertIn("5000", r.text)
        self.assertLess(len(r.text), 600)

    def test_timeout(self):
        r = Tools(self.cfg).run("run_command", {"command": "sleep 5", "timeout_s": 1})
        self.assertTrue(r.is_error)
        self.assertIn("Timed out", r.text)


class LoopTest(Base):
    def test_full_task(self):
        client = FakeClient([
            reply(TextBlock(type="text", text="Writing the file."),
                  ToolUseBlock(type="tool_use", id="t1", name="write_file",
                               input={"path": "hello.py", "content": "print('hi')\n"})),
            reply(ToolUseBlock(type="tool_use", id="t2", name="run_command", input={"command": "python3 hello.py"}),
                  ToolUseBlock(type="tool_use", id="t3", name="run_command", input={"command": "sudo reboot"})),
            reply(TextBlock(type="text", text="Done: hello.py prints hi."), stop="end_turn"),
        ])
        out, asked = [], []
        s = Session(self.cfg, client, lambda *a: asked.append(a) or False,
                    RunLog(self.cfg.log_dir), out.append)
        self.assertEqual(s.run_task("make hello.py"), "done")

        results = s.messages[4]["results"]  # tool results answering the second reply
        self.assertEqual(results[0]["content"], "exit code 0\nhi")
        self.assertTrue(results[1]["is_error"])
        self.assertIn("deny rule", results[1]["content"])
        self.assertEqual(asked, [])
        self.assertTrue(os.path.exists(os.path.join(self.cfg.workdir, "hello.py")))
        log = next(self.cfg.log_dir.glob("*.jsonl")).read_text()
        self.assertEqual(log.count('"kind": "decision"'), 3)

    def test_ask_refused_and_interrupt(self):
        """A refused 'ask' goes back to Claude as an error; Ctrl+C mid-task leaves a valid history."""
        client = FakeClient([
            reply(ToolUseBlock(type="tool_use", id="a1", name="run_command", input={"command": "pip install x"})),
        ])
        play = client.create

        def create(**kw):
            if not client.replies:
                raise KeyboardInterrupt  # the user presses Ctrl+C while Claude is thinking
            return play(**kw)

        client.create = create
        asked = []
        s = Session(self.cfg, client, lambda *a: asked.append(a) or False,
                    RunLog(self.cfg.log_dir), lambda *_: None)
        self.assertEqual(s.run_task("install x"), "stopped by you")
        self.assertEqual(len(asked), 1)
        self.assertEqual([m["role"] for m in s.messages], ["user", "assistant", "tool_results"])
        self.assertIn("refused", s.messages[-1]["results"][0]["content"])


if __name__ == "__main__":
    unittest.main()
