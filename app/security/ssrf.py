"""SSRF protection for merchant webhook URLs.

A webhook URL is chosen by the merchant, but the request comes from inside our network, so a
URL such as http://169.254.169.254/ (cloud metadata) or http://localhost:5432/ would let a
merchant probe or attack internal services. Every address the host resolves to must be public.

Checking the URL and then letting the HTTP client resolve the host again would leave a gap
(DNS rebinding: the name resolves to a public IP for the check and a private one for the
request). So `pin()` resolves once, checks every address, and the request goes to that exact
IP. The original hostname is still sent as the Host header and as the TLS server name, so
virtual hosting and certificate verification work as usual.
"""

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, field

import httpx

from app.config import Settings

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class UnsafeUrl(Exception):
    """The URL may not be called. The message says why, and is safe to show the merchant."""


@dataclass(frozen=True)
class UrlPolicy:
    require_https: bool  # production
    allow_private: bool  # local development only

    @classmethod
    def for_settings(cls, settings: Settings) -> "UrlPolicy":
        return cls(
            require_https=settings.environment == "production",
            allow_private=settings.webhook_allow_private_urls,
        )


# The safe default: https only, public addresses only.
STRICT = UrlPolicy(require_https=True, allow_private=False)


@dataclass(frozen=True)
class PinnedRequest:
    """Where to send the request, and what to send so the server still sees the hostname."""

    url: httpx.URL  # the original URL with the host replaced by the checked IP
    headers: dict[str, str] = field(default_factory=dict)
    extensions: dict[str, str] = field(default_factory=dict)


async def resolve(host: str, port: int) -> list[str]:
    """Every address the host resolves to. Tests replace this to avoid real DNS."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrl(f"the host {host!r} could not be resolved") from exc
    return [str(info[4][0]) for info in infos]


def is_public(address: IPAddress) -> bool:
    # An IPv4 address wrapped in IPv6 (::ffff:127.0.0.1) is judged by the address inside.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    # is_global is False for private, loopback, link-local (including 169.254.169.254),
    # carrier-grade NAT, documentation and other reserved ranges.
    return address.is_global and not address.is_multicast


async def pin(url: str, policy: UrlPolicy) -> PinnedRequest:
    """Check `url` against the policy and resolve it to one IP to connect to, or raise."""
    parsed = httpx.URL(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrl("the URL must use http or https")
    if policy.require_https and parsed.scheme != "https":
        raise UnsafeUrl("the URL must use https")
    if not parsed.host:
        raise UnsafeUrl("the URL has no host")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = [
        ipaddress.ip_address(raw.split("%")[0]) for raw in await resolve(parsed.host, port)
    ]
    if not addresses:
        raise UnsafeUrl(f"the host {parsed.host!r} could not be resolved")
    # Every address must be public, not just the first: a client may try any of them.
    if not policy.allow_private and not all(is_public(address) for address in addresses):
        raise UnsafeUrl("the URL points to a private, loopback or reserved address")

    host_header = parsed.host if parsed.port is None else f"{parsed.host}:{parsed.port}"
    return PinnedRequest(
        url=parsed.copy_with(host=str(addresses[0])),
        headers={"Host": host_header},
        extensions={"sni_hostname": parsed.host} if parsed.scheme == "https" else {},
    )
