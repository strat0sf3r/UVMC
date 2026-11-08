"""Offline tests for panel settings: model profiles and API keys.

Run from the supervisor folder:  UVMC_NO_SUDO=1 python3 -m unittest discover tests
"""

import os
import sys
import unittest
from pathlib import Path

os.environ["UVMC_NO_SUDO"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient  # noqa: E402

from test_service import ServiceBase  # noqa: E402
from test_supervisor import make_cfg  # noqa: E402
from uvmc import config  # noqa: E402
from uvmc.service import Controller, create_app  # noqa: E402


class SettingsTest(ServiceBase):
    def setUp(self):
        super().setUp()
        self.old = (config.KEY_DIR, config.KEY_FILE)
        config.KEY_DIR, config.KEY_FILE = Path(self.tmp.name) / "keys", Path(self.tmp.name) / "api_key"
        self.saved_env = {k: os.environ.pop(k) for k in list(os.environ) if k == "ANTHROPIC_API_KEY" or k.startswith("UVMC_KEY_")}
        self.ctl = Controller(self.cfg, lambda prof: None)
        self.c = TestClient(create_app(self.ctl, "t"))
        self.c.get("/?token=t")

    def tearDown(self):
        config.KEY_DIR, config.KEY_FILE = self.old
        os.environ.update(self.saved_env)
        super().tearDown()

    def fresh_controller(self):
        cfg = make_cfg(self.cfg.workdir, str(self.cfg.log_dir))
        cfg.state_dir = self.cfg.state_dir
        return Controller(cfg, lambda prof: None)

    def test_add_edit_remove_profiles(self):
        r = self.c.post("/api/settings/profile", json={
            "name": "local", "label": "My Ollama", "provider": "openai",
            "base_url": "http://192.168.1.20:11434/v1", "model": "qwen-coder", "key": ""})
        self.assertEqual(r.status_code, 200, r.text)
        names = {p["name"]: p for p in r.json()["profiles"]}
        self.assertEqual(names["local"]["source"], "panel")
        self.assertTrue(names["local"]["has_key"])  # no key needed
        self.assertIn("local", [m["name"] for m in self.c.get("/api/state").json()["models"]])

        # survives a restart of the supervisor
        again = self.fresh_controller()
        self.assertEqual(again.cfg.profile("local").base_url, "http://192.168.1.20:11434/v1")

        # editing a config.toml profile, then resetting it
        self.c.post("/api/settings/profile", json={"name": "claude", "label": "Claude (panel)", "provider": "anthropic",
                                                   "model": "claude-opus-5-5", "key": "anthropic"})
        self.assertEqual(self.ctl.cfg.profile("claude").model, "claude-opus-5-5")
        r = self.c.post("/api/settings/profile/remove", json={"name": "claude"})
        self.assertEqual(r.json()["result"], "reset to config.toml")
        self.assertEqual(self.ctl.cfg.profile("claude").model, "claude-sonnet-5-5")
        r = self.c.post("/api/settings/profile/remove", json={"name": "deepseek"})
        self.assertEqual(r.status_code, 400)  # from config.toml: change it there

        # default model
        self.c.post("/api/settings/default", json={"name": "local"})
        self.assertEqual(self.fresh_controller().cfg.model, "local")
        self.assertIn('model = "local"', self.c.get("/api/settings/toml").json()["toml"])
        self.assertIn("[models.local]", self.c.get("/api/settings/toml").json()["toml"])

        self.ctl.model = "local"
        self.c.post("/api/settings/profile/remove", json={"name": "local"})
        self.assertEqual(self.ctl.model, "claude")  # the conversation falls back to the default

    def test_validation(self):
        bad = [
            {"name": "Bad Name", "provider": "openai", "model": "m", "base_url": "http://x"},
            {"name": "x", "provider": "azure", "model": "m"},
            {"name": "x", "provider": "openai", "model": ""},
            {"name": "x", "provider": "openai", "model": "m"},                       # no address
            {"name": "x", "provider": "openai", "model": "m", "base_url": "ftp://x"},
        ]
        for b in bad:
            self.assertEqual(self.c.post("/api/settings/profile", json=b).status_code, 400, b)

    def test_keys_are_stored_but_never_shown(self):
        r = self.c.post("/api/settings/key", json={"name": "deepseek", "key": " sk-deepseek-1234567890abcd "})
        self.assertEqual(r.status_code, 200, r.text)
        keys = {k["name"]: k for k in r.json()["keys"]}
        self.assertTrue(keys["deepseek"]["set"])
        self.assertEqual(keys["deepseek"]["hint"], "ends in abcd")
        self.assertNotIn("sk-deepseek", r.text)
        self.assertNotIn("sk-deepseek", self.c.get("/api/settings").text)
        self.assertEqual(config.api_key("deepseek"), "sk-deepseek-1234567890abcd")
        self.assertEqual(oct((config.KEY_DIR / "deepseek").stat().st_mode & 0o777), "0o600")
        self.assertTrue([m for m in self.c.get("/api/state").json()["models"] if m["name"] == "deepseek"][0]["has_key"])

        self.c.post("/api/settings/key", json={"name": "anthropic", "key": "sk-ant-abcdefgh12345678"})
        self.assertEqual(config.api_key("anthropic"), "sk-ant-abcdefgh12345678")
        self.assertTrue(self.c.get("/api/state").json()["has_key"])

        self.c.post("/api/settings/key", json={"name": "deepseek", "key": ""})
        self.assertIsNone(config.api_key("deepseek"))
        self.assertEqual(self.c.post("/api/settings/key", json={"name": "x", "key": "short"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
