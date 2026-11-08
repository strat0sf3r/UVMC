"""Offline tests for the provider layer: adapters, profiles, keys, switching models.

Run from the supervisor folder:  UVMC_NO_SUDO=1 python3 -m unittest discover tests
"""

import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

os.environ["UVMC_NO_SUDO"] = "1"
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from anthropic.types import TextBlock, ToolUseBlock  # noqa: E402

from test_service import ServiceBase, wait_for  # noqa: E402
from test_supervisor import FakeClient, make_cfg, reply  # noqa: E402
from uvmc import config  # noqa: E402
from uvmc.agent import Session  # noqa: E402
from uvmc.log import RunLog, read_log  # noqa: E402
from uvmc.providers import (AnthropicProvider, OpenAIProvider, Profile, ProviderError,  # noqa: E402
                            ToolCall, make)
from uvmc.service import Controller  # noqa: E402


class FakeOpenAI:
    """A tiny OpenAI-compatible server that plays back scripted answers and records requests."""

    def __init__(self, answers, status=200):
        self.answers, self.requests, self.status = list(answers), [], status
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                data = json.dumps(fake.answers.pop(0) if fake.status == 200 else
                                  {"error": {"message": "Invalid API key"}}).encode()
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"

    def stop(self):
        self.srv.shutdown()


def chat(content=None, tool_calls=None, finish="stop", p=11, c=7, **extra):
    msg = {"role": "assistant", "content": content, **extra}
    if tool_calls:
        msg["tool_calls"] = tool_calls
        finish = "tool_calls"
    return {"choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": p, "completion_tokens": c}}


def call(cid, name, args):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}}


class OpenAIAdapterTest(ServiceBase):
    def test_full_loop_against_openai_compatible_server(self):
        fake = FakeOpenAI([
            chat("Writing it.", [call("c1", "write_file", {"path": "hi.txt", "content": "hello"})],
                 reasoning_content="I should write the file."),
            chat(None, [call("c2", "run_command", {"command": "cat hi.txt"}), call("c3", "read_file", "{not json")]),
            chat("Done: hi.txt says hello."),
        ])
        try:
            prof = Profile("deepseek", "openai", "deepseek-v4-pro", key="deepseek", base_url=fake.url)
            s = Session(self.cfg, make(prof, "sk-test"), lambda *a: True, RunLog(self.cfg.log_dir), lambda *_: None)
            self.assertEqual(s.run_task("make hi.txt"), "done")
        finally:
            fake.stop()

        r1, r2, r3 = (r["body"] for r in fake.requests)
        self.assertEqual(fake.requests[0]["path"], "/v1/chat/completions")
        self.assertEqual(fake.requests[0]["auth"], "Bearer sk-test")
        self.assertEqual(r1["model"], "deepseek-v4-pro")
        self.assertEqual(r1["messages"][0], {"role": "system", "content": self.cfg.house_rules})
        self.assertEqual(r1["messages"][1], {"role": "user", "content": "make hi.txt"})
        self.assertEqual(r1["tools"][0]["type"], "function")
        self.assertIn("parameters", r1["tools"][0]["function"])
        self.assertEqual(r1["max_tokens"], self.cfg.max_tokens)
        asst = r2["messages"][2]
        self.assertEqual(asst["tool_calls"][0]["function"]["name"], "write_file")
        self.assertEqual(json.loads(asst["tool_calls"][0]["function"]["arguments"])["path"], "hi.txt")
        self.assertEqual(r2["messages"][3]["role"], "tool")
        self.assertEqual(r2["messages"][3]["tool_call_id"], "c1")
        tool_msgs = [m for m in r3["messages"] if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tool_msgs], ["c1", "c2", "c3"])
        self.assertIn("exit code 0\nhello", tool_msgs[1]["content"])
        self.assertTrue(tool_msgs[2]["content"].startswith("Error: Bad arguments"))  # broken JSON reported back
        self.assertEqual(Path(self.cfg.workdir, "hi.txt").read_text(), "hello")
        replies = [e for e in read_log(next(self.cfg.log_dir.glob("*.jsonl"))) if e["kind"] == "reply"]
        self.assertEqual(replies[0]["reasoning"], "I should write the file.")
        self.assertEqual(replies[0]["model"], "deepseek")
        self.assertEqual(replies[0]["content"][1]["type"], "tool_use")

    def test_newer_openai_models_param_and_errors(self):
        fake = FakeOpenAI([chat("ok")])
        try:
            prof = Profile("gpt", "openai", "some-model", key="openai", base_url=fake.url,
                           max_tokens_param="max_completion_tokens")
            make(prof, "k").complete("sys", [{"role": "user", "text": "hi"}], [], 100)
            self.assertEqual(fake.requests[0]["body"]["max_completion_tokens"], 100)
            self.assertNotIn("max_tokens", fake.requests[0]["body"])
            self.assertNotIn("tools", fake.requests[0]["body"])
        finally:
            fake.stop()
        bad = FakeOpenAI([], status=401)
        try:
            with self.assertRaises(ProviderError) as e:
                make(Profile("x", "openai", "m", key="x", base_url=bad.url), "k").complete("s", [{"role": "user", "text": "hi"}], [], 10)
            self.assertIn("HTTP 401: Invalid API key", str(e.exception))
        finally:
            bad.stop()
        with self.assertRaises(ProviderError) as e:
            make(Profile("deepseek", "openai", "m", key="deepseek"), None)
        self.assertIn("set-key deepseek", str(e.exception))
        local = make(Profile("local", "openai", "m", key="", base_url="http://127.0.0.1:9/v1"), None)
        with self.assertRaises(ProviderError):
            local.complete("s", [{"role": "user", "text": "hi"}], [], 10)  # nothing listens there


