import base64
import hashlib
import ipaddress

import pytest
from ws_fake_upstream import FakeUpstream, Respond

from waystation_common.errors import DigestMismatch, ValidationFailed
from waystation_common.settings import Settings
from waystation_server.proxy.engine import Expected, _verify
from waystation_server.proxy.http import UpstreamClients
from waystation_server.proxy.ssrf import SsrfGuard, UpstreamBlocked


@pytest.mark.parametrize(
    ('ip', 'allow_private', 'allowed'),
    [
        ('127.0.0.1', False, False),
        ('10.1.2.3', False, False),
        ('169.254.169.254', False, False),  # cloud metadata
        ('192.0.2.10', False, False),  # documentation range: not global
        ('::1', False, False),
        ('fd00::1', False, False),
        ('::ffff:127.0.0.1', False, False),  # v4-mapped loopback
        ('10.1.2.3', True, True),
        ('0.0.0.0', True, False),
        ('224.0.0.1', True, False),
        ('198.51.100.7', False, True),  # in the allow list below
    ],
)
def test_guard(ip: str, allow_private: bool, allowed: bool) -> None:
    guard = SsrfGuard(['198.51.100.0/24'])
    assert guard.allowed(ipaddress.ip_address(ip), allow_private=allow_private) is allowed


async def test_guard_rejects_hosts_resolving_to_private_addresses() -> None:
    with pytest.raises(UpstreamBlocked):
        await SsrfGuard().resolve('localhost', 80, allow_private=False)
    assert await SsrfGuard().resolve('127.0.0.1', 80, allow_private=True)


async def test_connect_time_guard() -> None:
    """The guard runs inside the HTTP client: a direct request to a loopback upstream is refused."""
    clients = UpstreamClients(Settings())
    async with FakeUpstream({'/x': Respond(200, {}, b'x')}) as upstream:
        with pytest.raises(UpstreamBlocked):
            await clients.client_for(upstream.url, allow_private=False).get(upstream.url + 'x')
        allowed = await clients.client_for(upstream.url, allow_private=True).get(upstream.url + 'x')
        assert allowed.content == b'x'
        assert upstream.hits('/x') == 1
    await clients.aclose()


def test_expected_from_sri() -> None:
    data = b'tarball'
    sri = 'sha512-' + base64.b64encode(hashlib.sha512(data).digest()).decode()
    expected = Expected.from_sri(sri, size=len(data))
    assert expected.sha512 == hashlib.sha512(data).hexdigest()
    assert _verify(expected, data)['sha512'] == expected.sha512
    with pytest.raises(DigestMismatch):
        _verify(expected, b'other!!')
    with pytest.raises(ValidationFailed):
        Expected.from_sri('sha512-!!!')


def test_verify_size() -> None:
    with pytest.raises(DigestMismatch):
        _verify(Expected(size=3), b'four')
    assert _verify(None, b'x') == {'sha256': hashlib.sha256(b'x').hexdigest()}
