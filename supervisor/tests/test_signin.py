"""Signing in with your Claude plan from the panel, with a stand-in for `claude setup-token`."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["UVMC_NO_SUDO"] = "1"
HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from uvmc.signin import SignIn, SignInError, screen_text  # noqa: E402

TOKEN = "sk-ant-oat01-" + "Ab3_-" * 20 + "QQ"


class SignInTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.claude = os.path.join(self.tmp.name, "claude")
        with open(self.claude, "w") as f:
            f.write(f'#!/bin/sh\nexec "{sys.executable}" "{HERE / "fake_claude.py"}" "$@"\n')
        os.chmod(self.claude, 0o755)
        self.saved, self.checked = [], []

    def tearDown(self):
        self.tmp.cleanup()

    def signin(self, works=True):
        def verify(token):
            self.checked.append(token)
            return (True, "") if works else (False, "401 OAuth access token is invalid")
        return SignIn(self.claude, os.path.join(self.tmp.name, "work"), save=self.saved.append, verify=verify)

    def test_link_code_token(self):
        s = self.signin()
        url = s.start(wait=15)
        self.assertTrue(url.startswith("https://claude.com/cai/oauth/authorize?"))
        self.assertTrue(url.endswith("state=abc"))                   # whole, not cut at an escape code
        message = s.submit(" good#abc \n", wait=15)
        self.assertEqual(self.saved, [TOKEN])                       # not glued to the "Store" drawn after it
        self.assertEqual(self.checked, [TOKEN])
        self.assertIn(f"ends in {TOKEN[-4:]}", message)
        self.assertFalse(s.running())
        self.assertEqual(os.listdir(os.path.join(self.tmp.name, "work")), ["last.txt"])   # temporary home removed
        with open(os.path.join(self.tmp.name, "work", "last.txt"), encoding="utf-8") as f:
            self.assertNotIn(TOKEN[14:], f.read())   # the note about the sign-in never holds the token

    def test_wrong_code(self):
        s = self.signin()
        s.start(wait=15)
        with self.assertRaises(SignInError) as e:
            s.submit("wrong", wait=15)
        self.assertIn("Invalid code", str(e.exception))
        self.assertEqual(self.saved, [])

    def test_token_that_does_not_work_is_not_saved(self):
        s = self.signin(works=False)
        s.start(wait=15)
        with self.assertRaises(SignInError) as e:
            s.submit("good#abc", wait=15)
        self.assertIn("didn't work", str(e.exception))
        self.assertEqual(self.saved, [])

    def test_submit_without_start_and_missing_claude(self):
        s = self.signin()
        with self.assertRaises(SignInError):
            s.submit("good#abc")
        s.claude_bin = "/nonexistent/claude"
        with self.assertRaises(SignInError) as e:
            s.start()
        self.assertIn("not installed", str(e.exception))

    def test_screen_text(self):
        raw = b"\x1b[2GPaste\x1b[8Gcode\x1b[38;5;1m here\x1b[39m\r\r\n"
        self.assertEqual(screen_text(raw).strip(), "Paste code here")


if __name__ == "__main__":
    unittest.main()
