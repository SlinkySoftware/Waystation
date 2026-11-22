"""Composer repository options, upstream probe, prefetch syntax and "Set me up" snippets (proxy, hosted, group)."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from waystation_server.formats.base import (
    FormatDescriptor,
    RefreshRequest,
    SetupClient,
    SetupContext,
    SetupSnippet,
    UiHints,
)
from waystation_server.formats.common.setup import TOKEN, USERNAME, host_of, snippet
from waystation_server.formats.composer import prefetch
from waystation_server.formats.composer.protocol import P2, ROOT
from waystation_server.repos.model import RepoType

DEFAULT_DIST_HOSTS = ('github.com', 'codeload.github.com', 'api.github.com', 'gitlab.com')
_HOSTNAME = re.compile(
    r'^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$'
)

Host = Annotated[str, StringConstraints(min_length=1, max_length=253, strip_whitespace=True, to_lower=True)]


class ComposerOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', title='ComposerOptions')

    dist_hosts: list[Host] = Field(
        default_factory=lambda: list(DEFAULT_DIST_HOSTS),
        max_length=50,
        description="Hosts the proxy fetches and caches package archives (dist URLs) from; the upstream URL's host "
        'is always allowed too. Dist URLs on other hosts are left unchanged (the client fetches them itself). Proxy '
        'repositories only',
    )
    source_mode: Literal['keep', 'strip'] = Field(
        default='keep',
        description="`keep` leaves each version's `source` (VCS) entry in the metadata; `strip` removes it so "
        'clients can only install the cached archives (dev branches without an archive become uninstallable). Proxy '
        'repositories only',
    )

    @field_validator('dist_hosts')
    @classmethod
    def _hosts(cls, value: list[str]) -> list[str]:
        for host in value:
            if not _HOSTNAME.match(host):
                raise ValueError(f'invalid host name: {host}')
        return list(dict.fromkeys(value))


def _check_options(options: BaseModel, repo_type: RepoType) -> list[tuple[str, str]]:
    if not isinstance(options, ComposerOptions) or repo_type is RepoType.PROXY:
        return []
    out: list[tuple[str, str]] = []
    if options.dist_hosts != list(DEFAULT_DIST_HOSTS):
        out.append(('dist_hosts', 'only proxy repositories fetch archives'))
    if options.source_mode != 'keep':
        out.append(('source_mode', 'only proxy repositories rewrite upstream metadata'))
    return out


def _probe(options: BaseModel) -> str:
    return ROOT


def refresh_request(asset_key: str) -> RefreshRequest | None:
    """``p2`` documents (tagged and ``~dev``) are asked for again by their paths."""
    parts = asset_key.split('/')
    if len(parts) == 3 and parts[0] == P2 and parts[2].endswith('.json'):
        return RefreshRequest(asset_key)
    return None


def _snippets(ctx: SetupContext) -> list[SetupClient]:
    repo = ctx.repo
    url = ctx.repo_url
    host = host_of(ctx.base_url)
    use = snippet(
        'composer-repo',
        'Use this repository in a project',
        f'composer config repositories.waystation composer {url}\ncomposer config repositories.packagist.org false',
        description='The second line stops Composer asking packagist.org directly.',
    )
    if repo.type is RepoType.GROUP and repo.group is not None:
        use = use.model_copy(
            update={'description': f'One repository for the members, in order: {", ".join(repo.group.members)}.'}
        )
    snippets: list[SetupSnippet] = [use]
    if not url.startswith('https://'):
        snippets.append(
            snippet(
                'composer-secure-http',
                'Allow plain HTTP',
                'composer config secure-http false',
                description='Composer refuses http:// repositories unless secure-http is off (prefer HTTPS).',
            )
        )
    snippets += [
        snippet(
            'composer-auth',
            'Credentials (auth.json)',
            f'composer config http-basic.{host} {USERNAME} {TOKEN}',
            description='An API token is the password; leave it out when anonymous reads are allowed. Add --global '
            'to keep it out of the project.',
        ),
        snippet(
            'composer-json',
            'Or in composer.json',
            '{\n  "repositories": [\n'
            f'    {{"type": "composer", "url": "{url}"}},\n'
            '    {"packagist.org": false}\n  ]\n}\n',
            language='json',
            filename='composer.json',
        ),
        snippet('composer-require', 'Add a package', 'composer require monolog/monolog'),
    ]
    if repo.type is RepoType.HOSTED:
        snippets += [
            snippet(
                'composer-upload',
                'Upload a package',
                "cd my-package && zip -r ../acme-demo-1.0.0.zip . -x '.git/*' 'vendor/*'\n"
                f'curl -u {USERNAME}:{TOKEN} --upload-file ../acme-demo-1.0.0.zip {url}packages/upload/acme/demo/1.0.0',
                description='The zip holds composer.json at its root (or in one top-level folder); its name must be '
                'acme/demo. Needs the write permission.',
            ),
            snippet(
                'composer-delete',
                'Delete a version',
                f'curl -u {USERNAME}:{TOKEN} -X DELETE {url}packages/upload/acme/demo/1.0.0',
                description='Needs the delete permission.',
            ),
        ]
    return [SetupClient(id='composer', label='Composer', snippets=snippets)]


DESCRIPTOR = FormatDescriptor(
    name='composer',
    label='Composer',
    types=frozenset({RepoType.PROXY, RepoType.HOSTED, RepoType.GROUP}),
    options_model=ComposerOptions,
    ui=UiHints(icon='composer', browse='composer'),
    probe_path=_probe,
    setup_snippets=_snippets,
    check_options=_check_options,
    warm_item=prefetch.check_item,
    refresh_request=refresh_request,
    notes=('The upstream URL is a Composer 2 repository, e.g. https://repo.packagist.org.',),
)
