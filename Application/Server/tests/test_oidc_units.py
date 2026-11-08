"""OIDC relying party units (T-A02): discovery, JWKS and key rotation, ID token validation edge cases (wrong
audience/issuer, expiry, nonce, algorithm confusion, unknown key ids), claim mapping, PKCE and logout URLs."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

import pytest
from joserfc.jwk import ECKey, OKPKey
from ws_oidc import CLIENT_ID, CLIENT_SECRET, ISSUER, FakeIdp, query_of, rsa_key

from waystation_server.security import oidc
from waystation_server.security.oidc import FlowSecrets, OidcClient, OidcError
from waystation_server.security.providers import OidcClientAuth, OidcConfigBase

NONCE = 'nonce-123'


def config(**overrides: Any) -> OidcConfigBase:
    values: dict[str, Any] = {'issuer': ISSUER, 'client_id': CLIENT_ID}
    values.update(overrides)
    return OidcConfigBase(**values)


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


@pytest.fixture
def client(idp: FakeIdp) -> OidcClient:
    return OidcClient(idp)


async def verify(client: OidcClient, token: str, cfg: OidcConfigBase | None = None, nonce: str = NONCE) -> Any:
    cfg = cfg or config()
    meta = await client.metadata(cfg)
    return await client.verify_id_token(token, meta, cfg, nonce=nonce)


async def reason(client: OidcClient, token: str, cfg: OidcConfigBase | None = None, nonce: str = NONCE) -> str:
    with pytest.raises(OidcError) as caught:
        await verify(client, token, cfg, nonce)
    return caught.value.reason


# ---------------------------------------------------------------------------- discovery
async def test_discovery_is_parsed_and_cached(client: OidcClient, idp: FakeIdp) -> None:
    meta = await client.metadata(config())
    assert meta.issuer == ISSUER
    assert meta.token_endpoint.endswith('/token') and meta.end_session_endpoint
    assert meta.code_challenge_methods == ('plain', 'S256')
    await client.metadata(config())
    assert idp.count('/openid-configuration') == 1
    await client.metadata(config(), fresh=True)
    assert idp.count('/openid-configuration') == 2
    client.forget()
    await client.metadata(config())
    assert idp.count('/openid-configuration') == 3


@pytest.mark.parametrize(
    ('override', 'expected'),
    [
        ({'issuer': 'https://evil.example.com/'}, 'issuer_mismatch'),
        ({'token_endpoint': None}, 'discovery'),
        ({'jwks_uri': 'http://idp.example.com/certs'}, 'discovery'),
        ({'authorization_endpoint': 'javascript:alert(1)'}, 'discovery'),
        ({'userinfo_endpoint': 42}, 'discovery'),
    ],
)
async def test_discovery_refuses_bad_documents(
    client: OidcClient, idp: FakeIdp, override: dict[str, Any], expected: str
) -> None:
    idp.discovery_overrides = override
    with pytest.raises(OidcError) as caught:
        await client.metadata(config())
    assert caught.value.reason == expected


async def test_discovery_http_errors(client: OidcClient, idp: FakeIdp) -> None:
    idp.fail = {'/openid-configuration': 500}
    with pytest.raises(OidcError, match='HTTP 500'):
        await client.metadata(config())


def test_check_issuer() -> None:
    assert oidc.check_issuer('https://idp.example.com/realms/x', allow_private=False) is None
    assert oidc.check_issuer('http://idp.example.com', allow_private=True) is None
    assert oidc.check_issuer('http://idp.example.com', allow_private=False)
    assert oidc.check_issuer('ftp://idp.example.com', allow_private=True)
    assert oidc.check_issuer('https://user:pw@idp.example.com', allow_private=True)
    assert oidc.check_issuer('https://idp.example.com/?q=1', allow_private=True)


def test_http_result_json() -> None:
    assert oidc.HttpResult(200, 'application/json', b'{"a": 1}').json() == {'a': 1}
    for body in (b'[1]', b'not json'):
        with pytest.raises(OidcError):
            oidc.HttpResult(200, 'application/json', body).json()


# ---------------------------------------------------------------------------- keys
def test_jwks_parsing_skips_unusable_keys() -> None:
    sig = rsa_key('sig')
    enc = rsa_key('enc').as_dict(private=False) | {'use': 'enc'}
    private = rsa_key('priv').as_dict(private=True)
    oct_key = {'kty': 'oct', 'k': 'c2VjcmV0', 'kid': 'hmac'}
    broken = {'kty': 'RSA', 'kid': 'broken', 'n': '!!', 'e': 'AQAB'}
    keys = oidc.parse_jwks({'keys': [sig.as_dict(private=False), enc, private, oct_key, broken, 'junk']})
    assert [k.kid for k in keys] == ['sig', 'priv']
    assert not keys[1].key.is_private  # private members are stripped before import
    with pytest.raises(OidcError):
        oidc.parse_jwks({'keys': 'none'})


def test_key_fits_by_type_curve_and_alg() -> None:
    rsa = oidc.SigningKey('r', None, rsa_key('r'))
    p256 = oidc.SigningKey('e', None, ECKey.generate_key('P-256'))
    p384 = oidc.SigningKey('e3', 'ES384', ECKey.generate_key('P-384'))
    ed = oidc.SigningKey('o', None, OKPKey.generate_key('Ed25519'))
    assert oidc.key_fits(rsa, 'RS256') and oidc.key_fits(rsa, 'PS512')
    assert not oidc.key_fits(rsa, 'ES256') and not oidc.key_fits(rsa, 'HS256')
    assert oidc.key_fits(p256, 'ES256') and not oidc.key_fits(p256, 'ES384')
    assert oidc.key_fits(p384, 'ES384') and not oidc.key_fits(p384, 'ES256')
    assert oidc.key_fits(ed, 'EdDSA') and not oidc.key_fits(ed, 'RS256')
    pinned = oidc.SigningKey('r2', 'RS512', rsa_key('r2'))
    assert not oidc.key_fits(pinned, 'RS256')
    assert oidc.select_key((rsa,), 'RS256', None) is rsa
    assert oidc.select_key((rsa, pinned), 'RS512', None) is None  # ambiguous without a kid
    assert oidc.select_key((rsa, pinned), 'RS512', 'r2') is pinned


# ---------------------------------------------------------------------------- ID tokens
async def test_valid_tokens_for_each_key_type(client: OidcClient, idp: FakeIdp) -> None:
    ec = ECKey.generate_key('P-256', parameters={'kid': 'ec'})
    ed = OKPKey.generate_key('Ed25519', parameters={'kid': 'ed'})
    idp.keys += [ec, ed]
    for key, alg in ((idp.keys[0], 'RS256'), (ec, 'ES256'), (ed, 'EdDSA'), (idp.keys[0], 'PS256')):
        claims = await verify(client, idp.mint(idp.claims(NONCE), key=key, alg=alg))
        assert claims['sub'] == 'sub-alice'


@pytest.mark.parametrize(
    ('overrides', 'expected'),
    [
        ({'aud': 'someone-else'}, 'aud'),
        ({'aud': ['someone-else', 'other']}, 'aud'),
        ({'aud': [CLIENT_ID, 'other']}, 'azp'),  # several audiences need azp
        ({'azp': 'other'}, 'azp'),
        ({'iss': 'https://evil.example.com/realms/test'}, 'iss'),
        ({'exp': int(time.time()) - 120}, 'exp'),
        ({'exp': None}, 'exp'),
        ({'exp': True}, 'exp'),
        ({'iat': int(time.time()) + 600}, 'iat'),
        ({'iat': int(time.time()) - 2 * 86400}, 'iat'),
        ({'iat': None}, 'iat'),
        ({'nbf': int(time.time()) + 600}, 'nbf'),
        ({'nonce': 'other-nonce'}, 'nonce'),
        ({'nonce': None}, 'nonce'),
        ({'sub': None}, 'sub'),
        ({'sub': ''}, 'sub'),
    ],
)
async def test_claims_are_checked(client: OidcClient, idp: FakeIdp, overrides: dict[str, Any], expected: str) -> None:
    token = idp.mint(idp.claims(NONCE, **overrides))
    assert await reason(client, token) == expected


async def test_azp_with_several_audiences_is_accepted(client: OidcClient, idp: FakeIdp) -> None:
    token = idp.mint(idp.claims(NONCE, aud=[CLIENT_ID, 'account'], azp=CLIENT_ID))
    assert (await verify(client, token))['azp'] == CLIENT_ID


async def test_expiry_leeway(client: OidcClient, idp: FakeIdp) -> None:
    token = idp.mint(idp.claims(NONCE, exp=int(time.time()) - 30))
    assert (await verify(client, token))['sub'] == 'sub-alice'


async def test_alg_none_and_unsigned_tokens_are_refused(client: OidcClient, idp: FakeIdp) -> None:
    def b64(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b'=').decode()

    claims = idp.claims(NONCE)
    assert await reason(client, f'{b64({"alg": "none"})}.{b64(claims)}.') == 'id_token'
    assert await reason(client, f'{b64({"alg": "none"})}.{b64(claims)}.c2ln') == 'alg'
    assert await reason(client, f'{b64({"alg": "RS256"})}.{b64(claims)}') == 'id_token'  # two parts
    assert await reason(client, 'a.b.c.d.e') == 'id_token'  # JWE is not accepted
    assert await reason(client, f'{b64({"alg": "RS256"})}.x.' + 'A' * 20_000) == 'id_token'  # too large
    assert await reason(client, f'{base64.urlsafe_b64encode(b"[1]").decode()}.x.y') == 'id_token'
    assert await reason(client, '!!!.x.y') == 'id_token'


async def test_hmac_with_the_public_key_is_refused(client: OidcClient, idp: FakeIdp) -> None:
    """Algorithm confusion: an HS256 token keyed with the provider's public key must never verify."""
    public_pem = idp.keys[0].as_pem(private=False)

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b'=').decode()

    signing_input = f'{b64(b"""{"alg":"HS256","kid":"k1"}""")}.{b64(json.dumps(idp.claims(NONCE)).encode())}'
    mac = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()
    assert await reason(client, f'{signing_input}.{b64(mac)}') == 'alg'
    allowed_hs = config()
    allowed_hs.signing_algs.append('HS256')  # type: ignore[arg-type]  # even if the allowlist were tampered with
    assert await reason(client, f'{signing_input}.{b64(mac)}', allowed_hs) == 'alg'
    # Even when an administrator lists only RSA algorithms, an RS256 header with an EC key id is refused.
    ec = ECKey.generate_key('P-256', parameters={'kid': 'ec'})
    idp.keys.append(ec)
    mismatched = idp.mint(idp.claims(NONCE), key=ec, alg='ES256', kid='ec')
    _, body, signature = mismatched.split('.')
    confused = '.'.join(
        [base64.urlsafe_b64encode(b'{"alg":"RS256","kid":"ec"}').rstrip(b'=').decode(), body, signature]
    )
    assert await reason(client, confused) == 'kid'


