import ipaddress

import pytest

from app.security import ssrf
from app.security.ssrf import UnsafeUrl, UrlPolicy, is_public, pin

# Captured before the autouse `dns` fixture replaces it.
REAL_RESOLVE = ssrf.resolve

OPEN = UrlPolicy(require_https=False, allow_private=False)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # loopback
        "10.1.2.3",  # private
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # link-local: cloud metadata services live here
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",  # noqa: S104 (a test value, not a bind address)
        "192.0.2.1",  # documentation range
        "224.0.0.1",  # multicast
        "::1",
        "fe80::1",
        "fc00::1",  # unique local
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:10.0.0.1",
    ],
)
def test_internal_addresses_are_not_public(address):
    assert not is_public(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"])
def test_internet_addresses_are_public(address):
    assert is_public(ipaddress.ip_address(address))


async def test_pin_sends_the_request_to_the_checked_ip_under_the_original_name():
    pinned = await pin("https://shop.example/hooks?v=1", OPEN)

    assert str(pinned.url) == "https://93.184.216.34/hooks?v=1"
    assert pinned.headers == {"Host": "shop.example"}
    assert pinned.extensions == {"sni_hostname": "shop.example"}  # TLS still checks the name


async def test_pin_keeps_a_non_default_port_and_brackets_ipv6(dns):
    dns["v6.example"] = ["2606:4700:4700::1111"]

    pinned = await pin("https://v6.example:8443/h", OPEN)

    assert str(pinned.url) == "https://[2606:4700:4700::1111]:8443/h"
    assert pinned.headers == {"Host": "v6.example:8443"}


async def test_plain_http_needs_no_tls_name():
    pinned = await pin("http://shop.example/h", OPEN)
    assert pinned.extensions == {}


@pytest.mark.parametrize(
    ("url", "records", "reason"),
    [
        ("http://127.0.0.1/h", None, "private"),
        ("http://[::1]/h", None, "private"),
        ("http://169.254.169.254/latest/meta-data", None, "private"),
        ("https://internal.example/h", ["10.0.0.7"], "private"),
        # One bad address is enough: a client may connect to any of them.
        ("https://mixed.example/h", ["93.184.216.34", "127.0.0.1"], "private"),
        ("https://nowhere.invalid/h", None, "could not be resolved"),
        ("ftp://shop.example/h", None, "http or https"),
    ],
)
async def test_unsafe_urls_are_refused(dns, url, records, reason):
    if records is not None:
        dns[url.split("/")[2]] = records
    with pytest.raises(UnsafeUrl, match=reason):
        await pin(url, OPEN)


async def test_https_can_be_required():
    with pytest.raises(UnsafeUrl, match="https"):
        await pin("http://shop.example/h", UrlPolicy(require_https=True, allow_private=False))


async def test_private_addresses_can_be_allowed_for_local_development():
    pinned = await pin(
        "http://127.0.0.1:9000/h", UrlPolicy(require_https=False, allow_private=True)
    )
    assert str(pinned.url) == "http://127.0.0.1:9000/h"


async def test_real_resolver_resolves_localhost_to_loopback():
    addresses = await REAL_RESOLVE("localhost", 80)
    assert any(ipaddress.ip_address(address).is_loopback for address in addresses)


async def test_real_resolver_reports_unknown_hosts():
    with pytest.raises(UnsafeUrl, match="could not be resolved"):
        await REAL_RESOLVE("no-such-host.invalid", 80)
