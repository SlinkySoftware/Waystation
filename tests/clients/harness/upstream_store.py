"""Fixture store for the record/replay fake upstream (tests/clients/README.md).

A fixture is one upstream exchange: ``<root>/<host>/<slug>-<key16>.json`` (request summary + response status and
headers) and, when the response has a body, ``<slug>-<key16>.body`` (raw bytes). The key is a hash of the
*scrubbed* request, so the same function decides what is stored at record time and what is looked up at replay
time: anything secret or volatile (credentials, signed-URL query parameters) never reaches the key or the files.

Stdlib only: this module runs inside the fake-upstream container and in the dev image (unit tests).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = 'harness-redacted'

# Request headers that select a different upstream representation. Their values are part of the key (and stored).
KEY_HEADERS = ('accept', 'range', 'if-none-match', 'if-modified-since', 'if-range')

# Response headers worth replaying. Everything else (cookies, CDN node names, request ids, client IPs in
# rate-limit "source" headers, timing, HSTS...) is dropped at record time.
RESPONSE_HEADERS = frozenset(
    {
        'content-type',
        'content-length',
        'content-range',
        'content-disposition',
        'accept-ranges',
        'etag',
        'last-modified',
        'cache-control',
        'expires',
        'vary',
        'location',
        'link',
        'www-authenticate',
        'docker-content-digest',
        'docker-distribution-api-version',
        'oci-subject',
        'ratelimit-limit',
        'ratelimit-remaining',
    }
)

# Query parameters of signed/expiring URLs (S3, CloudFront, Azure SAS, generic tokens). Values are replaced.
_SECRET_QUERY = re.compile(
    r'^(x-amz-[a-z-]+|x-goog-[a-z-]+|signature|sig|se|sp|sv|st|skoid|sktid|skt|ske|sks|skv|token|access_token|'
    r'key-pair-id|policy|expires|verify|auth|apikey|api_key|password|secret|jwt)$',
    re.IGNORECASE,
)
# JSON object keys whose string values are credentials (Docker token service, OAuth responses, npm login...).
_SECRET_JSON_KEY = re.compile(r'(token|secret|password|passwd|credential|session|cookie)', re.IGNORECASE)


def scrub_url(url: str) -> str:
    """Return ``url`` with credentials and signed-URL query values replaced; order is preserved."""
    parts = urlsplit(url)
    netloc = parts.hostname or ''
    if parts.port is not None:
        netloc = f'{netloc}:{parts.port}'
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        if any(_SECRET_QUERY.match(k) for k, _ in pairs):
            query = urlencode([(k, REDACTED if _SECRET_QUERY.match(k) else v) for k, v in pairs], safe=':/@')
    return urlunsplit((parts.scheme, netloc, parts.path, query, ''))


def auth_kind(value: str | None) -> str:
    """Only the *scheme* of an Authorization header is significant (anonymous 401 vs. authorised 200)."""
    if not value:
        return 'none'
    return value.split(' ', 1)[0].lower()


@dataclass(frozen=True)
class RequestKey:
    method: str
    url: str  # scrubbed
    headers: tuple[tuple[str, str], ...]  # KEY_HEADERS present on the request, lower-case names
    auth: str
    body_sha256: str | None

    @classmethod
    def build(cls, method: str, url: str, headers: dict[str, str], body: bytes = b'') -> RequestKey:
        lower = {k.lower(): v for k, v in headers.items()}
        selected = tuple((h, lower[h].strip()) for h in KEY_HEADERS if h in lower)
        digest = hashlib.sha256(body).hexdigest() if body else None
        return cls(method.upper(), scrub_url(url), selected, auth_kind(lower.get('authorization')), digest)

    @property
    def digest(self) -> str:
        canonical = json.dumps(
            [self.method, self.url, list(self.headers), self.auth, self.body_sha256], separators=(',', ':')
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or 'unknown-host'

    @property
    def stem(self) -> str:
        path = urlsplit(self.url).path.rstrip('/')
        last = path.rsplit('/', 1)[-1] or 'root'
        slug = re.sub(r'[^A-Za-z0-9._-]+', '_', last)[:48].strip('._') or 'x'
        return f'{self.method.lower()}-{slug}-{self.digest[:16]}'

    def describe(self) -> str:
        extras = ' '.join(f'{k}={v}' for k, v in self.headers)
        return f'{self.method} {self.url} auth={self.auth} {extras}'.rstrip()


@dataclass
class Exchange:
    status: int
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b''


def scrub_response(
    status: int, headers: list[tuple[str, str]], body: bytes, drop_keys: frozenset[str] = frozenset()
) -> Exchange:
    """Keep allowlisted headers (with signed URLs scrubbed) and redact credentials in JSON bodies. ``drop_keys``
    (format.env ``DROP_JSON_KEYS``) are JSON object keys removed at any depth: personal or environment data the
    test does not need (npm packument readmes, maintainer e-mails, registry-internal host names)."""
    kept: list[tuple[str, str]] = []
    for name, value in headers:
        lname = name.lower()
        if lname not in RESPONSE_HEADERS:
            continue
        if lname == 'location':
            value = scrub_url(value)
        kept.append((lname, value))
    ctype = next((v for k, v in kept if k == 'content-type'), '')
    if body and 'json' in ctype.lower():
        body = _scrub_json_body(body, drop_keys)
        kept = [(k, str(len(body)) if k == 'content-length' else v) for k, v in kept]
    return Exchange(status, kept, body)


def _scrub_json_body(body: bytes, drop_keys: frozenset[str] = frozenset()) -> bytes:
    try:
        doc = json.loads(body)
    except ValueError:  # includes UnicodeDecodeError
        return body
    changed = False

    def walk(node: object) -> object:
        nonlocal changed
        if isinstance(node, dict):
            out: dict[str, object] = {}
            for k, v in node.items():
                if k in drop_keys:
                    changed = True
                elif isinstance(v, str) and _SECRET_JSON_KEY.search(str(k)):
                    out[k] = REDACTED
                    changed = changed or v != REDACTED
                else:
                    out[k] = walk(v)
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    scrubbed = walk(doc)
    if not changed:
        return body  # keep the upstream bytes exactly when there was nothing to redact
    return json.dumps(scrubbed, separators=(',', ':')).encode()


class FixtureStore:
    """Read/write fixtures under ``root`` (one directory per upstream host)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _paths(self, key: RequestKey) -> tuple[Path, Path]:
        folder = self.root / key.host
        return folder / f'{key.stem}.json', folder / f'{key.stem}.body'

    def _legacy_paths(self, key: RequestKey) -> tuple[Path, Path]:
        """Names written before 2026-11-10 (``Path.with_suffix`` on the stem: for a slug with a dot, the text from its
        last dot, key included, was replaced, so ``x.pom`` and ``x.jar`` could share a file). Read only, and only
        when the recorded request is this one."""
        base = self.root / key.host / key.stem
        return base.with_suffix('.json'), base.with_suffix('.body')

    def _find(self, key: RequestKey) -> tuple[Path, Path, dict[str, Any]] | None:
        meta_path, body_path = self._paths(key)
        if meta_path.is_file():
            return meta_path, body_path, json.loads(meta_path.read_text())
        meta_path, body_path = self._legacy_paths(key)
        if not meta_path.is_file():
            return None
        meta = json.loads(meta_path.read_text())
        request = meta.get('request', {})
        recorded = (
            request.get('method'),
            request.get('url'),
            request.get('auth'),
            [tuple(h) for h in request.get('headers', [])],
            request.get('body_sha256'),
        )
        if recorded != (key.method, key.url, key.auth, list(key.headers), key.body_sha256):
            return None
        return meta_path, body_path, meta

    def load(self, key: RequestKey) -> Exchange | None:
        found = self._find(key)
        if found is None:
            return None
        _, body_path, meta = found
        body = body_path.read_bytes() if body_path.is_file() else b''
        expected = meta['response'].get('body_sha256')
        if expected and hashlib.sha256(body).hexdigest() != expected:
            raise ValueError(f'fixture body does not match its recorded sha256: {body_path}')
        headers = [(str(k), str(v)) for k, v in meta['response']['headers']]
        return Exchange(int(meta['response']['status']), headers, body)

    def save(self, key: RequestKey, exchange: Exchange) -> Path:
        meta_path, body_path = self._paths(key)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            'request': {
                'method': key.method,
                'url': key.url,
                'auth': key.auth,
                'headers': [list(h) for h in key.headers],
                'body_sha256': key.body_sha256,
            },
            'response': {
                'status': exchange.status,
                'headers': [list(h) for h in exchange.headers],
                'body_size': len(exchange.body),
                'body_sha256': hashlib.sha256(exchange.body).hexdigest() if exchange.body else None,
            },
        }
        if exchange.body:
            _atomic_write(body_path, exchange.body)
        elif body_path.exists():
            body_path.unlink()
        _atomic_write(meta_path, (json.dumps(meta, indent=2, sort_keys=True) + '\n').encode())
        return meta_path


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(data)
        Path(tmp).chmod(0o644)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
