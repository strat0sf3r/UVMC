"""The private view for real (design.md 7p), in the VM, as the supervisor's user (it has sudo):

    cd supervisor && python3 tests/check_private_view.py

Two of the agent's users (the last two job users; this empties their homes) run commands the way the supervisor runs them (SystemdExecutor, in a
test box). Checked: each sees only its own processes and its own temporary folders, a file
in one's /tmp is the other's no more, the supervisor still sees everything, Stop still ends
a box's programs, and clean_user removes the private folders. Prints PASS or FAIL per check.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from uvmc import config  # noqa: E402
from uvmc.executor import PRIVATE_DIR, SystemdExecutor, agent_users, clean_user, kill_slice  # noqa: E402

cfg = config.load(Path("/etc/uvmc/config.toml") if Path("/etc/uvmc/config.toml").exists() else ROOT / "config.toml")
A, B = agent_users(cfg)[-2:]   # the last two job users: no conversation uses them, and jobs rarely do
SA, SB = "uvmc-agent-check7pa.slice", "uvmc-agent-check7pb.slice"
ok = True


def check(name, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))


def run(user, slice_name, script, timeout=20):
    ex = SystemdExecutor(cfg, slice_name, user=user, workdir="/")
    p = ex.run(script, timeout=timeout)
    return p.returncode, p.stdout.decode(errors="replace").strip(), p.stderr.decode(errors="replace").strip()


print(f"users {A} and {B}")
for u in (A, B):
    clean_user(cfg, u)

# B leaves a secret in its /tmp and /dev/shm, and runs a program with it on its command line
# while A looks (in a thread: the command lasts 8 s)
import threading  # noqa: E402
code, out, err = run(B, SB, "echo secret-b > /tmp/b.txt; echo secret-b > /dev/shm/b.shm; echo written")
check("B's command runs in its view", code == 0 and "written" in out, f"{code} {out} {err}")
bg = threading.Thread(target=run, args=(B, SB, "exec -a secret-b-arg sleep 8"))
bg.start()
time.sleep(1.5)

code, out, err = run(A, SA, "ps -eo user=,args= ; echo ---; ls -A /tmp /var/tmp /dev/shm; echo ---; "
                     "cat /proc/1/cmdline 2>&1 | head -c 80; echo; true")
procs, tmp, rest = (out.split("---") + ["", "", ""])[:3]
check("A's command runs in its view", code == 0, f"{code} {err}")
check("A sees only its own processes", procs.strip() and all(ln.split()[0] == A for ln in procs.strip().splitlines()), procs)
check("A doesn't see B's program or its arguments", "secret-b-arg" not in procs)
check("A sees none of B's temporary files", "b.txt" not in tmp and "b.shm" not in tmp, tmp)
check("A can't read the system's processes (pid 1)", "systemd" not in rest and "init" not in rest.splitlines()[0:1], rest)
ps = subprocess.run(["ps", "-u", B, "-o", "args="], capture_output=True, text=True).stdout
check("the supervisor still sees B's program", "secret-b-arg" in ps, ps)
bg.join()

code, out, _ = run(B, SB, "cat /tmp/b.txt /dev/shm/b.shm; ls -ld /tmp | cut -c1-10")
check("B keeps its own files between commands", out.count("secret-b") == 2, out)
check("B's /tmp is its alone (drwx------)", out.splitlines()[-1].startswith("drwx------"), out)

check("the shared /tmp holds no file of B's", not Path("/tmp/b.txt").exists())
listing = subprocess.run(["ls", f"/tmp/{PRIVATE_DIR}/{B}"], capture_output=True, text=True)
check("other users can't list B's private folder", listing.returncode != 0)

t0 = time.time()
bg = threading.Thread(target=run, args=(B, SB, "exec -a secret-b-stop sleep 60", 70))
bg.start()
time.sleep(1.5)
kill_slice(SB)
bg.join(20)
check("Stop still ends B's program", not bg.is_alive() and time.time() - t0 < 20)

problems = clean_user(cfg, B) + clean_user(cfg, A)
left = subprocess.run(["sudo", "-n", "find", f"/tmp/{PRIVATE_DIR}", f"/dev/shm/{PRIVATE_DIR}", f"/var/tmp/{PRIVATE_DIR}",
                       "-mindepth", "1", "-user", B], capture_output=True, text=True).stdout.strip()
check("clean_user removes the private folders", not problems and not left, f"{problems} {left}")

print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
