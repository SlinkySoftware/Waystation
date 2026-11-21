import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from waystation_common.crypto import Keyring
from waystation_server.registry_auth import image_name, request_scope
from waystation_server.security.registry_revocation import revoked
from waystation_server.security.registry_token import (
    KEY_FILE,
    REFRESH_TTL_S,
    Scope,
    SignerHolder,
    TokenSigner,
    parse_scopes,
)


def test_parse_scopes() -> None:
    scopes = parse_scopes(
        [
            'repository:library/nginx:pull',
            'repository:registry.example.com:5000/org/app:pull,push repository(plugin):x/y:pull',
            'registry:catalog:*',
            'bogus',
            'repository::pull',
            'repository:a:PULL',
            'user:x:pull',
        ]
    )
    assert scopes == [
        Scope('repository', 'library/nginx', ('pull',)),
        Scope('repository', 'registry.example.com:5000/org/app', ('pull', 'push')),
        Scope('repository', 'x/y', ('pull',)),
        Scope('registry', 'catalog', ('*',)),
    ]


@pytest.mark.parametrize(
    ('path', 'name'),
    [
        ('library/nginx/manifests/latest', 'library/nginx'),
        ('org/app/blobs/uploads/abc', 'org/app'),
        ('a/b/c/tags/list', 'a/b/c'),
        ('x/referrers/sha256:00', 'x'),
        ('plain', 'plain'),
    ],
)
def test_image_name(path: str, name: str) -> None:
    assert image_name(path) == name


def test_request_scope() -> None:
    assert request_scope('', 'GET') is None
    assert request_scope('_catalog', 'GET') == 'registry:catalog:*'
    assert request_scope('a/b/manifests/1', 'DELETE') == 'repository:a/b:delete'
    assert request_scope('a/b/blobs/uploads/', 'POST') == 'repository:a/b:pull,push'
    # cancelling an upload is part of a push, deleting a blob is a delete
    assert request_scope('a/b/blobs/uploads/0123456789abcdef0123456789abcdef', 'DELETE') == 'repository:a/b:pull,push'
    assert request_scope('a/b/blobs/sha256:' + 'a' * 64, 'DELETE') == 'repository:a/b:delete'


def test_issue_and_verify() -> None:
    signer = TokenSigner(Ed25519PrivateKey.generate())
    token, ttl = signer.issue(
        subject='anonymous', audience='registry.example.com', access=[Scope('repository', 'a/b', ('pull',))], now=1000
    )
    assert ttl == 300
    header = json.loads(base64.urlsafe_b64decode(token.split('.')[0] + '=='))
    assert header['alg'] == 'EdDSA'
    claims = signer.verify(token, audience='registry.example.com', now=1100)
    assert claims is not None
    assert claims.subject == 'anonymous'
    assert claims.access == (Scope('repository', 'a/b', ('pull',)),)
    assert signer.verify(token, audience='other.example.com', now=1100) is None
    assert signer.verify(token, audience='registry.example.com', now=1301) is None  # expired
    head, body, sig = token.split('.')
    forged_body = base64.urlsafe_b64encode(
        json.dumps({**json.loads(base64.urlsafe_b64decode(body + '==')), 'sub': 'admin'}).encode()
    ).rstrip(b'=')
    assert signer.verify(f'{head}.{forged_body.decode()}.{sig}', audience='registry.example.com', now=1100) is None
    other = TokenSigner(Ed25519PrivateKey.generate())
    assert other.verify(token, audience='registry.example.com', now=1100) is None
    assert signer.verify('not.a.jwt', audience='registry.example.com') is None


def test_refresh_tokens_are_not_access_tokens() -> None:
    signer = TokenSigner(Ed25519PrivateKey.generate())
    refresh = signer.issue_refresh(subject='u1', audience='registry.example.com', now=1000.5, token_id='t1')
    claims = signer.verify_refresh(refresh, audience='registry.example.com', now=1000 + 30 * 86400)
    assert claims is not None
    assert (claims.subject, claims.token_id, claims.access, claims.issued_at_ms) == ('u1', 't1', (), 1_000_500)
    assert signer.verify(refresh, audience='registry.example.com', now=1100) is None  # never a /v2/ credential
    assert signer.verify_refresh(refresh, audience='other.example.com', now=1100) is None
    assert signer.verify_refresh(refresh, audience='registry.example.com', now=1000 + REFRESH_TTL_S + 1) is None
    access, _ = signer.issue(subject='u1', audience='registry.example.com', access=[], now=1000)
    assert signer.verify_refresh(access, audience='registry.example.com', now=1100) is None
    issued = signer.verify(access, audience='registry.example.com', now=1100)
    assert issued is not None and issued.issued_at_ms == 1_000_000


def test_revocation_cut_off() -> None:
    assert not revoked(1000)
    assert not revoked(1000, 0, 999)
    assert revoked(1000, 0, 1000)
    assert revoked(1000, 2000)


async def test_signing_key_is_shared_and_encrypted(tmp_path: Path) -> None:
    keyring = Keyring([b'k' * 32])
    first = await SignerHolder(tmp_path, keyring).get()
    second = await SignerHolder(tmp_path, keyring).get()
    assert first.kid == second.kid
    stored = (tmp_path / 'keys' / KEY_FILE).read_text()
    assert stored.startswith('ws1.')
    assert oct((tmp_path / 'keys' / KEY_FILE).stat().st_mode & 0o777) == '0o600'
    ephemeral = await SignerHolder(tmp_path / 'other', None).get()
    assert ephemeral.kid != first.kid
