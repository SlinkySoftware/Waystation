"""Client address and request id of an HTTP request (ARCHITECTURE §1, SPECIFICATION §4.4, §8.4).

The server is only reachable from inside the compose networks (only ``web`` publishes a port), so its direct peer
is the web tier (or another internal container). Rules:

- A peer on a loopback or private address is an internal hop: its ``X-Real-IP`` (set by the web tier from
  ``$remote_addr``, overwriting anything the client sent) is taken as the client address, and its
  ``X-Request-Id`` (nginx ``$request_id``) is reused when well formed.
- When ``WAYSTATION_TRUST_FORWARDED`` is on and that address is one of ``WAYSTATION_TRUSTED_PROXIES`` (an external
  reverse proxy in front of the web tier), ``X-Forwarded-For`` is walked right to left and the first address that
  is not a trusted proxy is the client.
- Otherwise the peer address is the client.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from waystation_common.ids import new_id
from waystation_common.settings import Settings

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_REQUEST_ID = re.compile(r'^[A-Za-z0-9._-]{8,64}$')


def parse_networks(values: Iterable[str]) -> tuple[IPNetwork, ...]:
    return tuple(ipaddress.ip_network(value.strip(), strict=False) for value in values if value.strip())


def parse_ip(value: str | None) -> IPAddress | None:
    if not value:
        return None
    value = value.strip()
    if value.startswith('[') and ']' in value:  # [v6]:port
        value = value[1 : value.index(']')]
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def in_networks(ip: IPAddress, networks: Iterable[IPNetwork]) -> bool:
    return any(ip.version == net.version and ip in net for net in networks)


# Container networks: loopback, RFC 1918, IPv6 ULA and link-local (not Python's wider ``is_private``, which also
# covers documentation and other special-purpose ranges).
INTERNAL_NETWORKS = parse_networks(
    ['127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '::1/128', 'fc00::/7', 'fe80::/10']
)


def is_internal(ip: IPAddress) -> bool:
    return in_networks(ip, INTERNAL_NETWORKS)


@dataclass(frozen=True)
class ClientResolver:
    trust_forwarded: bool
    trusted_proxies: tuple[IPNetwork, ...]

    @classmethod
    def from_settings(cls, settings: Settings) -> ClientResolver:
        return cls(settings.trust_forwarded, parse_networks(settings.trusted_proxies))

    def resolve(self, peer: str | None, headers: Mapping[str, str]) -> tuple[str, bool]:
        """(client address, came through an internal hop)."""
        peer_ip = parse_ip(peer)
        if peer_ip is None:
            return peer or 'unknown', False
        if not is_internal(peer_ip):
            return str(peer_ip), False
        real = parse_ip(headers.get('x-real-ip'))
        if real is None:
            return str(peer_ip), True
        if self.trust_forwarded and in_networks(real, self.trusted_proxies):
            chain = [parse_ip(part) for part in headers.get('x-forwarded-for', '').split(',')]
            for hop in reversed([ip for ip in chain if ip is not None]):
                if not in_networks(hop, self.trusted_proxies):
                    return str(hop), True
        return str(real), True


def request_id(headers: Mapping[str, str], *, internal: bool) -> str:
    """Reuse the web tier's request id when it came through an internal hop, else make one."""
    candidate = headers.get('x-request-id', '')
    if internal and _REQUEST_ID.fullmatch(candidate):
        return candidate
    return new_id()
