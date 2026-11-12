"""A panel preview for Services, with a stand-in model and test apps (no real repositories).

    cd supervisor && UVMC_NO_SUDO=1 python3 tests/preview_services.py [port]

Then browse to http://<address>:<port>/?token=token and open Services. "test-app" is installed
with a new version waiting (one that also asks for another site); "other-app" waits for its
first approval. Outside a real VM nothing runs: status says so. Everything lives in a
temporary folder.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ["UVMC_NO_SUDO"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import uvicorn  # noqa: E402

from test_services import MANIFEST, git  # noqa: E402
from test_supervisor import FakeClient, make_cfg  # noqa: E402
from uvmc.service import Controller, create_app  # noqa: E402


def repo(path: Path, manifest: str, files: dict) -> str:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / "uvmc-app.toml").write_text(manifest)
    for k, v in files.items():
        (path / k).write_text(v)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "First version")
    return "file://" + str(path)


def main(port: int) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="uvmc-preview-"))
    (tmp / "work").mkdir()
    cfg = make_cfg(str(tmp / "work"), str(tmp / "logs"))
    ctl = Controller(cfg, lambda *_: FakeClient([]))
    app = create_app(ctl, "token")
    url = repo(tmp / "test-app", MANIFEST, {"app.py": "print('v1')\n"})
    v = ctl.services.add(url)
    ctl.services.approve("test-app", v["proposal"]["commit"])
    ctl.services.save_settings("test-app", {"greeting": "hello there", "box_key": "-----BEGIN KEY-----\nxyz"})
    ctl.services.set_host_key("test-app", "box", "10.0.0.9 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyForThePreview")
    ctl.services._on_net("test-app", {"host": "github.com", "port": 443, "verdict": "allow", "reason": "allowed site"})
    ctl.services._on_net("test-app", {"host": "10.0.0.5", "port": 22, "verdict": "deny", "reason": "port 22 to 10.0.0.5 isn't declared"})
    r = tmp / "test-app"
    (r / "app.py").write_text("print('v2: retries a failed fetch')\n")
    (r / "uvmc-app.toml").write_text(MANIFEST.replace('sites = ["example.com"]', 'sites = ["example.com", "pypi.org"]'))
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "Retry failed fetches; needs pypi.org")
    ctl.services.check_updates()
    repo(tmp / "other-app", MANIFEST.replace('name = "test-app"', 'name = "other-app"').replace('title = "Test app"', 'title = "Another app"'),
         {"main.py": "print('hi')\n", "README.md": "An app waiting for its first approval.\n"})
    ctl.services.add("file://" + str(tmp / "other-app"))
    # an app installed in place, its install session open in a shell, one site waiting for your answer
    import threading
    from test_services import LOCAL_PROFILE
    ctl.services.add_local(LOCAL_PROFILE)
    ctl.services.approve("random-app", "local")
    ctl.cfg.net_approval_timeout_s = 3600
    ctl.services.start_session("random-app", "shell", ["pypi.org"])
    ctl.services._used("random-app", "pypi.org")
    threading.Thread(target=ctl.services._ask, args=("random-app", "files.pythonhosted.org", 443), daemon=True).start()
    # another one with an agent working inside it, one site waiting for your answer in its conversation
    ctl.services.add_local(LOCAL_PROFILE.replace('"random-app"', '"agent-app"').replace("A random app", "An app an agent installs"))
    ctl.services.approve("agent-app", "local")
    inside = ctl.new_conversation_inside("agent-app")
    threading.Thread(target=ctl.services._ask, args=("agent-app", "github.com", 443), daemon=True).start()
    ctl.select(inside["conv"]) if hasattr(ctl, "select") else None
    print(f"Preview on port {port}: open /?token=token, then Services")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8099)
