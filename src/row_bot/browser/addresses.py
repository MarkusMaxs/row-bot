"""Where an address points: this computer, the local network, or the public internet.

Row-Bot's browser opens public sites like any browser. Addresses on this
computer open only once a conversation allows them (F4); other local-network
addresses (other computers, routers, cloud metadata) never open. This module
only classifies; ``browser.guard`` enforces it on the pages' requests.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Callable, Iterable
from urllib.parse import urlsplit

Resolver = Callable[[str, int], Iterable[str]]
Address = ipaddress.IPv4Address | ipaddress.IPv6Address

# Names that always mean a cloud's metadata service.
_METADATA_HOSTS = frozenset({"metadata", "metadata.aws.internal", "metadata.google.internal", "instance-data"})
_METADATA_ADDRESSES = frozenset({
    "100.100.100.200", "168.63.129.16", "169.254.169.254", "169.254.170.2",
    "192.0.0.192", "fd00:ec2::254", "fd20:ce::254",
})


def default_resolver(host: str, port: int) -> tuple[str, ...]:
    return tuple(
        str(record[4][0]).split("%", 1)[0]
        for record in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    )


def canonical_address(value: str) -> Address:
    """An IP address, with an IPv4-mapped IPv6 address read as its IPv4 one."""

    address = ipaddress.ip_address(str(value).strip("[]").split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def is_public(address: Address) -> bool:
    return bool(address.is_global)


def is_metadata_address(address: Address) -> bool:
    return address.is_link_local or str(address) in _METADATA_ADDRESSES


def is_loopback_name(host: str) -> bool:
    """``localhost`` and ``*.localhost`` always mean this computer (RFC 6761); Chromium never looks them up."""

    name = str(host or "").rstrip(".").casefold()
    return name == "localhost" or name.endswith(".localhost")


def literal_address(host: str) -> Address | None:
    try:
        return canonical_address(host)
    except ValueError:
        return None


def host_kind(host: str) -> str:
    """``public``, ``this_computer`` or ``local_network`` for a host name or IP literal.

    Only what the name itself says is used (no lookup): ``.local`` and
    ``.internal`` names, metadata names and private, link-local and reserved
    IP literals are the local network. Any other name counts as public.
    """

    name = str(host or "").strip().strip("[]").rstrip(".").casefold()
    if not name:
        return "local_network"
    if is_loopback_name(name):
        return "this_computer"
    address = literal_address(name)
    if address is not None:
        if address.is_loopback:
            return "this_computer"
        return "public" if is_public(address) and not is_metadata_address(address) else "local_network"
    if name in _METADATA_HOSTS or name.endswith((".local", ".internal", ".lan", ".home.arpa")) or "." not in name:
        return "local_network"
    return "public"


def url_host_kind(url: str) -> str | None:
    """``host_kind`` of an http(s)/ws(s) URL's host; None for other schemes (data:, blob:, about:)."""

    try:
        parsed = urlsplit(str(url or ""))
        scheme = parsed.scheme.casefold()
        host = parsed.hostname or ""
    except (TypeError, ValueError):
        return "local_network"
    if scheme not in {"http", "https", "ws", "wss"}:
        return None
    return host_kind(host)
