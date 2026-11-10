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
from uvmc import config, local_models, pc, settings  # noqa: E402
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


class FakeRouter:
    """llama.cpp's server in router mode (local-model 0.16+): /models, /props?model=, /models/load,
    /v1/chat/completions (records which model was asked for)."""

    def __init__(self, models=(("qwen3.5-9b", "unloaded"), ("gemma4-e4b", "loaded")), key="k"):
        self.models = [list(m) for m in models]
        self.asked, self.loads = [], []
        router = self

        class H(http.server.BaseHTTPRequestHandler):
            def reply(self, code, data):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def authorized(self):
                if self.headers.get("Authorization") == f"Bearer {key}":
                    return True
                self.reply(401, {"error": {"message": "Invalid API Key"}})
                return False

            def do_GET(self):
                if not self.authorized():
                    return
                if self.path.startswith("/models"):
                    self.reply(200, {"data": [{"id": i, "path": f"/m/{i}.gguf", "status": {"value": st}}
                                              for i, st in router.models]})
                elif self.path.startswith("/props?model="):
                    self.reply(200, {"build_info": "b13001-abc", "model_path": "D:\\m\\gemma-4-E4B_q4_0-it.gguf",
                                     "default_generation_settings": {"n_ctx": 8192}})
                else:
                    self.reply(400, {"error": {"message": "model name is missing from the request"}})

            def do_POST(self):
                if not self.authorized():
                    return
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/models/load":
                    router.loads.append(body.get("model"))
                    self.reply(200, {"success": True})
                elif self.path == "/v1/chat/completions":
                    router.asked.append(body.get("model"))
                    self.reply(200, {"choices": [{"message": {"role": "assistant", "content": "Hi."},
                                                  "finish_reason": "stop"}], "usage": {}})
                else:
                    self.reply(404, {})

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()


