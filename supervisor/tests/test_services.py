"""Services you add from git (uvmc/services.py): the manifest, the approval flow, updates, what
the service gets at run time, the proxy's declared machines, removal. Offline (UVMC_NO_SUDO):
no users, units or firewall are made; their texts are checked instead. The real run is checked
in the VM by tests/check_services.py."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

os.environ["UVMC_NO_SUDO"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from test_supervisor import make_cfg  # noqa: E402
from uvmc.netproxy import Proxy, SiteList  # noqa: E402
from uvmc.programs import Programs  # noqa: E402
from uvmc.services import ServiceError, Services, parse_manifest, walls  # noqa: E402

MANIFEST = """contract = 1
name = "test-app"
title = "Test app"
description = "A small app for the tests"
run = "python3 app.py --loop"
health = "python3 app.py --check"
memory = "128M"

[network]
sites = ["example.com"]
ssh = ["box"]

[ssh.box]
host = "10.0.0.9"
user = "worker"
key = "box_key"

[[settings]]
name = "greeting"
label = "Greeting"
type = "text"
default = "hello"

[[settings]]
name = "box_key"
label = "SSH key for the box"
type = "secret"

[[settings]]
name = "notes"
label = "Notes"
type = "long text"
required = false
"""


def git(repo, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout


class ManifestTest(unittest.TestCase):
    def test_a_good_one(self):
        m = parse_manifest(MANIFEST)
        self.assertEqual((m["name"], m["memory"], m["restart"]), ("test-app", "128M", "on-failure"))
        self.assertEqual(m["machines"], [{"name": "box", "host": "10.0.0.9", "user": "worker", "key": "box_key"}])
        self.assertEqual([s["type"] for s in m["settings"]], ["text", "secret", "long text"])
        self.assertFalse(m["settings"][2]["required"])

    def test_what_is_refused(self):
        cases = {
            "contract = 1": "contract = 2",
            'name = "test-app"': 'name = "Test App"',
            'memory = "128M"': 'memory = "lots"',
            'key = "box_key"': 'key = "greeting"',              # a key must be a secret
            'run = "python3 app.py --loop"': 'run = ""',
            'sites = ["example.com"]': 'sites = ["not a site"]',
            'user = "worker"': 'user = "root; rm -rf /"',
        }
        for old, new in cases.items():
            with self.assertRaises(ServiceError, msg=new):
                parse_manifest(MANIFEST.replace(old, new))
        with self.assertRaises(ServiceError):
            parse_manifest(MANIFEST.replace("[ssh.box]", "[ssh.other]"))   # a declared machine without its table
        with self.assertRaises(ServiceError):
            parse_manifest("this is = not toml [")

    def test_walls(self):
        a = parse_manifest(MANIFEST)
        b = parse_manifest(MANIFEST.replace('description = "A small app for the tests"', 'description = "new words"'))
        c = parse_manifest(MANIFEST.replace('sites = ["example.com"]', 'sites = ["example.com", "evil.example"]'))
        self.assertEqual(walls(a), walls(b))
        self.assertNotEqual(walls(a), walls(c))


class ServicesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.cfg = make_cfg(str(t / "work"), str(t / "logs"))
        self.events = []
        self.programs = Programs(self.cfg)
        self.s = Services(self.cfg, programs=self.programs, on_event=self.events.append)
        self.repo = t / "app"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "uvmc-app.toml").write_text(MANIFEST)
        (self.repo / "app.py").write_text("print('v1')\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "first version")
        self.url = "file://" + str(self.repo)

    def tearDown(self):
        for p in self.s.proxies.values():
            p.stop()
        self.tmp.cleanup()

    def commit(self, files: dict, msg: str) -> str:
        for k, v in files.items():
            (self.repo / k).write_text(v)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", msg)
        return git(self.repo, "rev-parse", "HEAD").strip()

    def install(self):
        v = self.s.add(self.url, "main")
        return self.s.approve("test-app", v["proposal"]["commit"])

    def test_adding_only_proposes(self):
        v = self.s.add(self.url, "main")
        self.assertFalse(v["installed"])
        p = v["proposal"]
        self.assertEqual(p["kind"], "install")
        self.assertEqual(p["subject"], "first version")
        self.assertIn("app.py", p["files"])
        self.assertEqual(p["manifest"]["title"], "Test app")
        self.assertFalse(Path(self.cfg.services_dir, "test-app", "versions").exists())   # nothing checked out yet
        self.assertNotIn("test-app", self.programs.registry)
        with self.assertRaises(ServiceError):
            self.s.add(self.url, "main")                                                # same name twice

    def test_bad_sources(self):
        for url in ("ssh://github.com/x/y", "http://github.com/x/y", "https://github.com/x/y; rm -rf /", "C:/repo"):
            with self.assertRaises(ServiceError, msg=url):
                self.s.add(url)
        (self.repo / "uvmc-app.toml").unlink()
        self.commit({"README": "no manifest"}, "no manifest")
        with self.assertRaises(ServiceError) as e:
            self.s.add(self.url)
        self.assertIn("uvmc-app.toml", str(e.exception))
        self.assertEqual(list(Path(self.cfg.services_dir).glob("*/repo.git")), [])     # nothing left behind

    def test_approve_exactly_what_you_saw(self):
        v = self.s.add(self.url, "main")
        with self.assertRaises(ServiceError):
            self.s.approve("test-app", "0" * 40)
        v = self.s.approve("test-app", v["proposal"]["commit"])
        self.assertTrue(v["installed"])
        code = Path(self.cfg.services_dir, "test-app", "versions", v["commit"][:12])
        self.assertEqual((code / "app.py").read_text(), "print('v1')\n")
        run = Path(self.cfg.services_dir, "test-app", "run.sh").read_text()
        self.assertIn(f"cd {code}", run)
        self.assertTrue(run.rstrip().endswith("exec python3 app.py --loop"))
        self.assertEqual(self.programs.registry["test-app"]["kind"], "service")          # its program entry
        key = (Path(self.cfg.state_dir) / "service-secrets" / "test-app" / "uvmc-key").read_text()
        self.assertEqual(self.programs.identify(key), "test-app")
        self.assertEqual(oct(os.stat(Path(self.cfg.state_dir) / "service-secrets" / "test-app").st_mode & 0o777), "0o700")

    def test_the_unit(self):
        self.install()
        text = self.s.unit_text("test-app")
        for line in ("User=uvmc-s-test-app", "Group=uvmc-services", "Slice=uvmc-services.slice", "MemoryMax=128M",
                     "Restart=on-failure", "NoNewPrivileges=yes", "ProtectSystem=strict", "PrivateTmp=yes",
                     "ProtectProc=invisible", "CapabilityBoundingSet=", "WantedBy=multi-user.target",
                     "LoadCredential=uvmc-key:", "LoadCredential=box_key:", "UVMC_URL=http://127.0.0.1:8080"):
            self.assertIn(line, text, line)
        port = self.s.get("test-app")["port"]
        self.assertIn(f"HTTPS_PROXY=http://127.0.0.1:{port}", text)
        health = self.s.unit_text("test-app", health=True)
        self.assertIn("Type=oneshot", health)
        self.assertNotIn("[Install]", health)

    def test_settings_secrets_and_host_keys(self):
        self.install()
        self.assertEqual(set(self.s.missing("test-app")), {"SSH key for the box", "host key of box"})
        with self.assertRaises(ServiceError):
            self.s.set_enabled("test-app", True)                                        # not before they're there
        self.s.save_settings("test-app", {"greeting": 'hi "there"', "box_key": "-----BEGIN KEY-----\nabc"})
        with self.assertRaises(ServiceError):
            self.s.save_settings("test-app", {"greeting": "two\nlines"})
        sec = Path(self.cfg.state_dir) / "service-secrets" / "test-app"
        self.assertEqual((sec / "box_key").read_text(), "-----BEGIN KEY-----\nabc\n")       # files end with a newline (ssh)
        self.assertIn('APP_GREETING="hi \\"there\\""', (sec / "env").read_text())
        self.assertNotIn("abc", (sec / "env").read_text())                              # secrets never in the environment
        self.assertNotIn("abc", json.dumps(self.s.view("test-app", detail=True)))
        self.s.save_settings("test-app", {"box_key": ""})                               # empty keeps the stored one
        self.assertTrue((sec / "box_key").exists())
        with self.assertRaises(ServiceError):
            self.s.set_host_key("test-app", "box", "not a key line")
        with self.assertRaises(KeyError):
            self.s.set_host_key("test-app", "other", "10.0.0.9 ssh-ed25519 AAAAC3Nza")
        self.s.set_host_key("test-app", "box", "10.0.0.9 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA\n")
        self.assertEqual(self.s.missing("test-app"), [])
        cfg = self.s.ssh_config("test-app")
        port = self.s.get("test-app")["port"]
        for line in ("Host box", "HostName 10.0.0.9", "User worker", "IdentityFile ${CREDENTIALS_DIRECTORY}/box_key",
                     "StrictHostKeyChecking yes", f"ProxyCommand nc -X connect -x 127.0.0.1:{port} %h %p"):
            self.assertIn(line, cfg)

    def test_updates_wait_for_you(self):
        first = self.install()["commit"]
        self.assertEqual(self.s.check_updates(), [])
        new = self.commit({"app.py": "print('v2')\n"}, "second version")
        self.assertEqual(self.s.check_updates(), ["test-app"])
        p = self.s.view("test-app")["proposal"]
        self.assertEqual((p["kind"], p["commit"], p["walls_changed"]), ("update", new, False))
        self.assertEqual(p["log"][0]["subject"], "second version")
        self.assertIn("app.py", p["stat"])
        self.assertIn("+print('v2')", self.s.diff("test-app"))
        self.assertEqual(self.s.get("test-app")["commit"], first)                     # still the old one
        self.assertEqual(self.events[-1]["kind"], "service_update")
        self.s.discard("test-app")
        self.assertEqual(self.s.check_updates(), [])                                  # a discarded one doesn't come back
        newer = self.commit({"uvmc-app.toml": MANIFEST.replace('sites = ["example.com"]', 'sites = ["example.com", "more.example"]')},
                            "wants another site")
        self.s.check_updates()
        p = self.s.view("test-app")["proposal"]
        self.assertTrue(p["walls_changed"])                                            # shown as such on the card
        v = self.s.approve("test-app", newer)
        self.assertEqual(v["commit"], newer)
        self.assertEqual(v["manifest"]["sites"], ["example.com", "more.example"])
        self.assertFalse(Path(self.cfg.services_dir, "test-app", "versions", first[:12]).exists())

    def test_a_broken_manifest_in_an_update(self):
        self.install()
        bad = self.commit({"uvmc-app.toml": "contract = 1\nname = \"test-app\"\n"}, "broken manifest")
        self.s.check_updates()
        p = self.s.view("test-app")["proposal"]
        self.assertIsNone(p["manifest"])
        self.assertIn("run", p["problem"])
        with self.assertRaises(ServiceError):
            self.s.approve("test-app", bad)

    # --- a profile kept by UVMC (for apps without their own uvmc-app.toml) -------------------

    def test_a_repository_without_a_manifest(self):
        from uvmc.services import NoManifest
        (self.repo / "uvmc-app.toml").unlink()
        self.commit({"main.py": "print('a random app')\n"}, "a random app")
        with self.assertRaises(NoManifest):
            self.s.add(self.url)
        v = self.s.add(self.url, manifest_text=MANIFEST)                # you write one for it
        self.assertEqual(v["manifest_from"], "uvmc")
        self.assertEqual(v["proposal"]["manifest"]["name"], "test-app")
        v = self.s.approve("test-app", v["proposal"]["commit"])
        self.assertEqual(v["manifest"]["run"], "python3 app.py --loop")
        self.assertEqual(self.s.profile_text("test-app"), {"text": MANIFEST.strip(), "from": "uvmc"})
        # its updates use the profile kept by UVMC
        new = self.commit({"main.py": "print('v2')\n"}, "v2")
        self.s.check_updates()
        p = self.s.view("test-app")["proposal"]
        self.assertEqual((p["commit"], p["walls_changed"]), (new, False))

    def test_changing_the_profile_waits_for_you(self):
        first = self.install()
        self.assertEqual(self.s.profile_text("test-app")["from"], "repository")
        wider = MANIFEST.replace('sites = ["example.com"]', 'sites = ["example.com", "pypi.org"]')
        v = self.s.set_adapter("test-app", wider)
        p = v["proposal"]
        self.assertEqual((p["kind"], p["commit"], p["walls_changed"]), ("manifest", first["commit"], True))
        self.assertEqual(self.s.get("test-app")["manifest"]["sites"], ["example.com"])   # not yet
        with self.assertRaises(ServiceError):
            self.s.set_adapter("test-app", MANIFEST.replace('name = "test-app"', 'name = "renamed"'))
        v = self.s.approve("test-app", first["commit"])
        self.assertEqual(v["manifest"]["sites"], ["example.com", "pypi.org"])
        self.assertEqual(v["manifest_from"], "uvmc")
        self.s.set_adapter("test-app", None)                             # back to the repository's
        v = self.s.approve("test-app", first["commit"])
        self.assertEqual((v["manifest_from"], v["manifest"]["sites"]), ("repository", ["example.com"]))

    def test_versions_of_the_contract(self):
        with self.assertRaises(ServiceError) as e:
            parse_manifest(MANIFEST.replace("contract = 1", "contract = 2"))
        self.assertIn("Update UVMC", str(e.exception))
        with self.assertRaises(ServiceError) as e:
            parse_manifest(MANIFEST.replace("contract = 1", 'contract = 1\nbuild = "pip install -r requirements.txt"'))
        self.assertIn("build", str(e.exception))                           # planned keys aren't silently ignored
        with self.assertRaises(ServiceError):
            parse_manifest(MANIFEST.replace("contract = 1", 'contract = 1\nruntime = "container"'))
        m = parse_manifest(MANIFEST.replace("contract = 1", 'contract = 1\nhomepage = "https://example.com"'))
        self.assertEqual((m["runtime"], m["unknown"]), ("process", ["homepage"]))   # unknown keys: shown, not fatal

    def test_remove(self):
        self.install()
        self.s.remove("test-app")
        self.assertEqual(self.s.view(), [])
        self.assertNotIn("test-app", self.programs.registry)
        self.assertFalse(Path(self.cfg.services_dir, "test-app").exists())
        self.assertFalse((Path(self.cfg.state_dir) / "service-secrets" / "test-app").exists())

    def test_discarding_a_new_one_leaves_nothing(self):
        self.s.add(self.url)
        self.s.discard("test-app")
        self.assertEqual(self.s.view(), [])
        self.assertFalse(Path(self.cfg.services_dir, "test-app").exists())


class DeclaredMachinesTest(unittest.TestCase):
    """The proxy: an extra port only to the exact hosts declared for it."""

    def setUp(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen()
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()
        self.events = []
        self.proxy = Proxy(0, [80, 443], SiteList([], Path("/nonexistent")), lambda h, p: False, self.events.append,
                           host_ports={"127.0.0.1": [self.port]}).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.srv.accept()
                c.close()
            except OSError:
                return

    def tearDown(self):
        self.proxy.stop()
        self.srv.close()

    def connect(self, target):
        s = socket.create_connection(("127.0.0.1", self.proxy.port), timeout=5)
        s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        answer = s.recv(200).decode(errors="replace")
        s.close()
        return answer

    def test_only_the_declared_host_and_port(self):
        self.assertIn("200", self.connect(f"127.0.0.1:{self.port}"))
        from test_service import wait_for
        self.assertTrue(wait_for(lambda: self.events))                 # logged when the connection ends
        self.assertEqual(self.events[-1]["reason"], "declared machine")
        answer = self.connect(f"localhost:{self.port}")                 # same machine, other name: not declared
        self.assertIn("403", answer)
        self.assertIn(f"port {self.port} to localhost isn't declared", answer)
        self.assertIn("403", self.connect(f"127.0.0.1:{self.port + 1}"))    # declared host, other port


class RoutesTest(unittest.TestCase):
    def test_add_and_approve_through_the_api(self):
        from fastapi.testclient import TestClient
        from test_service import ServiceBase
        from uvmc.service import Controller, create_app
        from test_supervisor import FakeClient
        base = ServiceBase()
        base.setUp()
        try:
            ctl = Controller(base.cfg, lambda *_: FakeClient([]))
            c = TestClient(create_app(ctl, "t"))
            H = {"Authorization": "Bearer t"}
            repo = Path(base.tmp.name) / "app"
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
            (repo / "uvmc-app.toml").write_text(MANIFEST)
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "v1")
            r = c.post("/api/services/add", json={"url": "file://" + str(repo)}, headers=H)
            self.assertEqual(r.status_code, 200, r.text)
            commit = r.json()["proposal"]["commit"]
            self.assertEqual(c.post("/api/services/approve", json={"name": "test-app", "commit": "x"}, headers=H).status_code, 409)
            self.assertEqual(c.post("/api/services/approve", json={"name": "test-app", "commit": commit}, headers=H).status_code, 200)
            one = c.get("/api/services/test-app", headers=H).json()
            self.assertTrue(one["installed"])
            self.assertIn("settings", one)
            # its program can't be removed or re-keyed from Settings > Programs
            self.assertEqual(c.post("/api/programs/remove", json={"name": "test-app"}, headers=H).status_code, 409)
            self.assertEqual(c.post("/api/programs/key", json={"name": "test-app"}, headers=H).status_code, 409)
            for p in ctl.services.proxies.values():
                p.stop()
        finally:
            base.tearDown()


if __name__ == "__main__":
    unittest.main()
