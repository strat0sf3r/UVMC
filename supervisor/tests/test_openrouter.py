"""Offline tests for OpenRouter (openrouter.py and the openai adapter's OpenRouter parts).

A stand-in server on 127.0.0.1 plays OpenRouter: the tests add that host to openrouter.HOSTS.
Run from the supervisor folder:  UVMC_NO_SUDO=1 python3 -m unittest discover tests
"""

import http.server
import json
import os
import sys
import threading
import tomllib
import unittest
from unittest import mock
from pathlib import Path

os.environ["UVMC_NO_SUDO"] = "1"
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient  # noqa: E402

from test_service import ServiceBase  # noqa: E402
from uvmc import config, openrouter  # noqa: E402
from uvmc.agent import Session  # noqa: E402
from uvmc.log import RunLog, read_log  # noqa: E402
from uvmc.providers import OpenAIProvider, Profile, ProviderError, ToolCall, make  # noqa: E402
from uvmc.service import Controller, create_app  # noqa: E402

DETAILS = [{"type": "reasoning.encrypted", "id": "r1", "format": "anthropic-claude-v1", "index": 0, "data": "opaque"}]


def chat(content=None, tool_calls=None, usage=None, **extra):
    msg = {"role": "assistant", "content": content, **extra}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5}}


