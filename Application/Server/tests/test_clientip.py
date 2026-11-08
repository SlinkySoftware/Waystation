import pytest

from waystation_server.clientip import ClientResolver, parse_ip, parse_networks, request_id

TRUSTED = ClientResolver(trust_forwarded=True, trusted_proxies=parse_networks(['198.51.100.0/24']))
PLAIN = ClientResolver(trust_forwarded=False, trusted_proxies=())


@pytest.mark.parametrize(
    ('resolver', 'peer', 'headers', 'expected'),
    [
        (PLAIN, '203.0.113.5', {'x-real-ip': '192.0.2.1'}, ('203.0.113.5', False)),  # external peer: header ignored
        (PLAIN, '10.1.2.3', {'x-real-ip': '192.0.2.1'}, ('192.0.2.1', True)),  # web tier hop
        (PLAIN, '10.1.2.3', {}, ('10.1.2.3', True)),
        (PLAIN, '10.1.2.3', {'x-real-ip': 'garbage'}, ('10.1.2.3', True)),
        (PLAIN, None, {}, ('unknown', False)),
        (PLAIN, 'not-an-ip', {}, ('not-an-ip', False)),
        # external proxy in front of the web tier
        (
            TRUSTED,
            '10.1.2.3',
            {'x-real-ip': '198.51.100.7', 'x-forwarded-for': '192.0.2.50, 203.0.113.9, 198.51.100.7'},
            ('203.0.113.9', True),
        ),
        (PLAIN, '10.1.2.3', {'x-real-ip': '198.51.100.7', 'x-forwarded-for': '203.0.113.9'}, ('198.51.100.7', True)),
        (TRUSTED, '10.1.2.3', {'x-real-ip': '198.51.100.7', 'x-forwarded-for': '198.51.100.8'}, ('198.51.100.7', True)),
        (PLAIN, '::1', {'x-real-ip': '2001:db8::1'}, ('2001:db8::1', True)),
    ],
)
def test_resolve(resolver: ClientResolver, peer: str | None, headers: dict[str, str], expected: tuple) -> None:  # type: ignore[type-arg]
    assert resolver.resolve(peer, headers) == expected


def test_parse_ip_brackets() -> None:
    assert str(parse_ip('[2001:db8::1]:443')) == '2001:db8::1'
    assert parse_ip('') is None


def test_request_id() -> None:
    assert request_id({'x-request-id': 'abcdefgh'}, internal=True) == 'abcdefgh'
    assert request_id({'x-request-id': 'abc'}, internal=True) != 'abc'
    assert request_id({'x-request-id': 'abcdefgh'}, internal=False) != 'abcdefgh'
