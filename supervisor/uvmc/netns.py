"""Each agent user's own network (design.md 7t).

Every conversation user, job user and the agent user has a network namespace of its own,
/run/netns/uvmc-<user>, with nothing in it but its own loopback: no route anywhere, no DNS,
no other user's servers and no other program of the VM. Its commands enter it in their
private view (executor.VIEW_SCRIPT), so a server one conversation starts on 127.0.0.1 is
reachable by that conversation alone, across its commands.

The only way out is its proxy, at the usual address: the supervisor opens the proxy's port on
127.0.0.1 inside the namespace through a small root helper, which hands the listening socket
back over a socket pair on its standard input (SCM_RIGHTS). The proxy then serves that socket
from the supervisor's own process, as before.

The namespaces are made when the supervisor starts (and again when one is missing); they live
in /run, so a restart of the VM starts them fresh. Tools the agent builds keep the VM's
network: their firewall chain already lets them reach only their own proxy.
"""

from __future__ import annotations

import socket
import subprocess
import sys

from .executor import netns_name, no_sudo, root

NETNS_DIR = "/run/netns"

# As root: makes each named namespace that isn't there yet (a name left as a plain file, not
# a mount, is a leftover and is replaced), with its loopback up.
ENSURE_SCRIPT = r"""
for n; do
  mountpoint -q "/run/netns/$n" && continue
  rm -f "/run/netns/$n"
  ip netns add "$n" && ip -n "$n" link set lo up || { echo "could not make $n" >&2; exit 1; }
done
"""

# As root, inside the namespace: opens the port and hands the listening socket back on stdin.
LISTEN_SCRIPT = (
    "import socket,sys\n"
    "s=socket.socket(socket.AF_INET,socket.SOCK_STREAM)\n"
    "s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
    "s.bind(('127.0.0.1',int(sys.argv[1])))\n"
    "s.listen(128)\n"
    "socket.send_fds(socket.socket(fileno=0),[b'uvmc'],[s.fileno()])\n"
)


def ensure(users: list[str]) -> str:
    """Makes the users' namespaces that are missing. Returns what went wrong ('' when all is well)."""
    if no_sudo() or not users:
        return ""
    try:
        p = root(["sh", "-c", ENSURE_SCRIPT, "uvmc-netns", *(netns_name(u) for u in users)], timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    return "" if p.returncode == 0 else (p.stderr.decode(errors="replace").strip() or f"exit {p.returncode}")


def listen(user: str, port: int, timeout: int = 20) -> socket.socket:
    """A listening socket on 127.0.0.1:port inside the user's namespace (made first if missing).
    Raises OSError when it can't be opened."""
    problem = ensure([user])
    if problem:
        raise OSError(f"no network of its own for {user}: {problem}")
    mine, theirs = socket.socketpair()
    try:
        p = subprocess.run(["sudo", "-n", "nsenter", f"--net={NETNS_DIR}/{netns_name(user)}", "--",
                            sys.executable, "-I", "-c", LISTEN_SCRIPT, str(int(port))],
                           stdin=theirs, capture_output=True, timeout=timeout)
        theirs.close()
        if p.returncode != 0:
            raise OSError(p.stderr.decode(errors="replace").strip() or f"exit {p.returncode}")
        mine.settimeout(5)
        _, fds, _, _ = socket.recv_fds(mine, 16, 1)
        if not fds:
            raise OSError("the helper sent no socket")
        return socket.socket(fileno=fds[0])
    except subprocess.SubprocessError as e:
        raise OSError(str(e)) from e
    finally:
        theirs.close()
        mine.close()
