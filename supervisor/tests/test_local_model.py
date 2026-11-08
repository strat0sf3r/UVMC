"""The model on your PC: finding the PC, the profile's "{pc}", the proxy wall, the panel's status.

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
from unittest import mock

os.environ["UVMC_NO_SUDO"] = "1"
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from test_providers import FakeOpenAI, chat  # noqa: E402
from test_service import ServiceBase  # noqa: E402
from test_supervisor import FakeClient  # noqa: E402
from test_walls import UPSTREAM_HOST, start_upstream, through_proxy  # noqa: E402
from uvmc import config, pc  # noqa: E402
from uvmc.netproxy import Proxy, SiteList  # noqa: E402
from uvmc.providers import Profile, ProviderError, make  # noqa: E402
from uvmc.service import Controller  # noqa: E402

# /proc/net/route of a machine whose gateway (the PC) is 172.24.128.1, as on Hyper-V.
ROUTES = ("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
          "eth0\t00000000\t018018AC\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
          "eth0\t008018AC\t00000000\t0001\t0\t0\t100\t00F0FFFF\t0\t0\t0\n")


def routes_file(tmp, gateway_hex="018018AC"):
    f = Path(tmp) / "route"
    f.write_text(ROUTES.replace("018018AC", gateway_hex))
    return f


class PcTest(unittest.TestCase):
    def test_address_and_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = routes_file(tmp)
            self.assertEqual(pc.address(f), "172.24.128.1")
            self.assertEqual(pc.resolve("http://{pc}:8091/v1", f), "http://172.24.128.1:8091/v1")
            self.assertEqual(pc.resolve("https://api.deepseek.com", f), "https://api.deepseek.com")
            self.assertTrue(pc.is_pc("172.24.128.1", f))
            self.assertFalse(pc.is_pc("172.24.128.2", f))
            self.assertFalse(pc.is_pc("example.invalid", f))
            self.assertTrue(pc.is_pc("localhost", routes_file(tmp, "0100007F")))  # a name pointing at the PC
        missing = Path("/no/such/route")
        self.assertIsNone(pc.address(missing))
        self.assertEqual(pc.resolve("http://{pc}:8091/v1", missing), "http://{pc}:8091/v1")
        self.assertFalse(pc.is_pc("172.24.128.1", missing))

    def test_shipped_profile(self):
        p = config.load(ROOT / "config.toml").profile("local")
        self.assertEqual((p.provider, p.base_url, p.key), ("openai", "http://{pc}:8091/v1", "local"))


class ProviderOnPcTest(unittest.TestCase):
    def test_placeholder_filled_in_and_hints(self):
        fake = FakeOpenAI([chat("Hello from your PC.")])
        try:
            port = fake.url.split(":")[2].split("/")[0]
            prof = Profile("local", "openai", "local", key="local", base_url=f"http://{{pc}}:{port}/v1")
            with mock.patch.object(pc, "address", return_value="127.0.0.1"):
                reply = make(prof, "k").complete("s", [{"role": "user", "text": "hi"}], [], 10)
            self.assertEqual(reply.text, "Hello from your PC.")
            self.assertEqual(fake.requests[0]["auth"], "Bearer k")
        finally:
            fake.stop()
        bad = FakeOpenAI([], status=401)
        try:
            port = bad.url.split(":")[2].split("/")[0]
            prof = Profile("local", "openai", "local", key="local", base_url=f"http://{{pc}}:{port}/v1")
            with mock.patch.object(pc, "address", return_value="127.0.0.1"), self.assertRaises(ProviderError) as e:
                make(prof, "wrong").complete("s", [{"role": "user", "text": "hi"}], [], 10)
            self.assertIn("run windows\\local-model.cmd again", str(e.exception))
        finally:
            bad.stop()
        prof = Profile("local", "openai", "local", key="local", base_url="http://{pc}:9/v1")
        with mock.patch.object(pc, "address", return_value="127.0.0.1"), self.assertRaises(ProviderError) as e:
            make(prof, "k").complete("s", [{"role": "user", "text": "hi"}], [], 10)  # nothing listens there
        self.assertIn("local-model.cmd -Start", str(e.exception))


class ProxyWallTest(unittest.TestCase):
    def test_pc_refused_even_when_allowed(self):
        up = start_upstream()
        port = up.server_address[1]
        events, asked = [], []
        with tempfile.TemporaryDirectory() as tmp:
            sites = SiteList([UPSTREAM_HOST], Path(tmp) / "learned.txt")   # allowed, and the port too
            proxy = Proxy(0, [port], sites, lambda h, p: asked.append(h) or True, events.append).start()
            proxy.is_pc = lambda host: host == UPSTREAM_HOST               # ...but it is the PC
            try:
                r = through_proxy(proxy.port, f"CONNECT {UPSTREAM_HOST}:{port} HTTP/1.1\r\n\r\n".encode())
            finally:
                proxy.stop()
                up.shutdown()
        self.assertIn(b"403", r)
        self.assertIn(b"the PC itself", r)
        self.assertEqual(asked, [])                                        # you're not even asked
        self.assertEqual([(e["verdict"], e["reason"]) for e in events], [("deny", "the PC itself")])


class FakeLlama:
    """llama.cpp's server, as far as the panel's status line looks at it (/props, with a key)."""

    def __init__(self, key="k"):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.headers.get("Authorization") != f"Bearer {key}":
                    code, data = 401, {"error": {"message": "Invalid API Key"}}
                else:
                    code, data = 200, {"model_alias": "qwen3-8b", "build_info": "b12211-7d7f02875",
                                       "model_path": "D:\\code\\uvmc-local\\models\\Qwen3-8B-Q4_K_M.gguf",
                                       "default_generation_settings": {"n_ctx": 8192}}
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()


class StatusTest(ServiceBase):
    def status(self, port, key):
        self.cfg.models["local"] = Profile("local", "openai", "local", key="local",
                                           base_url=f"http://{{pc}}:{port}/v1", label="Local model on my PC")
        ctl = Controller(self.cfg, lambda *_: FakeClient([]))
        env = {"UVMC_KEY_LOCAL": key} if key else {}
        with mock.patch.object(pc, "address", return_value="127.0.0.1"), mock.patch.dict(os.environ, env):
            if not key:
                os.environ.pop("UVMC_KEY_LOCAL", None)
            return ctl.local_model()

    def test_running(self):
        srv = FakeLlama()
        try:
            s = self.status(srv.port, "k")
        finally:
            srv.stop()
        self.assertEqual((s["running"], s["key_ok"], s["model"], s["file"], s["context"]),
                         (True, True, "qwen3-8b", "Qwen3-8B-Q4_K_M.gguf", 8192))
        self.assertEqual(s["profile"], "local")

    def test_wrong_key_and_not_running(self):
        srv = FakeLlama()
        try:
            s = self.status(srv.port, "old")
        finally:
            srv.stop()
        self.assertEqual((s["running"], s["key_ok"]), (True, False))
        s = self.status(9, "k")
        self.assertEqual((s["running"], s["has_key"]), (False, True))
        self.assertIn("Nothing answers", s["error"])
        with mock.patch.object(config, "api_key", return_value=None):
            s = self.status(9, None)
        self.assertFalse(s["has_key"])

    def test_no_pc_profile(self):
        self.cfg.models.pop("local", None)   # e.g. you removed it from config.toml
        ctl = Controller(self.cfg, lambda *_: FakeClient([]))
        self.assertIsNone(ctl.local_model()["profile"])


if __name__ == "__main__":
    unittest.main()
