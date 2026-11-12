"""Each agent user's own network (design.md 7t, netns.py).

Offline these check the commands, the socket hand-off (the helper's script run without sudo or
a namespace) and a proxy serving a socket it was handed; the namespaces themselves are
checked in the VM by tests/check_netns.py (as the supervisor's user, which has sudo).
"""

import os
import socket
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ["UVMC_NO_SUDO"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from uvmc import netns  # noqa: E402
from uvmc.netproxy import Proxy, SiteList  # noqa: E402


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class EnsureTest(unittest.TestCase):
    def test_nothing_offline(self):
        with mock.patch("uvmc.netns.root") as root:
            self.assertEqual(netns.ensure(["agent1"]), "")
        root.assert_not_called()

    def test_makes_each_missing_one(self):
        done = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch("uvmc.netns.no_sudo", return_value=False), \
                mock.patch("uvmc.netns.root", return_value=done) as root:
            self.assertEqual(netns.ensure(["agent", "agent1"]), "")
        cmd = root.call_args[0][0]
        self.assertEqual(cmd[:3], ["sh", "-c", netns.ENSURE_SCRIPT])
        self.assertEqual(cmd[-2:], ["uvmc-agent", "uvmc-agent1"])

    def test_reports_failure(self):
        failed = subprocess.CompletedProcess([], 1, b"", b"could not make uvmc-agent1\n")
        with mock.patch("uvmc.netns.no_sudo", return_value=False), \
                mock.patch("uvmc.netns.root", return_value=failed):
            self.assertIn("uvmc-agent1", netns.ensure(["agent1"]))

    def test_the_script(self):
        s = netns.ENSURE_SCRIPT
        self.assertIn('mountpoint -q "/run/netns/$n" && continue', s)    # a real one stays (its servers too)
        self.assertIn('rm -f "/run/netns/$n"', s)                        # a leftover plain file is replaced
        self.assertIn('ip -n "$n" link set lo up', s)
        self.assertIn("exit 1", s)


@unittest.skipUnless(hasattr(socket, "send_fds"), "needs Linux (SCM_RIGHTS)")
class HandOffTest(unittest.TestCase):
    def test_helper_hands_back_a_listening_socket(self):
        # the helper's own script, run as ourselves on the normal loopback
        port = free_port()
        mine, theirs = socket.socketpair()
        p = subprocess.run([sys.executable, "-I", "-c", netns.LISTEN_SCRIPT, str(port)], stdin=theirs, timeout=20)
        theirs.close()
        self.assertEqual(p.returncode, 0)
        _, fds, _, _ = socket.recv_fds(mine, 16, 1)
        ls = socket.socket(fileno=fds[0])
        self.assertEqual(ls.getsockname(), ("127.0.0.1", port))
        c = socket.create_connection(("127.0.0.1", port), timeout=5)   # still listening after the helper ended
        ls.settimeout(5)
        conn, _ = ls.accept()
        conn.close(), c.close(), ls.close(), mine.close()

    def test_listen_runs_the_helper_in_the_users_network(self):
        port = free_port()
        real_run = subprocess.run
        seen = {}

        def fake_run(cmd, **kw):   # drop "sudo -n nsenter --net=... --" and run the rest as ourselves
            seen["cmd"] = cmd
            return real_run(cmd[cmd.index("--") + 1:], **kw)

        with mock.patch("uvmc.netns.ensure", return_value="") as ensure, \
                mock.patch("uvmc.netns.subprocess.run", side_effect=fake_run):
            ls = netns.listen("agent3", port)
        ensure.assert_called_once_with(["agent3"])
        self.assertEqual(seen["cmd"][:4], ["sudo", "-n", "nsenter", "--net=/run/netns/uvmc-agent3"])
        self.assertEqual(ls.getsockname(), ("127.0.0.1", port))
        ls.close()

    def test_listen_fails_without_its_network(self):
        with mock.patch("uvmc.netns.ensure", return_value="could not make uvmc-agent3"):
            with self.assertRaises(OSError):
                netns.listen("agent3", free_port())


class ProxyListenerTest(unittest.TestCase):
    def test_proxy_serves_the_socket_it_was_given(self):
        ls = socket.socket()
        ls.bind(("127.0.0.1", 0))
        ls.listen(16)
        port = ls.getsockname()[1]
        p = Proxy(0, [443], SiteList([], Path(os.devnull)), lambda h, pt: False, lambda info: None, listener=ls).start()
        try:
            self.assertEqual(p.port, port)
            c = socket.create_connection(("127.0.0.1", port), timeout=5)
            c.sendall(b"nonsense\r\n\r\n")
            self.assertIn(b"400", c.recv(200))        # the proxy itself answered
            c.close()
        finally:
            p.stop()


class ServiceTest(unittest.TestCase):
    def test_conversation_proxy_listens_in_its_users_network(self):
        from uvmc.service import Controller
        with mock.patch("uvmc.service.no_sudo", return_value=False), \
                mock.patch("uvmc.service.netns.listen", return_value="SOCK") as listen:
            self.assertEqual(Controller._listener("agent2", 3302), "SOCK")
        listen.assert_called_once_with("agent2", 3302)
        self.assertIsNone(Controller._listener("agent2", 3302))   # tests: the normal loopback


if __name__ == "__main__":
    unittest.main()
