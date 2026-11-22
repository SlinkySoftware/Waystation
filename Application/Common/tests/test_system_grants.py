"""Signed system grants (adr/0012, ``waystation_common.system_grants``): bound to purpose, repository and expiry."""

import base64

import pytest

from waystation_common.system_grants import PREFETCH, TTL_S, SystemGrants

KEY = b'k' * 32
OTHER = b'o' * 32
NOW = 1_800_000_000.0


def test_a_grant_verifies_for_its_purpose_and_repository_only() -> None:
    grants = SystemGrants([KEY])
    grant = grants.sign(PREFETCH, 'repo-1', now=NOW)
    assert grants.verify(grant, PREFETCH, 'repo-1', now=NOW)
    assert grants.verify(grant, PREFETCH, 'repo-1', now=NOW + TTL_S)
    assert not grants.verify(grant, PREFETCH, 'repo-2', now=NOW)
    assert not grants.verify(grant, 'other', 'repo-1', now=NOW)


def test_expired_far_future_and_malformed_grants_are_refused() -> None:
    grants = SystemGrants([KEY])
    grant = grants.sign(PREFETCH, 'repo-1', now=NOW)
    assert not grants.verify(grant, PREFETCH, 'repo-1', now=NOW + TTL_S + 1)
    future = grants.sign(PREFETCH, 'repo-1', now=NOW + 3600)
    assert not grants.verify(future, PREFETCH, 'repo-1', now=NOW)
    expiry, _, mac = grant.partition('.')
    for bad in ('', 'x', f'{expiry}', f'{expiry}.', f'abc.{mac}', f'{expiry}.{mac[:-1]}A', grant + 'x' * 100):
        assert not grants.verify(bad, PREFETCH, 'repo-1', now=NOW)


def test_keys_rotate_and_no_keys_means_no_grants() -> None:
    old = SystemGrants([OTHER]).sign(PREFETCH, 'repo-1', now=NOW)
    rotated = SystemGrants([KEY, OTHER])  # the first key signs, every key verifies
    assert rotated.verify(old, PREFETCH, 'repo-1', now=NOW)
    assert not SystemGrants([KEY]).verify(old, PREFETCH, 'repo-1', now=NOW)
    empty = SystemGrants([])
    assert not empty.available
    assert not empty.verify(old, PREFETCH, 'repo-1', now=NOW)
    with pytest.raises(RuntimeError):
        empty.sign(PREFETCH, 'repo-1')


def test_from_secret_keys_parses_base64() -> None:
    grants = SystemGrants.from_secret_keys([base64.b64encode(KEY).decode()])
    assert grants.available
    assert SystemGrants([KEY]).verify(grants.sign(PREFETCH, 'r', now=NOW), PREFETCH, 'r', now=NOW)