class SwitchingTest(ServiceBase):
    def test_conversation_moves_between_providers(self):
        history = [
            {"role": "user", "text": "first"},
            {"role": "assistant", "text": "Looking.", "tool_calls": [ToolCall("t1", "list_dir", {})],
             "native": {"anthropic": ["<exact claude blocks>"]}},
            {"role": "tool_results", "results": [{"id": "t1", "content": "a.txt", "is_error": False}]},
            {"role": "assistant", "text": "There is a.txt.", "tool_calls": [], "native": {}},
            {"role": "user", "text": "second"},
        ]
        anth = AnthropicProvider(Profile("claude", "anthropic", "m"), client=object()).to_native(history)
        self.assertEqual(anth[1]["content"], ["<exact claude blocks>"])  # same provider: exact replay
        self.assertEqual(anth[2]["content"][0]["type"], "tool_result")
        oai = OpenAIProvider(Profile("deepseek", "openai", "m", base_url="http://x"), api_key="k").to_native("sys", history)
        self.assertEqual([m["role"] for m in oai], ["system", "user", "assistant", "tool", "assistant", "user"])
        self.assertEqual(oai[2]["tool_calls"][0]["id"], "t1")


class ProfilesAndKeysTest(unittest.TestCase):
    def test_profiles_from_shipped_config(self):
        cfg = config.load(ROOT / "config.toml")
        self.assertEqual(cfg.model, "claude")
        self.assertIn("deepseek", cfg.models)
        self.assertNotIn("gpt", cfg.models)  # no model name filled in yet, so not offered
        self.assertEqual(cfg.profile("deepseek").base_url, "https://api.deepseek.com")

    def test_old_config_still_works(self):
        with tempfile.TemporaryDirectory() as d:
            text = (ROOT / "config.toml").read_text()
            start = text.index("# --- Model profiles")
            end = text.index("[workspace]")
            old = text[:start] + text[end:]
            old = old.replace('model = "claude"', 'model = "claude-sonnet-4-5"')
            p = Path(d) / "config.toml"
            p.write_text(old)
            cfg = config.load(p)
            self.assertEqual(list(cfg.models), ["claude"])
            self.assertEqual(cfg.profile(None).model, "claude-sonnet-4-5")

    def test_key_lookup(self):
        with tempfile.TemporaryDirectory() as d:
            old_dir, old_file = config.KEY_DIR, config.KEY_FILE
            config.KEY_DIR, config.KEY_FILE = Path(d) / "keys", Path(d) / "api_key"
            saved = {k: os.environ.pop(k) for k in ("ANTHROPIC_API_KEY", "UVMC_KEY_DEEPSEEK") if k in os.environ}
            try:
                self.assertIsNone(config.api_key("deepseek"))
                (Path(d) / "keys").mkdir()
                (Path(d) / "keys" / "deepseek").write_text("sk-deep\n")
                (Path(d) / "api_key").write_text("sk-ant\n")
                self.assertEqual(config.api_key("deepseek"), "sk-deep")
                self.assertEqual(config.api_key("anthropic"), "sk-ant")
                os.environ["UVMC_KEY_DEEPSEEK"] = "from-env"
                self.assertEqual(config.api_key("deepseek"), "from-env")
                self.assertTrue(config.has_key(Profile("local", "openai", "m", key="")))
            finally:
                os.environ.pop("UVMC_KEY_DEEPSEEK", None)
                os.environ.update(saved)
                config.KEY_DIR, config.KEY_FILE = old_dir, old_file


class ControllerModelTest(ServiceBase):
    def test_pick_model_per_conversation(self):
        self.cfg.models["deepseek"] = Profile("deepseek", "openai", "m", key="deepseek", label="DeepSeek")
        used = []
        fake = FakeClient([reply(TextBlock(type="text", text="Hi."), stop="end_turn")])
        ctl = Controller(self.cfg, lambda prof: used.append(prof.name) or fake)
        self.assertEqual({m["name"] for m in ctl.snapshot()["models"]}, set(self.cfg.models))
        ctl.set_model("deepseek")
        ctl.start_task("hello")
        self.assertTrue(wait_for(lambda: ctl.state == "idle" and used))
        self.assertEqual(used, ["deepseek"])
        with self.assertRaises(KeyError):
            ctl.set_model("nope")
        kinds = [e["kind"] for e in ctl.log.read()]
        self.assertIn("model", kinds)

    def test_missing_key_is_reported(self):
        ctl = Controller(self.cfg, lambda prof: make(Profile("deepseek", "openai", "m", key="deepseek"), None))
        ctl.start_task("hello")
        self.assertTrue(wait_for(lambda: ctl.state == "idle"))
        errors = [e["text"] for e in ctl.log.read() if e["kind"] == "error"]
        self.assertIn("set-key deepseek", errors[0])


if __name__ == "__main__":
    unittest.main()
