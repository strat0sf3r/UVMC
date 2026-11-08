"""The PC that runs this machine, as the machine sees it.

The PC is this machine's network gateway. On Windows the Hyper-V network gets a new range
at each restart, so a model running on the PC (windows\\local-model.cmd) can't have a fixed
address in config.toml: its profile says "{pc}", and that is filled in here, each time.

The agent never reaches the PC: the firewall only lets it talk to the proxy, and the proxy
refuses the PC's address whatever the port (see netproxy.py).
"""

from __future__ import annotations

import ipaddress
import socket
from pathlib import Path

ROUTES = Path("/proc/net/route")
PLACEHOLDER = "{pc}"


def address(routes: Path | None = None) -> str | None:
    """The default gateway's IPv4 address, or None if there is none (or not on Linux)."""
    try:
        lines = (routes or ROUTES).read_text().splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        f = line.split()
        if len(f) > 3 and f[1] == "00000000" and int(f[3], 16) & 2:  # destination 0.0.0.0, RTF_GATEWAY
            return socket.inet_ntoa(int(f[2], 16).to_bytes(4, "little"))
    return None


def resolve(url: str, routes: Path | None = None) -> str:
    """Fills in "{pc}" in a URL. Left as it is when the PC's address is not known."""
    if PLACEHOLDER not in url:
        return url
    ip = address(routes)
    return url.replace(PLACEHOLDER, ip) if ip else url


def is_pc(host: str, routes: Path | None = None) -> bool:
    """True when host (an address or a name) points at the PC."""
    ip = address(routes)
    if not ip:
        return False
    try:
        return str(ipaddress.ip_address(host.strip("[]"))) == ip
    except ValueError:
        pass
    try:
        found = {a[4][0] for a in socket.getaddrinfo(host, None, socket.AF_INET)}
    except OSError:
        return False
    return ip in found