async def test_alg_outside_the_provider_allowlist(client: OidcClient, idp: FakeIdp) -> None:
    token = idp.mint(idp.claims(NONCE))
    assert await reason(client, token, config(signing_algs=['ES256'])) == 'alg'


async def test_bad_signature_and_crit(client: OidcClient, idp: FakeIdp) -> None:
    token = idp.mint(idp.claims(NONCE))
    header, body, signature = token.split('.')
    tampered_claims = idp.claims(NONCE, preferred_username='admin')
    tampered = base64.urlsafe_b64encode(json.dumps(tampered_claims).encode()).rstrip(b'=').decode()
    assert await reason(client, f'{header}.{tampered}.{signature}') == 'signature'
    crit_header = base64.urlsafe_b64encode(b'{"alg":"RS256","kid":"k1","crit":["x"],"x":1}').rstrip(b'=').decode()
    assert await reason(client, f'{crit_header}.{body}.{signature}') == 'id_token'
    odd_kid = base64.urlsafe_b64encode(b'{"alg":"RS256","kid":7}').rstrip(b'=').decode()
    assert await reason(client, f'{odd_kid}.{body}.{signature}') == 'id_token'


async def test_unknown_kid_refreshes_jwks_once(
    client: OidcClient, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = idp.mint(idp.claims(NONCE))
    await verify(client, first)
    assert idp.count('/certs') == 1
    # The provider rotates: a new key appears; a token signed with it triggers one JWKS refresh.
    rotated = rsa_key('k2')
    idp.keys.append(rotated)
    monkeypatch.setattr(oidc, 'JWKS_MIN_REFRESH_S', 0.0)
    assert (await verify(client, idp.mint(idp.claims(NONCE), key=rotated)))['sub'] == 'sub-alice'
    assert idp.count('/certs') == 2
    # A key id the provider does not have: one refresh, then refused.
    stranger = rsa_key('k-unknown')
    assert await reason(client, idp.mint(idp.claims(NONCE), key=stranger)) == 'kid'
    assert idp.count('/certs') == 3
    # Refreshes are rate limited: a burst of unknown key ids does not hammer the provider.
    monkeypatch.setattr(oidc, 'JWKS_MIN_REFRESH_S', 60.0)
    for _ in range(3):
        assert await reason(client, idp.mint(idp.claims(NONCE), key=stranger)) == 'kid'
    assert idp.count('/certs') == 3


async def test_jwks_failures(client: OidcClient, idp: FakeIdp) -> None:
    idp.fail = {'/certs': 503}
    with pytest.raises(OidcError, match='HTTP 503'):
        await verify(client, idp.mint(idp.claims(NONCE)))


# ---------------------------------------------------------------------------- claims → identity
def test_identity_mapping() -> None:
    claims = {
        'sub': 's1',
        'preferred_username': 'Alice',
        'name': ' Alice Example ',
        'email': 'alice@example.com',
        'realm_access': {'roles': ['ops', 'dev']},
        'groups': ['/platform/ops', 'devs', '/', 5, ''],
    }
    identity = oidc.identity_from('oidc:p1', config(), claims)
    assert identity.username == 'alice' and identity.display_name == 'Alice Example'
    assert identity.provider == 'oidc:p1' and identity.subject == 's1'
    assert [(g.id, g.name) for g in identity.groups] == [('/platform/ops', 'ops'), ('devs', 'devs'), ('/', '/')]
    dotted = oidc.identity_from('oidc:p1', config(groups_claim='realm_access.roles'), claims)
    assert [g.name for g in dotted.groups] == ['ops', 'dev']
    single = oidc.identity_from('oidc:p1', config(groups_claim='team'), {**claims, 'team': 'blue'})
    assert [g.id for g in single.groups] == ['blue']
    none = oidc.identity_from('oidc:p1', config(groups_claim=None, display_name_claim='missing'), claims)
    assert none.groups == () and none.display_name == 'Alice'
    assert oidc.claim({'a.b': 1, 'a': {'b': 2}}, 'a.b') == 1
    assert oidc.claim({'a': {'b': 2}}, 'a.c') is None


def test_identity_refusals() -> None:
    base = {'sub': 's1', 'preferred_username': 'alice', 'email': 'alice@example.com', 'groups': ['devs']}
    with pytest.raises(OidcError, match='username'):
        oidc.identity_from('oidc:p', config(), {**base, 'preferred_username': '   '})
    with pytest.raises(OidcError, match='username'):
        oidc.identity_from('oidc:p', config(), {**base, 'preferred_username': 'a\u0000b'})
    with pytest.raises(OidcError) as caught:
        oidc.identity_from('oidc:p', config(username_claim='email'), {**base, 'email_verified': False})
    assert caught.value.reason == 'email_unverified'
    assert oidc.identity_from('oidc:p', config(username_claim='email'), base).username == 'alice@example.com'
    with pytest.raises(OidcError) as caught:
        oidc.identity_from('oidc:p', config(require_group='admins'), base)
    assert caught.value.reason == 'require_group'
    assert oidc.identity_from('oidc:p', config(require_group='DEVS'), base).groups


def test_group_list_is_bounded() -> None:
    groups = oidc.groups_from([f'g{i}' for i in range(800)] + ['x' * 2000])
    assert len(groups) == oidc.MAX_GROUPS


# ---------------------------------------------------------------------------- flow helpers
def test_pkce_s256_matches_rfc7636() -> None:
    flow = FlowSecrets('s', 'n', 'dBjftJeZ4CVP-mJ92K9mfO2RNBzBRAokwSD5YomHg5uBc')
    expected = base64.urlsafe_b64encode(hashlib.sha256(flow.verifier.encode()).digest()).rstrip(b'=').decode()
    assert flow.challenge == expected
    fresh = FlowSecrets.new()
    assert len({fresh.state, fresh.nonce, fresh.verifier}) == 3 and len(fresh.verifier) >= 43


async def test_authorization_url(client: OidcClient) -> None:
    meta = await client.metadata(config())
    flow = FlowSecrets.new()
    cfg = config(scopes=['profile', 'groups'])
    url = oidc.authorization_url(meta, cfg, flow, redirect_uri='https://registry.example.com/auth/oidc/p/callback')
    query = query_of(url)
    assert url.startswith(meta.authorization_endpoint + '?')
    assert query['response_type'] == 'code' and query['client_id'] == CLIENT_ID
    assert query['scope'] == 'openid profile groups'
    assert query['state'] == flow.state and query['nonce'] == flow.nonce
    assert query['code_challenge'] == flow.challenge and query['code_challenge_method'] == 'S256'
    assert 'code_verifier' not in query


def test_end_session_url() -> None:
    meta = oidc.parse_metadata(FakeIdp().discovery(), ISSUER)
    url = oidc.end_session_url(meta, config(), id_token_hint='tok', post_logout_redirect_uri='https://x.example.com/')
    assert url is not None
    assert query_of(url) == {
        'client_id': CLIENT_ID,
        'post_logout_redirect_uri': 'https://x.example.com/',
        'id_token_hint': 'tok',
    }
    assert oidc.end_session_url(meta, config(rp_logout=False), id_token_hint=None, post_logout_redirect_uri='/') is None
    bare = oidc.parse_metadata({**FakeIdp().discovery(), 'end_session_endpoint': None}, ISSUER)
    assert oidc.end_session_url(bare, config(), id_token_hint=None, post_logout_redirect_uri='/') is None
    with_query = oidc.parse_metadata({**FakeIdp().discovery(), 'end_session_endpoint': f'{ISSUER}/logout?x=1'}, ISSUER)
    hinted = oidc.end_session_url(with_query, config(), id_token_hint=None, post_logout_redirect_uri='/')
    assert hinted is not None and '?x=1&client_id=' in hinted and 'id_token_hint' not in hinted


def test_client_authentication_methods() -> None:
    form: dict[str, str] = {}
    headers = oidc.client_auth(config(client_id='a b:c'), 'p@ss:word', form)
    raw = base64.b64decode(headers['Authorization'].removeprefix('Basic ')).decode()
    assert raw == 'a%20b%3Ac:p%40ss%3Aword' and form == {}
    form = {}
    assert oidc.client_auth(config(client_auth=OidcClientAuth.CLIENT_SECRET_POST), 'sec', form) == {}
    assert form == {'client_id': CLIENT_ID, 'client_secret': 'sec'}
    form = {}
    assert oidc.client_auth(config(client_auth=OidcClientAuth.NONE), 'ignored', form) == {}
    assert form == {'client_id': CLIENT_ID}


# ---------------------------------------------------------------------------- the whole exchange
async def test_sign_in_exchange_and_userinfo(client: OidcClient, idp: FakeIdp) -> None:
    flow = FlowSecrets.new()
    code = idp.issue_code(idp.mint(idp.claims(flow.nonce, groups=None)))
    idp.userinfo_claims = {'groups': ['from-userinfo'], 'iss': 'https://evil.example.com', 'name': 'Other'}
    result = await client.sign_in(
        'oidc:p',
        config(use_userinfo=True),
        CLIENT_SECRET,
        code=code,
        redirect_uri='https://registry.example.com/auth/oidc/p/callback',
        flow_nonce=flow.nonce,
        verifier=flow.verifier,
    )
    assert result.identity.username == 'alice'
    assert [g.id for g in result.identity.groups] == ['from-userinfo']  # userinfo fills gaps
    assert result.identity.display_name == 'Alice Example'  # the ID token wins
    assert result.claims['iss'] == ISSUER
    _, _, headers, form = next(c for c in idp.calls if c[1].endswith('/token'))
    assert form['code_verifier'] == flow.verifier and headers['Authorization'].startswith('Basic ')


async def test_exchange_failures(client: OidcClient, idp: FakeIdp) -> None:
    cfg = config()
    meta = await client.metadata(cfg)

    async def exchange() -> None:
        await client.exchange(
            meta, cfg, CLIENT_SECRET, code='nope', redirect_uri='https://r.example.com/', verifier='v'
        )

    with pytest.raises(OidcError, match='invalid_grant'):
        await exchange()
    idp.token_body, idp.token_status = {'access_token': 'a'}, 200
    with pytest.raises(OidcError, match='no ID token'):
        await exchange()
    idp.token_body, idp.token_status = {}, 502
    with pytest.raises(OidcError, match='HTTP 502'):
        await exchange()


async def test_userinfo_must_be_about_the_same_subject(client: OidcClient, idp: FakeIdp) -> None:
    cfg = config()
    meta = await client.metadata(cfg)
    with pytest.raises(OidcError, match='another subject'):
        await client.userinfo(meta, cfg, 'at', 'someone-else')
    idp.fail = {'/userinfo': 401}
    with pytest.raises(OidcError, match='HTTP 401'):
        await client.userinfo(meta, cfg, 'at', 'sub-alice')
    bare = oidc.parse_metadata({**idp.discovery(), 'userinfo_endpoint': None}, ISSUER)
    with pytest.raises(OidcError, match='no userinfo'):
        await client.userinfo(bare, cfg, 'at', 'sub-alice')


async def test_diagnose(client: OidcClient, idp: FakeIdp) -> None:
    result = await client.diagnose(config(use_userinfo=True), CLIENT_SECRET, redirect_uri='https://r.example.com/cb')
    assert result.ok, result.checks
    names = {c.name: c for c in result.checks}
    assert set(names) == {'discovery', 'jwks', 'algorithms', 'pkce', 'userinfo', 'logout', 'client'}
    assert names['client'].message == 'client credentials accepted' and result.keys == 1
    # a public client the provider does not know as public: refused
    refused = await client.diagnose(
        config(client_auth=OidcClientAuth.NONE, client_id='other'), None, redirect_uri='https://r.example.com/cb'
    )
    assert not refused.ok and next(c for c in refused.checks if c.name == 'client').ok is False
    # algorithms the provider does not use, no S256, no keys
    idp.discovery_overrides = {'code_challenge_methods_supported': ['plain']}
    odd = await client.diagnose(config(signing_algs=['ES512']), CLIENT_SECRET, redirect_uri='https://r.example.com/')
    failed = {c.name for c in odd.checks if not c.ok}
    assert failed == {'jwks', 'algorithms', 'pkce'}
    idp.discovery_overrides = {'issuer': 'https://other.example.com'}
    broken = await client.diagnose(config(), CLIENT_SECRET, redirect_uri='https://r.example.com/')
    assert [c.name for c in broken.checks] == ['discovery'] and broken.metadata is None
