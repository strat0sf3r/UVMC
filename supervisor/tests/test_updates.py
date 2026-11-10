"""Settings > Updates: which parts have a newer version out, checked without installing anything."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["UVMC_NO_SUDO"] = "1"
ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient  # noqa: E402

from test_service import ServiceBase  # noqa: E402
from uvmc import updates  # noqa: E402
from uvmc.config import load as load_config  # noqa: E402
from uvmc.service import create_app  # noqa: E402

LTS = """Dist: noble
Name: Noble Numbat
Version: 24.04.3 LTS
Supported: 1

Dist: resolute
Name: Resolute Raccoon
Version: 26.04 LTS
Supported: 1

Dist: zesty
Name: Old One
Version: 30.04 LTS
Supported: 0
"""


class FakeWeb:
    def __init__(self, **over):
        self.answers = {
            updates.PYPI.format("uvmc-fake-old"): json.dumps({"info": {"version": "1.2.0"}}),
            updates.PYPI.format("uvmc-fake-same"): json.dumps({"info": {"version": "2.0.0"}}),
            updates.PYPI.format("playwright"): json.dumps({"info": {"version": "1.65.0"}}),
            updates.NPM_CLAUDE: json.dumps({"version": "2.1.319"}),
            updates.GITHUB_MULTIPASS: json.dumps({"tag_name": "v1.17.0"}),
            updates.UBUNTU_LTS: LTS,
        }
        self.answers.update(over)
        self.asked = []

    def __call__(self, url):
        self.asked.append(url)
        a = self.answers.get(url)
        if a is None or isinstance(a, Exception):
            raise a or OSError("unreachable")
        return a


class UpdatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        (d / "req.txt").write_text("# pins\nuvmc-fake-old==1.0.0\nuvmc-fake-same==2.0.0  # comment\n")
        (d / "breq.txt").write_text("# browser\nplaywright==1.64.1\n")
        (d / "READY").write_text("1.64.1\n")
        (d / "state").mkdir()
        (d / "state" / "pc-multipass-version").write_text("1.16.4+win\n")
        self.notified = []
        self.web = FakeWeb()

    def make(self, **kw):
        d = Path(self.tmp.name)
        args = dict(fetch=self.web, claude_version=lambda: "2.1.300 (Claude Code)", apt=lambda: 3,
                    reboot_file=d / "no-reboot", notify=self.notified.append, requirements=d / "req.txt",
                    browser_requirements=d / "breq.txt", browser_ready=d / "READY", os_version=lambda: "24.04")
        args.update(kw)
        return updates.Updates(d / "state", **args)

    def test_versions(self):
        self.assertTrue(updates.is_newer("v1.17.0", "1.16.4+win"))
        self.assertTrue(updates.is_newer("1.10.0", "1.9.9"))              # numbers, not text
        self.assertFalse(updates.is_newer("2.1.319", "2.1.319 (Claude Code)"))
        self.assertFalse(updates.is_newer("", "1.0"))                     # unknown: never "behind"
        self.assertEqual(updates.newest_lts(LTS), ("26.04", "Resolute Raccoon"))   # unsupported ones skipped

    def test_check_finds_what_is_behind_and_says_how(self):
        u = self.make()
        r = u.check()
        by = {i["id"]: i for i in r["items"]}
        self.assertTrue(by["py:uvmc-fake-old"]["behind"])
        self.assertIn("uvmc-fake-old==1.2.0", by["py:uvmc-fake-old"]["how"])
        self.assertIn("requirements.txt", by["py:uvmc-fake-old"]["how"])
        self.assertFalse(by["py:uvmc-fake-same"]["behind"])
        self.assertEqual((by["browser"]["installed"], by["browser"]["latest"]), ("1.64.1", "1.65.0"))
        self.assertIn("browser-requirements.txt", by["browser"]["how"])
        self.assertTrue(by["claude"]["behind"])
        self.assertEqual((by["multipass"]["installed"], by["multipass"]["latest"]), ("1.16.4", "1.17.0"))
        self.assertIn("7e", by["multipass"]["how"])                        # wait for UVMC's tested move
        self.assertTrue(by["ubuntu"]["behind"])
        self.assertEqual(by["apt"]["installed"], "3 waiting")
        self.assertFalse(by["apt"]["behind"])
        self.assertEqual(r["behind"], 5)
        saved = json.loads((Path(self.tmp.name) / "state" / "updates.json").read_text())
        self.assertEqual(len(saved["items"]), len(r["items"]))
        self.assertEqual(self.make().snapshot()["behind"], 5)              # read back after a restart

    def test_a_new_version_is_told_once(self):
        u = self.make()
        u.check()
        self.assertEqual(len(self.notified), 1)
        self.assertEqual({i["id"] for i in self.notified[0]},
                         {"py:uvmc-fake-old", "browser", "claude", "multipass", "ubuntu"})
        u.check()
        self.assertEqual(len(self.notified), 1)                            # same versions: no new notice
        self.web.answers[updates.NPM_CLAUDE] = json.dumps({"version": "2.1.400"})
        u.check()
        self.assertEqual([i["id"] for i in self.notified[1]], ["claude"])  # only what's new

    def test_offline_parts_say_so_and_the_rest_still_works(self):
        self.web.answers[updates.GITHUB_MULTIPASS] = OSError("network down")
        self.web.answers[updates.PYPI.format("playwright")] = "not json"
        r = self.make().check()
        by = {i["id"]: i for i in r["items"]}
        self.assertIn("network down", by["multipass"]["error"])
        self.assertFalse(by["multipass"]["behind"])
        self.assertIn("Couldn't check", by["browser"]["error"])
        self.assertTrue(by["claude"]["behind"])

    def test_restart_needed_and_no_multipass_version_yet(self):
        d = Path(self.tmp.name)
        (d / "reboot").write_text("*** System restart required ***\n")
        (d / "state" / "pc-multipass-version").unlink()
        by = {i["id"]: i for i in self.make(reboot_file=d / "reboot").check()["items"]}
        self.assertTrue(by["apt"]["restart"])
        self.assertTrue(by["apt"]["behind"])
        self.assertIn("restart", by["apt"]["how"])
        self.assertFalse(by["multipass"]["behind"])
        self.assertIn("update.cmd", by["multipass"]["how"])

    def test_no_claude_code_no_line(self):
        ids = {i["id"] for i in self.make(claude_version=lambda: "").check()["items"]}
        self.assertNotIn("claude", ids)


class UpdatesApiTest(ServiceBase):
    def test_api(self):
        ctl = self.controller([])
        ctl.updates.fetch = FakeWeb()
        ctl.updates.apt = lambda: 0
        c = TestClient(create_app(ctl, "t"))
        h = {"Authorization": "Bearer t"}
        d = c.get("/api/updates", headers=h).json()
        self.assertEqual((d["checked"], d["items"]), (0, []))             # nothing checked yet
        self.assertTrue(d["enabled"])
        d = c.post("/api/updates/check", headers=h, json={}).json()
        self.assertGreater(d["checked"], 0)
        self.assertTrue(any(i["id"] == "multipass" for i in d["items"]))
        self.assertEqual(c.get("/api/updates").status_code, 401)           # the panel's token, like the rest


class PinsAndScriptsTest(unittest.TestCase):
    def test_requirements_are_exact_pins(self):
        for f in ("requirements.txt", "browser-requirements.txt"):
            lines = [ln.split("#")[0].strip() for ln in (ROOT / f).read_text().splitlines()]
            lines = [ln for ln in lines if ln]
            self.assertTrue(lines)
            for ln in lines:
                self.assertRegex(ln, r"^[A-Za-z0-9_.-]+==\d+(\.\d+)*$", f"{f}: {ln}")

    def test_config_and_scripts(self):
        cfg = load_config(ROOT / "config.toml")
        self.assertTrue(cfg.updates_check)
        self.assertEqual(cfg.updates_every_hours, 24)
        setup = (ROOT / "setup.sh").read_text()
        self.assertIn('"$SRC/requirements.txt" "$SRC/browser-requirements.txt" /opt/uvmc/', setup)
        self.assertIn("pc-multipass-version", setup)
        self.assertIn("UVMC_PC_MULTIPASS=%MPV%", (REPO / "windows" / "update.cmd").read_text())
        self.assertIn("UVMC_PC_MULTIPASS=$mpv", (REPO / "unix" / "update.sh").read_text())


if __name__ == "__main__":
    unittest.main()
