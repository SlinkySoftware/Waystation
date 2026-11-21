"""Swift Package Registry wire format (SE-0292 registry specification): API version negotiation, problem details,
release lists, release information, ``Link`` headers and source repository URL normalisation.

Every response carries ``Content-Version: 1``. Errors are RFC 7807 problem details
(``application/problem+json``, ``Content-Language: en``).
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit

from starlette.responses import Response

API_VERSION = '1'
VERSION_HEADERS = {'Content-Version': API_VERSION}
JSON_TYPE = 'application/json'
PROBLEM_TYPE = 'application/problem+json'
ZIP_TYPE = 'application/zip'
SWIFT_TYPE = 'text/x-swift'
SIGNATURE_FORMATS = frozenset({'cms-1.0.0'})
_ACCEPT = re.compile(r'^application/vnd\.swift\.registry(?:\.v([^+]*))?(?:\+(json|zip|swift))?$')
TITLES = {
    400: 'Bad Request',
    401: 'Unauthorized',
    403: 'Forbidden',
    404: 'Not Found',
    405: 'Method Not Allowed',
    409: 'Conflict',
    410: 'Gone',
    413: 'Content Too Large',
    415: 'Unsupported Media Type',
    422: 'Unprocessable Content',
    502: 'Bad Gateway',
    504: 'Gateway Timeout',
    507: 'Insufficient Storage',
}


class Problem(Exception):  # noqa: N818 - an answer, not a failure
    """A registry error answered as problem details."""

    def __init__(self, status: int, detail: str, *, headers: Mapping[str, str] | None = None) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.headers = dict(headers or {})


def problem_doc(status: int, detail: str) -> dict[str, Any]:
    return {'type': 'about:blank', 'title': TITLES.get(status, 'Error'), 'status': status, 'detail': detail}


def problem(status: int, detail: str, *, headers: Mapping[str, str] | None = None) -> Response:
    body = json.dumps(problem_doc(status, detail)).encode()
    return Response(
        body,
        status_code=status,
        media_type=PROBLEM_TYPE,
        headers={**VERSION_HEADERS, 'Content-Language': 'en', **(headers or {})},
    )


def check_accept(value: str | None) -> None:
    """SE-0292 §3.5: an ``Accept`` naming the registry media type must ask for API version 1 (or none): an invalid
    version is ``400``, a valid but unsupported one ``415``. Other media types (``*/*``, ``application/json``...) are
    answered with version 1."""
    for item in (value or '').split(','):
        media = item.split(';', 1)[0].strip().lower()
        if not media.startswith('application/vnd.swift.registry'):
            continue
        match = _ACCEPT.match(media)
        if match is None:
            raise Problem(400, 'invalid API version')
        version = match.group(1)
        if version is None or version == API_VERSION:
            continue
        if not version.isdigit():
            raise Problem(400, 'invalid API version')
        raise Problem(415, 'unsupported API version')


def dump(doc: Mapping[str, Any]) -> bytes:
    return json.dumps(doc, separators=(',', ':'), ensure_ascii=False).encode()


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    when = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return when.astimezone(UTC).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


@dataclass(frozen=True)
class Urls:
    """URLs into this repository (absolute: clients follow them as given)."""

    base: str  # .../repository/<repo>/ (trailing slash)

    def package(self, scope: str, name: str) -> str:
        return f'{self.base}{quote(scope)}/{quote(name)}'

    def release(self, scope: str, name: str, version: str) -> str:
        return f'{self.package(scope, name)}/{quote(version, safe="+")}'

    def manifest(self, scope: str, name: str, version: str, swift_version: str | None = None) -> str:
        url = f'{self.release(scope, name, version)}/Package.swift'
        return f'{url}?swift-version={quote(swift_version)}' if swift_version else url


def link_header(links: Iterable[tuple[str, str, Mapping[str, str]]]) -> str:
    """``<url>; rel="x"; k="v"`` entries joined by ``, ``."""
    out = []
    for url, rel, params in links:
        extra = ''.join(f'; {k}="{v}"' for k, v in params.items())
        out.append(f'<{url}>; rel="{rel}"{extra}')
    return ', '.join(out)


def removed_problem(version: str) -> dict[str, Any]:
    return problem_doc(410, f'release {version} was removed from this registry')


def releases_doc(urls: Urls, scope: str, name: str, versions: Sequence[tuple[str, bool]]) -> dict[str, Any]:
    """``{"releases": {version: {"url", "problem"?}}}`` in the order given (highest precedence first); the flag says
    the release was removed."""
    out: dict[str, Any] = {}
    for version, removed in versions:
        entry: dict[str, Any] = {'url': urls.release(scope, name, version)}
        if removed:
            entry['problem'] = removed_problem(version)
        out[version] = entry
    return {'releases': out}


def neighbours(
    urls: Urls, scope: str, name: str, published: Sequence[str], version: str
) -> list[tuple[str, str, Mapping[str, str]]]:
    """``latest-version``, ``successor-version`` and ``predecessor-version`` links of ``version`` among the
    published versions (highest precedence first)."""
    links: list[tuple[str, str, Mapping[str, str]]] = []
    if published:
        links.append((urls.release(scope, name, published[0]), 'latest-version', {}))
    if version in published:
        index = published.index(version)
        if index > 0:
            links.append((urls.release(scope, name, published[index - 1]), 'successor-version', {}))
        if index + 1 < len(published):
            links.append((urls.release(scope, name, published[index + 1]), 'predecessor-version', {}))
    return links


def release_doc(record: Mapping[str, Any]) -> dict[str, Any]:
    """SE-0292 §4.2: ``id``, ``version``, ``resources`` (the source archive's checksum and signature), ``metadata``,
    ``publishedAt``."""
    resource: dict[str, Any] = {'name': 'source-archive', 'type': ZIP_TYPE, 'checksum': record['checksum']}
    signature = record.get('signature')
    if isinstance(signature, Mapping) and signature.get('base64'):
        resource['signing'] = {
            'signatureBase64Encoded': signature['base64'],
            'signatureFormat': signature.get('format') or 'cms-1.0.0',
        }
    metadata = record.get('metadata')
    doc: dict[str, Any] = {
        'id': f'{record["scope"]}.{record["name"]}',
        'version': record['version'],
        'resources': [resource],
        'metadata': dict(metadata) if isinstance(metadata, Mapping) else {},
    }
    published = iso(record.get('published_at'))
    if published:
        doc['publishedAt'] = published
    return doc


def digest_header(sha256_hex: str) -> str:
    return 'sha-256=' + base64.b64encode(bytes.fromhex(sha256_hex)).decode('ascii')


# ---------------------------------------------------------------------------- release metadata (SE-0292 appendix B)
MAX_METADATA = 256 * 1024
MAX_URLS = 50
MAX_URL = 2048
_STRING_FIELDS = ('description', 'licenseURL', 'readmeURL', 'originalPublicationTime')


def check_metadata(raw: str) -> dict[str, Any]:
    """The publisher's release metadata: a JSON object whose known fields have the schema's types (raises
    ``Problem(422)``)."""
    if len(raw.encode('utf-8')) > MAX_METADATA:
        raise Problem(413, f'release metadata is larger than {MAX_METADATA} bytes')
    if not raw.strip():
        return {}
    try:
        doc = json.loads(raw)
    except ValueError:
        raise Problem(422, 'invalid JSON provided for release metadata') from None
    if not isinstance(doc, dict):
        raise Problem(422, 'release metadata must be a JSON object')
    for key in _STRING_FIELDS:
        if key in doc and not isinstance(doc[key], str):
            raise Problem(422, f'release metadata: {key} must be a string')
    urls = doc.get('repositoryURLs')
    if urls is not None and (
        not isinstance(urls, list)
        or len(urls) > MAX_URLS
        or not all(isinstance(u, str) and 0 < len(u) <= MAX_URL for u in urls)
    ):
        raise Problem(422, f'release metadata: repositoryURLs must be a list of at most {MAX_URLS} URLs')
    author = doc.get('author')
    if author is not None and (not isinstance(author, dict) or not isinstance(author.get('name'), str)):
        raise Problem(422, 'release metadata: author must be an object with a name')
    if isinstance(author, dict):
        organization = author.get('organization')
        if organization is not None and (
            not isinstance(organization, dict) or not isinstance(organization.get('name'), str)
        ):
            raise Problem(422, 'release metadata: author.organization must be an object with a name')
    return doc


_SCP = re.compile(r'^(?:[A-Za-z0-9._-]+@)?([A-Za-z0-9.-]+):(?!//)(.+)$')


def url_key(url: str) -> str | None:
    """A source repository URL in the form ``/identifiers`` compares: ``host/path`` in lower case, without scheme,
    credentials, port, ``.git`` suffix or trailing slash. ``https://github.com/Mona/LinkedList.git``,
    ``git@github.com:mona/linkedlist.git`` and ``ssh://git@github.com/mona/LinkedList`` are the same."""
    parts = url_parts(url)
    return f'{parts[0]}/{parts[1].lower()}' if parts is not None else None


def url_parts(url: str) -> tuple[str, str] | None:
    """(lower-case host, path without slashes at the ends and without ``.git``, case kept) of a source repository
    URL (https, ssh, git or scp-like ``git@host:owner/repo.git``); None when it is not one."""
    url = url.strip()
    if not url or len(url) > MAX_URL or any(c in url for c in '\x00\r\n\t '):
        return None
    scp = _SCP.match(url) if '://' not in url else None
    if scp is not None:
        host, path = scp.group(1), scp.group(2)
    else:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ('http', 'https', 'ssh', 'git') or not parts.hostname:
            return None
        host, path = parts.hostname, parts.path
    path = path.strip('/').removesuffix('.git').strip('/')
    if not path or any(seg in ('', '.', '..') for seg in path.split('/')):
        return None
    return host.lower(), path
