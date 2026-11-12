"""A panel preview for Programs, with a stand-in model (as the README screenshots are made).

    cd supervisor && UVMC_NO_SUDO=1 python3 tests/preview_programs.py [port]

Opens nothing by itself: browse to http://<address>:<port>/?token=token. Two test programs:
"demo-dispatcher" sends a report every 20 s through the client library (status, stats, a
table, a chart, a picture); "quiet-one" went silent. Everything lives in a temporary folder.
"""

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ["UVMC_NO_SUDO"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(ROOT.parent / "client" / "python"))

import uvicorn  # noqa: E402


from test_supervisor import FakeClient, make_cfg  # noqa: E402
from uvmc.service import Controller, create_app  # noqa: E402

def png(w: int = 120, h: int = 60) -> bytes:
    """A small gradient picture, made here (no files needed)."""
    import struct
    import zlib
    rows = b"".join(b"\x00" + bytes(v for x in range(w) for v in (40 + x * 180 // w, 90, 200 - y * 120 // h)) for y in range(h))
    chunk = lambda kind, data: struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))  # noqa: E731
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


PNG = png()


def main(port: int) -> None:
    tmp = tempfile.mkdtemp(prefix="uvmc-preview-")
    work = os.path.join(tmp, "work")
    os.makedirs(work)
    cfg = make_cfg(work, os.path.join(tmp, "logs"))
    ctl = Controller(cfg, lambda *_: FakeClient([]))
    app = create_app(ctl, "token")
    key = ctl.programs.add("demo-dispatcher", "Fills free training slots from the job queue (test data)")
    quiet = ctl.programs.add("quiet-one", "A program that stopped calling")
    ctl.programs.update("demo-dispatcher", silent_after=5)
    ctl.programs.update("quiet-one", silent_after=1)

    def demo():
        from uvmc_client import Uvmc
        u = Uvmc(url=f"http://127.0.0.1:{port}", key=key, data=os.path.join(tmp, "demo"))
        q = Uvmc(url=f"http://127.0.0.1:{port}", key=quiet, data=os.path.join(tmp, "quiet"))
        time.sleep(2)
        q.heartbeat()
        ctl.programs.registry["quiet-one"]["last_seen"] = time.time() - 23 * 60
        ctl.programs.check_silent()
        n = 0
        while True:
            n += 1
            failed = n % 3 == 0
            u.report("Training queue", ("bad" if failed else "ok", "job pilot-s3 failed" if failed else f"{2 + n % 2} running, 5 waiting"),
                     [u.stats([{"label": "Running", "value": 2 + n % 2}, {"label": "Waiting", "value": 5},
                               {"label": "Done today", "value": 4 + n, "trend": "up"}]),
                      u.table(["Job", "Machine", "State"], [["pilot-s1", "mac", "running"], ["pilot-s2", "oracle", "running"],
                                                           ["pilot-s3", "mac", "failed" if failed else "waiting"]]),
                      u.columns([u.chart(["1", "2", "3", "4", "5"], {"score": [0.1, 0.3, 0.35, 0.5, 0.48 + n / 100]})],
                                [u.image(PNG, "a tiny test picture"), u.text("Last lines of the log:")]),
                      u.section("Log", [u.code("pass 12: mac 2/2 slots, oracle 1/1\npass 13: fetched pilot-s0")])],
                     notify="job pilot-s3 failed" if failed else None)
            time.sleep(20)

    threading.Thread(target=demo, daemon=True).start()
    print(f"Preview on port {port}: open /?token=token")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8099)
