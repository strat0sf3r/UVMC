"""Agents can't watch each other (design.md 7p): every command of a conversation, job or tool
starts in a private view of its own (executor.private_view): a /proc showing only its user's
processes and its own /tmp, /var/tmp and /dev/shm.

Offline these check the commands; the view itself is checked in the VM by
tests/check_private_view.py (as the supervisor's user, which has sudo).
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ["UVMC_NO_SUDO"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from uvmc.config import load as load_config  # noqa: E402
from uvmc.executor import PRIVATE_DIR, VIEW_SCRIPT, SudoExecutor, SystemdExecutor, private_view  # noqa: E402


class PrivateViewTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / "config.toml")

    def check(self, cmd, user, group):
        view = private_view(user, group)
        i = cmd.index("unshare")
        self.assertEqual(cmd[i:i + len(view)], view)
        setpriv = cmd.index("setpriv")
        self.assertEqual(setpriv, i + len(view))                   # straight before it drops to the user
        self.assertIn(f"--reuid={user}", cmd[setpriv:])
        return cmd[setpriv:]

    def test_conversation_commands(self):
        cmd = SystemdExecutor(self.cfg, user="agent1", workdir="/w", proxy_port=3301).command("echo hi", ("a",), 30)
        self.assertLess(cmd.index("systemd-run"), cmd.index("unshare"))   # inside its box
        rest = self.check(cmd, "agent1", self.cfg.user)
        self.assertEqual(rest[-2:], ["uvmc", "a"])                 # its arguments, unchanged
        self.assertIn('cd "/w" && echo hi', rest)                 # the script itself, unchanged

    def test_the_fallback_without_systemd(self):
        cmd = SudoExecutor(self.cfg, user="agent2", workdir="/w").command("true", (), 30)
        self.assertEqual(cmd[:3], ["sudo", "-n", "unshare"])
        rest = self.check(cmd, "agent2", self.cfg.user)
        self.assertIn("HOME=" + os.path.join(self.cfg.homes_root, "agent2"), rest)

    def test_tools(self):
        from test_supervisor import make_cfg
        from uvmc.made_tools import ToolBox
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(os.path.join(tmp, "w"), os.path.join(tmp, "logs"))
            box = ToolBox(cfg)
            e = {"version": 1, "manifest": {"timeout_seconds": 60, "command": "python3 main.py"}}
            with mock.patch("uvmc.made_tools.no_sudo", return_value=False):
                for systemd in (True, False):
                    with mock.patch("uvmc.made_tools.os.path.isdir", return_value=systemd):
                        cmd = box.command("watcher", e, {"HOME": "/h", "UVMC_TOOL": "watcher"})
                        self.check(cmd, box.user("watcher"), cfg.made_tools_group)

    def test_the_script(self):
        # fails closed: every step checked, and the command only runs at the very end
        self.assertIn("hidepid=invisible", VIEW_SCRIPT)
        self.assertTrue(VIEW_SCRIPT.strip().endswith('exec "$@"'))
        for step in ("mount -t proc", "install -d -m 711", "install -d -m 700", "mount --bind"):
            line = next(ln for ln in VIEW_SCRIPT.splitlines() if step in ln)
            self.assertIn("|| fail", line, step)
        for d in ("/tmp", "/var/tmp", "/dev/shm"):
            self.assertIn(d, VIEW_SCRIPT)
        self.assertIn(PRIVATE_DIR, VIEW_SCRIPT)
        self.assertIn("--propagation", private_view("a", "b"))       # shared folders mounted later still show
        self.assertEqual(private_view("a", "b")[private_view("a", "b").index("--propagation") + 1], "slave")


if __name__ == "__main__":
    unittest.main()