def call(cid, name, args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class FakeOpenRouter:
    """Plays back (status, body, headers) answers to POSTs, and serves GET /models and /key."""

    def __init__(self, answers=(), models=None, key=None):
        self.answers, self.requests, self.gets = list(answers), [], []
        self.models, self.key = models or {"data": []}, key or {"data": {}}
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def answer(self, status, body, headers=None):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append({"path": self.path, "body": body, "headers": dict(self.headers)})
                a = fake.answers.pop(0)
                self.answer(*a) if isinstance(a, tuple) else self.answer(200, a)

            def do_GET(self):
                fake.gets.append({"path": self.path, "auth": self.headers.get("Authorization")})
                if self.path.startswith("/v1/models"):
                    self.answer(200, fake.models)
                elif self.path == "/v1/key":
                    self.answer(200, fake.key)
                else:
                    self.answer(404, {"error": {"code": 404, "message": "not here"}})

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"

    def stop(self):
        self.srv.shutdown()


class Base(ServiceBase):
    def setUp(self):
        super().setUp()
        self.hosts = list(openrouter.HOSTS)
        openrouter.HOSTS.append("127.0.0.1")
        openrouter._models_cache.clear()

    def tearDown(self):
        openrouter.HOSTS[:] = self.hosts
        openrouter._models_cache.clear()
        super().tearDown()

    def serve(self, *a, **kw):
        fake = FakeOpenRouter(*a, **kw)
        self.addCleanup(fake.stop)
        return fake


class HelpersTest(unittest.TestCase):
    def test_recognised_by_address(self):
        self.assertTrue(openrouter.is_openrouter("https://openrouter.ai/api/v1"))
        self.assertTrue(openrouter.is_openrouter("https://eu.openrouter.ai/api/v1"))
        self.assertFalse(openrouter.is_openrouter("https://api.deepseek.com"))
        self.assertFalse(openrouter.is_openrouter("https://openrouter.ai.example.com/v1"))
        self.assertFalse(openrouter.is_openrouter("https://notopenrouter.ai/v1"))

    def test_free_models(self):
        self.assertTrue(openrouter.is_free("qwen/qwen3-coder:free"))
        self.assertTrue(openrouter.is_free("x/y:free:nitro"))
        self.assertFalse(openrouter.is_free("deepseek/deepseek-v4-pro"))
        self.assertFalse(openrouter.is_free("x/freebie"))

    def test_routing(self):
        self.assertEqual(openrouter.routing("open", False), {})
        self.assertEqual(openrouter.routing("open", True), {"require_parameters": True})
        self.assertEqual(openrouter.routing("paid", True),
                         {"require_parameters": True, "zdr": True, "data_collection": "deny"})


class AdapterTest(Base):
    def test_request_shape_by_privacy(self):
        fake = self.serve([chat("a"), chat("b"), chat("c")])
        tools = [{"name": "read_file", "description": "d", "input_schema": {"type": "object"}}]
        hi = [{"role": "user", "text": "hi"}]
        make(Profile("or", "openai", "m/x", key="openrouter", base_url=fake.url, privacy="paid"), "k").complete("s", hi, tools, 100)
        make(Profile("or2", "openai", "m/x", key="openrouter", base_url=fake.url, privacy="open"), "k").complete("s", hi, [], 100)
        openrouter.HOSTS.remove("127.0.0.1")   # the same server, but not OpenRouter: nothing added
        make(Profile("other", "openai", "m/x", key="k", base_url=fake.url, privacy="paid"), "k").complete("s", hi, tools, 100)
        paid, opened, other = (r["body"] for r in fake.requests)
        self.assertEqual(paid["provider"], {"require_parameters": True, "zdr": True, "data_collection": "deny"})
        self.assertNotIn("provider", opened)
        self.assertNotIn("provider", other)
        for r in fake.requests:   # UVMC doesn't announce itself
            names = {k.lower() for k in r["headers"]}
            self.assertFalse(names & {"http-referer", "x-title", "x-openrouter-title"})

    def test_reasoning_back_to_the_same_profile_only(self):
        fake = self.serve([
            chat("Writing.", [call("c1", "write_file", {"path": "a.txt", "content": "x"})],
                 reasoning="I'll write it.", reasoning_details=DETAILS,
                 usage={"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0012}),
            chat("Done.", usage={"prompt_tokens": 20, "completion_tokens": 3, "cost": 0.0003}),
        ])
        prof = Profile("orc", "openai", "anthropic/claude-sonnet", key="openrouter", base_url=fake.url, privacy="paid")
        s = Session(self.cfg, make(prof, "k"), lambda *a: True, RunLog(self.cfg.log_dir), lambda *_: None)
        self.assertEqual(s.run_task("make a.txt"), "done")
        asst = fake.requests[1]["body"]["messages"][2]
        self.assertEqual(asst["role"], "assistant")
        self.assertEqual(asst["reasoning_details"], DETAILS)

        replies = [e for e in read_log(next(self.cfg.log_dir.glob("*.jsonl"))) if e["kind"] == "reply"]
        self.assertEqual(replies[0]["reasoning"], "I'll write it.")       # "reasoning" read like reasoning_content
        self.assertEqual(replies[0]["usage"], {"in": 10, "out": 5, "cost": 0.0012})
        self.assertNotIn("opaque", json.dumps(replies))                    # the blocks stay in memory only

        # Another profile (another OpenRouter model, or DeepSeek) gets the generic form.
        other = OpenAIProvider(Profile("or-other", "openai", "x/y", key="openrouter", base_url=fake.url), api_key="k")
        msgs = other.to_native("", s.messages)
        self.assertNotIn("reasoning_details", [m for m in msgs if m["role"] == "assistant"][0])
        same = OpenAIProvider(prof, api_key="k")
        self.assertIn("reasoning_details", [m for m in same.to_native("", s.messages) if m["role"] == "assistant"][0])

    def test_no_cost_no_field(self):
        fake = self.serve([chat("ok")])
        r = make(Profile("or", "openai", "m/x", key="k", base_url=fake.url), "k").complete("s", [{"role": "user", "text": "hi"}], [], 10)
        self.assertIsNone(r.cost)
        self.assertIsNone(r.native)

    def test_errors_say_what_to_do(self):
        err = lambda code, msg, **meta: {"error": {"code": code, "message": msg, "metadata": meta}}
        fake = self.serve([
            (402, err(402, "Insufficient credits")),
            (429, err(429, "Rate limit exceeded"), {"Retry-After": "17"}),
            (503, err(503, "No endpoints found matching your data policy", provider_name="Acme")),
            (429, err(429, "Provider returned error", provider_name="Google AI Studio")),
        ])
        hi = [{"role": "user", "text": "hi"}]
        cases = [(Profile("a", "openai", "x/y", key="k", base_url=fake.url), "openrouter.ai/settings/credits"),
                 (Profile("b", "openai", "x/y:free", key="k", base_url=fake.url), "try again in 17 s; free models allow 20"),
                 (Profile("c", "openai", "x/y", key="k", base_url=fake.url, privacy="paid"), "keeps nothing")]
        for prof, words in cases:
            with self.assertRaises(ProviderError) as e:
                make(prof, "k").complete("s", hi, [], 10)
            self.assertIn(words, str(e.exception))
        self.assertIn("[provider: Acme]", str(e.exception))
        with self.assertRaises(ProviderError) as e:   # the provider's own limit, seen for real 2026-11-12
            make(Profile("d", "openai", "x/y:free", key="k", base_url=fake.url), "k").complete("s", hi, [], 10)
        self.assertIn("that provider is busy", str(e.exception))
        self.assertNotIn("OpenRouter's limit", str(e.exception))


class ProfilesTest(Base):
    def setUp(self):
        super().setUp()
        self.old = (config.KEY_DIR, config.KEY_FILE)
        config.KEY_DIR, config.KEY_FILE = Path(self.tmp.name) / "keys", Path(self.tmp.name) / "api_key"
        self.saved_env = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("UVMC_KEY_")}
        self.ctl = Controller(self.cfg, lambda prof: None)
        self.c = TestClient(create_app(self.ctl, "t"))
        self.c.get("/?token=t")

    def tearDown(self):
        config.KEY_DIR, config.KEY_FILE = self.old
        os.environ.pop("UVMC_KEY_OPENROUTER", None)
        os.environ.update(self.saved_env)
        super().tearDown()

    def test_free_model_only_open(self):
        base = {"name": "orfree", "provider": "openai", "base_url": "https://openrouter.ai/api/v1",
                "model": "qwen/qwen3-coder:free", "key": "openrouter"}
        r = self.c.post("/api/settings/profile", json={**base, "privacy": "paid"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("free models", r.json()["error"])
        self.assertEqual(self.c.post("/api/settings/profile", json={**base, "privacy": "open"}).status_code, 200)
        with self.assertRaises(ValueError):
            config._load_models({"x": {"provider": "openai", "base_url": "https://openrouter.ai/api/v1",
                                       "model": "a/b:free", "privacy": "paid"}}, {})

    def test_shipped_profile_waits_for_a_model(self):
        raw = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(raw["models"]["openrouter"]["base_url"], openrouter.BASE_URL)
        self.assertEqual(raw["models"]["openrouter"]["privacy"], "open")
        self.assertNotIn("openrouter", config._load_models(raw["models"], {}))   # no model name yet
        raw["models"]["openrouter"]["model"] = "deepseek/deepseek-v4-pro"
        self.assertEqual(config._load_models(raw["models"], {})["openrouter"].key, "openrouter")

    def test_key_can_come_before_the_model(self):
        """The shipped profile has no model yet, and the picker needs the key: the key is listed."""
        keys = {k["name"]: k for k in self.c.get("/api/settings").json()["keys"]}
        self.assertIn("openrouter", keys)
        self.assertEqual(keys["openrouter"]["used_by"], [])
        self.assertIn("openai", keys)                    # the other unfinished profiles too (gpt)
        self.assertNotIn("openrouter", [m["name"] for m in self.c.get("/api/state").json()["models"]])
        waiting = {w["name"]: w for w in self.c.get("/api/settings").json()["waiting"]}
        self.assertEqual(waiting["openrouter"]["base_url"], openrouter.BASE_URL)   # shown with "Choose a model"
        self.assertIn("gpt", waiting)
        r = self.c.post("/api/settings/profile", json={**waiting["openrouter"], "model": "x/y:free", "privacy": "open"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("openrouter", [w["name"] for w in r.json()["waiting"]])     # now a profile like the others
        self.assertIn("openrouter", [p["name"] for p in r.json()["profiles"]])
        r = self.c.post("/api/settings/key", json={"name": "openrouter", "key": "sk-or-v1-test-key"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue({k["name"]: k for k in r.json()["keys"]}["openrouter"]["set"])

    def test_models_and_credit(self):
        fake = self.serve(models={"data": [
            {"id": "b/coder", "name": "B Coder", "pricing": {"prompt": "0.000001", "completion": "0.000004"},
             "context_length": 128000, "supported_parameters": ["tools", "temperature"]},
            {"id": "a/tiny:free", "name": "A Tiny (free)", "pricing": {"prompt": "0", "completion": "0"},
             "context_length": 32000, "supported_parameters": ["tools"]},
            {"id": "c/chat", "name": "C Chat", "pricing": {}, "supported_parameters": ["temperature"]},
        ]}, key={"data": {"label": "sk-or-v1-abc...xyz", "limit": None, "limit_remaining": None, "usage": 0,
                          "usage_daily": 0, "usage_monthly": 0, "is_free_tier": True,
                          "free_model_daily_requests": {"used": 3, "limit": 50, "remaining": 47}}})
        q = {"base_url": fake.url, "key": "openrouter"}
        r = self.c.get("/api/openrouter/models", params=q)
        self.assertEqual(r.status_code, 502, r.text)   # no key yet
        self.assertIn("Settings › API keys", r.json()["error"])

        os.environ["UVMC_KEY_OPENROUTER"] = "sk-or-test"
        rows = self.c.get("/api/openrouter/models", params=q).json()["models"]
        self.assertEqual([m["id"] for m in rows], ["a/tiny:free", "b/coder"])   # tools only, by name
        self.assertEqual(rows[1]["in"], 1.0)
        self.assertEqual(rows[1]["out"], 4.0)
        self.assertTrue(rows[0]["free"])
        self.assertEqual(fake.gets[0]["path"], "/v1/models?supported_parameters=tools")
        self.assertEqual(fake.gets[0]["auth"], "Bearer sk-or-test")
        self.c.get("/api/openrouter/models", params=q)
        self.assertEqual(len(fake.gets), 1)                                       # kept an hour

        credit = self.c.get("/api/openrouter/credit", params=q)
        self.assertEqual(credit.status_code, 200, credit.text)
        self.assertEqual(credit.json()["free_requests"], {"used": 3, "limit": 50, "remaining": 47})
        self.assertTrue(credit.json()["free_tier"])
        self.assertNotIn("sk-or", credit.text)                                    # neither the key nor its label

        # Only OpenRouter's own address is asked, with a key.
        self.assertEqual(self.c.get("/api/openrouter/models", params={"base_url": "https://example.com/v1"}).status_code, 400)
        self.assertEqual(self.c.get("/api/openrouter/credit", params={**q, "key": "../etc"}).status_code, 400)

        # Not while the VM is locked.
        with mock.patch.object(type(self.ctl.vm_lock), "locked", new_callable=mock.PropertyMock, return_value=True):
            self.assertEqual(self.c.get("/api/openrouter/credit", params=q).status_code, 409)


if __name__ == "__main__":
    unittest.main()
