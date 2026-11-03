"""Which addresses count as "inside the tailnet" when one hub calls another.

Hub URLs travel by gossip (directory entries), enrollment and peering invitations. Without a check,
a compromised hub could announce a router's admin page, a cloud metadata service or a local port as
its URL and make every other hub send requests there (server-side request forgery). A hub URL is
accepted only when it is http(s) to a Tailscale address or to a MagicDNS name of the tailnet.
"""
from __future__ import annotations

import ipaddress
import urllib.parse
import urllib.request

RANGES = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))


def is_loopback(url: str) -> bool:
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def hub_url_ok(url: str, domain: str = "", loopback: bool = False) -> bool:
    """`domain` is the tailnet the hub belongs to (its MagicDNS suffix, e.g. tail1234.ts.net).
    `loopback` allows 127.0.0.1/localhost, for test meshes where every hub runs on one machine."""
    try:
        u = urllib.parse.urlparse(url or "")
        host = (u.hostname or "").lower().rstrip(".")
        u.port  # noqa: B018  (raises on a malformed port)
    except ValueError:
        return False
    if u.scheme not in ("http", "https") or not host or u.username or u.password:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return any(ip in n for n in RANGES) or (loopback and ip.is_loopback)
    if host == "localhost":
        return loopback
    dom = (domain or "").lower().strip(".")
    if dom:
        return host.endswith("." + dom)
    return host.endswith(".ts.net")  # tailnet not known yet: any MagicDNS name


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hub-to-hub calls never follow a redirect: an allowed hub URL could otherwise answer 302 to an
    address outside the tailnet. A redirect comes back as an HTTPError with its 3xx code."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def urlopen(req, timeout=10):
    return _opener.open(req, timeout=timeout)
