"""UVMC as a tool for the Claude apps on your PC (POST /mcp), off until switched on."""

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


def rpc(c, method, params=None, mid=1):
    return c.post("/mcp", headers=H, json={"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


class McpTest(ServiceBase):
    def setUp(self):
        super().setUp()
        self.ctl = self.controller([
            reply(ToolUseBlock(type="tool_use", id="c1", name="write_file", input={"path": "out.txt", "content": "42\n"})),
            reply(TextBlock(type="text", text="Wrote out.txt."), stop="end_turn")])
        self.c = TestClient(create_app(self.ctl, "t"))

    def test_off_until_switched_on(self):
        r = rpc(self.c, "initialize")
        self.assertEqual(r.status_code, 403)
        self.assertIn("Switch it on", r.json()["error"]["message"])
        self.assertEqual(self.c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code, 401)

    def test_tools(self):
        self.c.post("/api/settings/mcp", headers=H, json={"enabled": True})
        init = rpc(self.c, "initialize", {"protocolVersion": "2025-06-18"}).json()["result"]
        self.assertEqual(init["serverInfo"]["name"], "uvmc")
        self.assertEqual(self.c.post("/mcp", headers=H, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code, 202)
        names = [t["name"] for t in rpc(self.c, "tools/list").json()["result"]["tools"]]
        self.assertEqual(names, ["run_task", "task_status", "stop_task", "list_files", "read_file"])

        r = rpc(self.c, "tools/call", {"name": "run_task", "arguments": {"task": "write 42 to out.txt", "wait_seconds": 20}})
        text = r.json()["result"]["content"][0]["text"]
        self.assertIn("Ended: done.", text)
        self.assertIn("Wrote out.txt.", text)
        self.assertIn("- write_file: ok", text)

        r = rpc(self.c, "tools/call", {"name": "read_file", "arguments": {"path": "out.txt"}}).json()["result"]
        self.assertEqual(r["content"][0]["text"], "42\n")
        r = rpc(self.c, "tools/call", {"name": "list_files", "arguments": {}}).json()["result"]
        self.assertIn("out.txt (3 bytes)", r["content"][0]["text"])
        r = rpc(self.c, "tools/call", {"name": "task_status", "arguments": {}}).json()["result"]
        self.assertIn("State: idle", r["content"][0]["text"])
        r = rpc(self.c, "tools/call", {"name": "read_file", "arguments": {"path": "missing.txt"}}).json()["result"]
        self.assertTrue(r["isError"])

    def test_panel_sees_which_app_connected(self):
        self.assertEqual(self.c.get("/api/settings/mcp", headers=H).json()["seen"], [])
        rpc(self.c, "initialize", {"clientInfo": {"name": "claude-code", "version": "2.1.319"}})   # switch off
        seen = self.c.get("/api/settings/mcp", headers=H).json()["seen"]
        self.assertEqual((seen[0]["name"], seen[0]["allowed"]), ("claude-code", False))
        self.c.post("/api/settings/mcp", headers=H, json={"enabled": True})
        rpc(self.c, "initialize", {"clientInfo": {"name": "claude-code", "version": "2.1.319"}})
        self.assertTrue(self.c.get("/api/settings/mcp", headers=H).json()["seen"][0]["allowed"])

    def test_switch_shows_in_settings(self):
        self.assertFalse(self.c.get("/api/settings", headers=H).json()["mcp_enabled"])
        self.c.post("/api/settings/mcp", headers=H, json={"enabled": True})
        self.assertTrue(self.c.get("/api/settings", headers=H).json()["mcp_enabled"])


if __name__ == "__main__":
    unittest.main()
