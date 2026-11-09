"""Go module repository options, upstream probe and "Set me up" snippets (proxy and group)."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from waystation_server.formats.base import FormatDescriptor, SetupClient, SetupContext, UiHints
from waystation_server.formats.common.setup import TOKEN, USERNAME, host_of, snippet
from waystation_server.repos.model import RepoType

DEFAULT_SUMDB = 'sum.golang.org'
DEFAULT_SUMDB_URL = 'https://sum.golang.org'
_SUMDB_NAME = r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'
_PATTERN = re.compile(r'^[A-Za-z0-9.*?\[\]_~/-]+$')

Pattern = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]


class GoOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', title='GoOptions')

    sumdb_proxy: bool = Field(
        default=True,
        description='Proxy the checksum database at `sumdb/<name>/` so clients verify modules without direct access '
        'to it (proxy repositories; groups use their first proxy member that does)',
    )
    sumdb_name: str = Field(
        default=DEFAULT_SUMDB,
        max_length=200,
        pattern=_SUMDB_NAME,
        description='Name of the checksum database (GOSUMDB)',
    )
    sumdb_url: str = Field(
        default=DEFAULT_SUMDB_URL,
        max_length=500,
        pattern=r'^https?://[^\s?#]+$',
        description='URL of the checksum database (the SSRF guard applies as for the upstream)',
    )
    verify_checksums: bool = Field(
        default=True,
        description='Verify `.mod` and `.zip` files against the checksum database before caching them '
        '(modules matching `nosumdb` are not looked up)',
    )
    nosumdb: list[Pattern] = Field(
        default_factory=list,
        max_length=100,
        description='Module path patterns never looked up in the checksum database (GONOSUMDB syntax: a glob per '
        'path prefix, e.g. `example.com/private/*`): private modules',
    )

    @field_validator('nosumdb')
    @classmethod
    def _patterns(cls, value: list[str]) -> list[str]:
        for pattern in value:
            if not _PATTERN.match(pattern) or pattern.startswith('/'):
                raise ValueError(f'invalid module path pattern: {pattern}')
        return value


def _proxy_url(ctx: SetupContext) -> str:
    return ctx.repo_url.rstrip('/')


def _snippets(ctx: SetupContext) -> list[SetupClient]:
    url = _proxy_url(ctx)
    host = host_of(ctx.base_url)
    https = ctx.base_url.startswith('https://')
    go = [
        snippet(
            'go-env',
            'Use this repository for every module download',
            f'go env -w GOPROXY={url}\n'
            '# checksums are still verified against sum.golang.org, through this repository (sumdb/ proxy)\n'
            'go mod download',
            description='Without ",direct" in GOPROXY every module comes through Waystation. The checksum database '
            'is reached through it too: the go command asks the proxy for sumdb/sum.golang.org/supported first.',
        ),
        snippet('go-once', 'One command', f'GOPROXY={url} go get example.com/some/module@latest'),
        snippet(
            'go-private',
            'Private modules (not in the public checksum database)',
            '# fetched through Waystation, not looked up in the checksum database:\n'
            'go env -w GONOSUMDB=example.com/private/*\n'
            '# GOPRIVATE=... would also bypass the proxy (GONOPROXY): use it only for modules fetched directly',
            description='Add the same patterns to the repository\'s "nosumdb" option so Waystation does not look '
            'them up either.',
        ),
    ]
    netrc_note = (
        'The go command sends .netrc credentials over HTTPS only (GOAUTH=netrc, the default). Use an API token as '
        'the password; NETRC points to another file.'
    )
    if not https:
        netrc_note += ' This base URL is plain HTTP: put Waystation behind TLS to use authentication.'
    auth = [
        snippet(
            'netrc',
            'Credentials (when anonymous reads are not allowed)',
            f'machine {host}\nlogin {USERNAME}\npassword {TOKEN}\n',
            language='text',
            filename='.netrc',
            description=f'~/.netrc (Linux/macOS) or %USERPROFILE%\\_netrc (Windows). {netrc_note}',
        ),
    ]
    return [
        SetupClient(id='go', label='go', snippets=go),
        SetupClient(id='go-auth', label='Authentication', snippets=auth),
    ]


def _check_options(options: BaseModel, repo_type: RepoType) -> list[tuple[str, str]]:
    return []


DESCRIPTOR = FormatDescriptor(
    name='go',
    label='Go modules',
    types=frozenset({RepoType.PROXY, RepoType.GROUP}),
    options_model=GoOptions,
    ui=UiHints(icon='go', browse='go'),
    setup_snippets=_snippets,
    check_options=_check_options,
    notes=('Go has no hosted repositories: publish modules by tagging them in version control.',),
)