class CatalogTest(unittest.TestCase):
    """local_models.json: read by both PC scripts and the panel; every file's hash pinned."""

    def test_entries_complete(self):
        data = json.loads(local_models.CATALOG.read_text(encoding="utf-8"))
        fams = data["families"]
        self.assertGreaterEqual(len(fams), 6)
        self.assertIn(data["default"], [f["id"] for f in fams])
        ids, files = [], []
        for f in fams:
            with self.subTest(f["id"]):
                for k in ("id", "label", "maker", "kind", "by", "about", "good", "weak"):
                    self.assertTrue(str(f.get(k, "")).strip(), k)
                self.assertIn(f["kind"], ("official", "quantized", "uncensored"))
                self.assertTrue(f["sizes"])
            for s in f["sizes"]:
                with self.subTest(s["id"]):
                    self.assertRegex(s["id"], r"^[a-z0-9][a-z0-9.-]*$")
                    self.assertRegex(s["id"], settings.NAME_LOCAL)      # the panel can pick it
                    for k in ("label", "size", "version", "repo", "file"):
                        self.assertTrue(str(s.get(k, "")).strip(), k)
                    self.assertRegex(s["sha256"], r"^[0-9a-f]{64}$")
                    self.assertTrue(s["file"].endswith(".gguf"))
                    self.assertIsInstance(s["tested"], bool)
                    # memory: more than the file on a card, more again on a Mac; gaming cards and Macs only
                    self.assertLess(s["size_gb"], s["card_gb"])
                    self.assertLessEqual(s["card_gb"], 32)
                    self.assertLess(s["card_gb"], s["mac_gb"] + 0.01)
                    self.assertLessEqual(s["mac_gb"], 64)
                    if f["kind"] == "official":
                        self.assertIn(s["repo"].split("/")[0], ("Qwen", "google", "mistralai", "openai"))
                    ids.append(s["id"]); files.append(s["repo"] + "/" + s["file"])
        self.assertEqual(len(set(ids)), len(ids))                       # one name, one size
        self.assertEqual(len(set(files)), len(files))                   # one file, one name
        self.assertEqual(set(ids), set(local_models.catalog()))
        # the sizes on the user's PC keep their names
        for mid in ("qwen3.5-9b-uncensored-hauhaucs", "qwen3.5-9b-uncensored", "gemma4-e4b-uncensored"):
            self.assertIn(mid, ids)

    def test_fit_and_hardware(self):
        s = local_models.catalog()["qwen3.5-9b"]             # 5.7 GB, needs an 8 GB card
        card = lambda mib: {"kind": "card", "name": "x", "mib": mib}
        self.assertEqual(local_models.fit(s, card(8176)), "fits")
        self.assertEqual(local_models.fit(s, card(6400)), "tight")       # the file fits, little room left
        self.assertEqual(local_models.fit(s, card(4096)), "too_big")
        self.assertEqual(local_models.fit(s, None), "")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "hw.json"
            self.assertIsNone(local_models.hardware(p))      # not told yet
            p.write_text('{"kind": "card", "name": "AMD Radeon RX 5700 XT", "mib": 8176}')
            self.assertEqual(local_models.hardware(p), {"kind": "card", "name": "AMD Radeon RX 5700 XT", "mib": 8176})
            p.write_text('{"kind": "phone", "mib": 8176}')
            self.assertIsNone(local_models.hardware(p))
            p.write_text("not json")
            self.assertIsNone(local_models.hardware(p))

    def test_info_and_choice(self):
        i = local_models.info("gemma4-e4b")
        self.assertEqual((i["maker"], i["family"], i["size"], i["known"]), ("Google (USA)", "Gemma 4", "E4B", True))
        self.assertEqual(local_models.info("my-own"), {"id": "my-own", "label": "my-own", "known": False})
        rows = [{"id": "a", "status": "unloaded"}, {"id": "b", "status": "loaded"}]
        self.assertEqual(local_models.choose("a", rows), "a")          # picked and downloaded
        self.assertEqual(local_models.choose("gone", rows), "b")       # picked but deleted: the one on the card
        self.assertEqual(local_models.choose("local", rows), "b")      # nothing picked
        self.assertEqual(local_models.choose("", [{"id": "a", "status": "unloaded"}]), "a")
        self.assertIsNone(local_models.router_models({"model_alias": "qwen3-8b"}))   # an older local-model
        self.assertIsNone(local_models.router_models({"data": [{"id": "x"}]}))       # a plain server's list
        self.assertEqual(local_models.router_models({"data": [{"id": "x", "status": {"value": "loaded"}}]}),
                         [{"id": "x", "status": "loaded"}])


