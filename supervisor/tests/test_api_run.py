"""Using UVMC from scripts: run-and-wait, automatic refusals, and the API docs."""

import os
import sys
import unittest
from pathlib import Path

os.environ["UVMC_NO_SUDO"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from anthropic.types import TextBlock, ToolUseBlock  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from test_service import ServiceBase  # noqa: E402
from test_supervisor import reply  # noqa: E402
from uvmc.service import create_app  # noqa: E402

H = {"Authorization": "Bearer t"}


def tool(cid, command):
    return ToolUseBlock(type="tool_use", id=cid, name="run_command", input={"command": command})


class RunTest(ServiceBase):
    def client(self, replies):
        self.ctl = self.controller(replies)
        return TestClient(create_app(self.ctl, "t"))

    def test_run_and_wait(self):
        c = self.client([reply(tool("c1", "echo hello")),
                         reply(TextBlock(type="text", text="It printed hello."), stop="end_turn")])
        r = c.post("/api/run", headers=H, json={"text": "say hello"})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertTrue(d["done"])
        self.assertEqual(d["reason"], "done")
        self.assertEqual(d["text"], "It printed hello.")
        self.assertEqual(d["steps"], [{"tool": "run_command", "error": False, "result": "exit code 0\nhello",
                                       "worker": False}])
        self.assertEqual(self.ctl.state, "idle")

    def test_no_ask_refuses_instead_of_waiting(self):
        c = self.client([reply(tool("c1", "rm -r old")),   # an ask pattern in the test config
                         reply(TextBlock(type="text", text="I wasn't allowed."), stop="end_turn")])
        d = c.post("/api/run", headers=H, json={"text": "clean up", "no_ask": True, "timeout": 30}).json()
        self.assertTrue(d["done"])
        self.assertEqual(d["approvals"], [{"type": "tool", "answer": "deny", "automatic": True}])
        self.assertTrue(d["steps"][0]["error"])
        self.assertFalse(self.ctl.no_ask)                      # only for that run

    def test_timeout_stops_the_task(self):
        c = self.client([reply(tool("c1", "rm -r old"))])  # waits for an approval nobody gives
        d = c.post("/api/run", headers=H, json={"text": "clean up", "timeout": 1}).json()
        self.assertFalse(d["done"])
        self.assertEqual(d["reason"], "timed out, stopped")
        self.assertEqual(self.ctl.state, "idle")

    def test_bad_requests(self):
        c = self.client([])
        self.assertEqual(c.post("/api/run", headers=H, json={"text": " "}).status_code, 400)
        self.assertEqual(c.post("/api/run", headers=H, json={"text": "x", "model": "nope"}).status_code, 400)
        self.assertEqual(c.post("/api/run", json={"text": "x"}).status_code, 401)

    def test_docs(self):
        c = self.client([])
        self.assertEqual(c.get("/docs", headers=H).status_code, 200)
        spec = c.get("/openapi.json", headers=H).json()
        self.assertIn("/api/run", spec["paths"])
        self.assertIn("Runs one task", spec["paths"]["/api/run"]["post"]["description"])
        self.assertEqual(c.get("/openapi.json").status_code, 401)


if __name__ == "__main__":
    unittest.main()
