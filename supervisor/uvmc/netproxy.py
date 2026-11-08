"""The agent's only way onto the internet.

The VM's firewall blocks every outgoing connection from the agent user except to
this proxy on 127.0.0.1. The proxy lets allowed sites through, asks you about
others, refuses the rest, and reports every connection (site, bytes, time).

It only sees which site is contacted, not what is sent inside HTTPS.
"""

from __future__ import annotations

import ipaddress
import select
import socket
import socketserver
import threading
import time
from pathlib import Path
from typing import Callable

MAX_HEADER = 64 * 1024
IDLE_TIMEOUT = 300


def normalize_host(host: str) -> str:
    return host.strip().strip("[]").rstrip(".").lower()


def valid_site(host: str) -> bool:
    host = normalize_host(host)
    if not host or len(host) > 253:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    return all(part and len(part) <= 63 and all(c.isalnum() or c == "-" for c in part)
               for part in host.split("."))


class SiteList:
    """Allowed sites: the ones in config.toml plus the ones you approve with "Always allow"."""

    def __init__(self, configured: list[str], learned_file: Path):
        self.configured = [normalize_host(d) for d in configured]
        self.file = learned_file
        self._lock = threading.Lock()
        self.learned: list[str] = []
        try:
            self.learned = [normalize_host(line) for line in learned_file.read_text().splitlines()
                            if line.strip() and not line.startswith("#")]
        except OSError:
            pass

    def allows(self, host: str) -> bool:
        host = normalize_host(host)
        return any(host == d or host.endswith("." + d) for d in self.configured + self.learned)

    def add(self, host: str) -> None:
        host = normalize_host(host)
        if not valid_site(host):
            raise ValueError(f"'{host}' is not a valid site name.")
        with self._lock:
            if host not in self.learned and host not in self.configured:
                self.learned.append(host)
                self._save()

    def remove(self, host: str) -> None:
        host = normalize_host(host)
        with self._lock:
            if host in self.configured:
                raise ValueError(f"{host} is set in config.toml; remove it there.")
            if host in self.learned:
                self.learned.remove(host)
                self._save()

    def _save(self) -> None:
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            self.file.write_text("# Sites approved with 'Always allow' in the UVMC panel\n"
                                 + "".join(f"{h}\n" for h in self.learned))
        except OSError:
            pass

    def snapshot(self) -> dict:
        return {"configured": list(self.configured), "learned": list(self.learned)}


# decide(host, port) -> True (let through) or False (refuse). May block while you decide.
Decider = Callable[[str, int], bool]
Reporter = Callable[[dict], None]


class Proxy:
    def __init__(self, port: int, allowed_ports: list[int], sites: SiteList,
                 decide: Decider, report: Reporter, host: str = "127.0.0.1"):
        from . import pc
        self.sites, self.decide, self.report = sites, decide, report
        self.allowed_ports = set(allowed_ports)
        self.is_pc = pc.is_pc
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                proxy._handle(self.request)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server((host, port), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, name="uvmc-proxy", daemon=True)

    def start(self) -> "Proxy":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # --- one client connection ----------------------------------------------

    def _handle(self, client: socket.socket) -> None:
        client.settimeout(30)
        started = time.time()
        try:
            head, rest = self._read_head(client)
        except (OSError, ValueError):
            return
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            return self._refuse(client, 400, "Bad request")
        method = method.upper()

        if method == "CONNECT":
            host, _, port_s = target.rpartition(":")
            port = int(port_s) if port_s.isdigit() else 443
        else:
            if not target.lower().startswith("http://"):
                return self._refuse(client, 400, "This proxy only takes http:// URLs or CONNECT")
            after = target[7:]
            hostport, slash, path = after.partition("/")
            host, _, port_s = hostport.rpartition(":") if ":" in hostport.split("]")[-1] else (hostport, "", "")
            port = int(port_s) if port_s.isdigit() else 80
            path = "/" + path if slash else "/"
        host = normalize_host(host)

        info = {"host": host, "port": port, "method": method}
        if not valid_site(host):
            self._event(info, "deny", "not a valid site name", started)
            return self._refuse(client, 400, "Not a valid site name")
        if port not in self.allowed_ports:
            self._event(info, "deny", f"port {port} is not allowed", started)
            return self._refuse(client, 403, f"UVMC: port {port} is not allowed")
        if self.is_pc(host):  # the PC that runs this machine is never reachable, whatever you allow
            self._event(info, "deny", "the PC itself", started)
            return self._refuse(client, 403, "UVMC: the PC itself can't be reached from here")

        if self.sites.allows(host):
            reason = "allowed site"
        elif self.decide(host, port):
            reason = "approved by you"
        else:
            self._event(info, "deny", "not an allowed site", started)
            return self._refuse(client, 403, f"UVMC: {host} is not an allowed site. Ask the user to allow it.")

        try:
            upstream = socket.create_connection((host, port), timeout=15)
        except OSError as e:
            self._event(info, "error", f"could not connect: {e}", started)
            return self._refuse(client, 502, f"UVMC: could not connect to {host}:{port}")

        sent = received = 0
        try:
            if method == "CONNECT":
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                if rest:
                    upstream.sendall(rest)
                    sent += len(rest)
            else:
                headers = [h for h in lines[1:] if h and not h.lower().startswith(("proxy-connection:", "connection:", "keep-alive:"))]
                request = f"{method} {path} {version}\r\n" + "\r\n".join(headers) + "\r\nConnection: close\r\n\r\n"
                payload = request.encode("latin-1") + rest
                upstream.sendall(payload)
                sent += len(payload)
            up, down = self._relay(client, upstream)
            sent += up
            received += down
        except OSError:
            pass
        finally:
            upstream.close()
            self._event(info, "allow", reason, started, sent, received)

    @staticmethod
    def _read_head(sock: socket.socket) -> tuple[bytes, bytes]:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(8192)
            if not chunk:
                raise ValueError("closed")
            data += chunk
            if len(data) > MAX_HEADER:
                raise ValueError("header too large")
        head, _, rest = data.partition(b"\r\n\r\n")
        return head, rest

    @staticmethod
    def _relay(a: socket.socket, b: socket.socket) -> tuple[int, int]:
        """Copies both ways until one side closes. Returns (bytes a->b, bytes b->a)."""
        counts = {a: 0, b: 0}
        other = {a: b, b: a}
        open_ = [a, b]
        last = time.time()
        while open_:
            ready, _, _ = select.select(open_, [], [], 5)
            if not ready:
                if time.time() - last > IDLE_TIMEOUT:
                    break
                continue
            last = time.time()
            for s in ready:
                try:
                    data = s.recv(65536)
                except OSError:
                    data = b""
                if not data:
                    open_.remove(s)
                    try:
                        other[s].shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    if other[s] not in open_:
                        open_ = []
                    continue
                try:
                    other[s].sendall(data)
                except OSError:
                    open_ = []
                    break
                counts[s] += len(data)
        return counts[a], counts[b]

    def _refuse(self, client: socket.socket, code: int, text: str) -> None:
        body = (text + "\n").encode()
        phrase = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}.get(code, "Error")
        try:
            client.sendall(f"HTTP/1.1 {code} {phrase}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
                           f"Connection: close\r\n\r\n".encode() + body)
        except OSError:
            pass

    def _event(self, info: dict, verdict: str, reason: str, started: float, sent: int = 0, received: int = 0) -> None:
        try:
            self.report({**info, "verdict": verdict, "reason": reason, "sent": sent, "received": received,
                         "seconds": round(time.time() - started, 2)})
        except Exception:
            pass