class RouterProviderTest(unittest.TestCase):
    def ask(self, srv, model):
        prof = Profile("local", "openai", model, key="local", base_url=f"http://{{pc}}:{srv.port}/v1")
        with mock.patch.object(pc, "address", return_value="127.0.0.1"):
            make(prof, "k").complete("s", [{"role": "user", "text": "hi"}], [], 10)
        return srv.asked[-1]

    def test_asks_for_the_right_model(self):
        srv = FakeRouter()
        try:
            self.assertEqual(self.ask(srv, "local"), "gemma4-e4b")      # nothing picked: the one on the card
            self.assertEqual(self.ask(srv, "qwen3.5-9b"), "qwen3.5-9b")  # picked in the panel
            self.assertEqual(self.ask(srv, "deleted-one"), "gemma4-e4b")
        finally:
            srv.stop()


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

    def router_ctl(self, srv):
        self.cfg.models["local"] = Profile("local", "openai", "local", key="local",
                                           base_url=f"http://{{pc}}:{srv.port}/v1", label="Local model on my PC")
        return Controller(self.cfg, lambda *_: FakeClient([]))

    def test_router_status_and_pick(self):
        srv = FakeRouter()
        try:
            ctl = self.router_ctl(srv)
            with mock.patch.object(pc, "address", return_value="127.0.0.1"), \
                    mock.patch.dict(os.environ, {"UVMC_KEY_LOCAL": "k"}), \
                    mock.patch.object(local_models, "HARDWARE", Path("/nonexistent/pc-hardware.json")):
                s = ctl.local_model()
                self.assertTrue(s["router"])
                self.assertEqual([(m["id"], m["status"]) for m in s["models"]],
                                 [("qwen3.5-9b", "unloaded"), ("gemma4-e4b", "loaded")])
                self.assertEqual(s["models"][0]["maker"], "Alibaba (China)")
                self.assertEqual((s["in_use"], s["picked"], s["context"]), ("gemma4-e4b", "", 8192))
                self.assertEqual(s["file"], "gemma-4-E4B_q4_0-it.gguf")
                fams = {f["id"]: f for f in s["families"]}
                self.assertIn("qwen3.5-9b-uncensored", [x["id"] for x in fams["qwen-huihui"]["sizes"]])
                self.assertTrue(fams["qwen"]["recommended"])
                self.assertEqual({x["fit"] for f in s["families"] for x in f["sizes"]}, {""})   # PC not told yet

                s = ctl.set_local_model("qwen3.5-9b")
                self.assertEqual((s["in_use"], s["picked"]), ("qwen3.5-9b", "qwen3.5-9b"))
                self.assertEqual(srv.loads, ["qwen3.5-9b"])                       # warmed up on the card
                self.assertEqual(ctl.cfg.models["local"].model, "qwen3.5-9b")     # what the next task asks for
                self.assertEqual(Controller(self.cfg, lambda *_: FakeClient([])).settings.local_model(),
                                 "qwen3.5-9b")                                     # kept across restarts

                with self.assertRaises(Exception) as e:
                    ctl.set_local_model("not-a-model")                             # neither downloaded nor listed
                self.assertIn("neither downloaded", str(e.exception))
                self.assertEqual(ctl.settings.local_model(), "qwen3.5-9b")         # unchanged

                s = ctl.set_local_model("gemma4-12b")                              # listed, not downloaded yet
                self.assertEqual(s["picked"], "gemma4-12b")                        # kept: used once it's there
                self.assertEqual(srv.loads, ["qwen3.5-9b"])                        # nothing asked of the PC
                self.assertNotEqual(s["in_use"], "gemma4-12b")

                with tempfile.TemporaryDirectory() as d:
                    hw = Path(d) / "hw.json"
                    hw.write_text('{"kind": "card", "name": "AMD Radeon RX 5700 XT", "mib": 8176}')
                    with mock.patch.object(local_models, "HARDWARE", hw):
                        s = ctl.local_model()
                    self.assertEqual(s["hardware"]["mib"], 8176)
                    fit = {x["id"]: x["fit"] for f in s["families"] for x in f["sizes"]}
                    self.assertEqual((fit["qwen3.5-9b"], fit["gemma4-12b"], fit["qwen3.8-27b"]), ("fits", "tight", "too_big"))

                ctl.set_local_model("")
                self.assertEqual(ctl.cfg.models["local"].model, "local")
        finally:
            srv.stop()

    def test_pick_route(self):
        srv = FakeRouter()
        try:
            ctl = self.router_ctl(srv)
            from fastapi.testclient import TestClient
            from uvmc.service import create_app
            client = TestClient(create_app(ctl, "token"))
            h = {"Authorization": "Bearer token"}
            with mock.patch.object(pc, "address", return_value="127.0.0.1"), \
                    mock.patch.dict(os.environ, {"UVMC_KEY_LOCAL": "k"}):
                r = client.post("/api/local-model", json={"model": "qwen3.5-9b"}, headers=h)
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.json()["result"]["in_use"], "qwen3.5-9b")
                r = client.post("/api/local-model", json={"model": "../../etc"}, headers=h)
                self.assertEqual(r.status_code, 400)
        finally:
            srv.stop()

    def test_no_pc_profile(self):
        self.cfg.models.pop("local", None)   # e.g. you removed it from config.toml
        ctl = Controller(self.cfg, lambda *_: FakeClient([]))
        self.assertIsNone(ctl.local_model()["profile"])


if __name__ == "__main__":
    unittest.main()
