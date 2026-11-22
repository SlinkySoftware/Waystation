"""Alpine apk repository options, upstream probe, prefetch syntax and "Set me up" snippets (proxy, hosted).

``branch``, ``repositories`` and ``architectures`` only shape the snippets, "test upstream" and prefetch items
without a path: one proxy repository serves every branch, repository and architecture below its upstream URL
(``https://dl-cdn.alpinelinux.org/alpine/``), and a hosted repository every ``<branch>/<repository>/<arch>`` it gets
uploads for. Groups are not supported: apk indexes are signed per repository and cannot be merged.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from waystation_common import apk
from waystation_server.formats.apk import prefetch
from waystation_server.formats.base import FormatDescriptor, RefreshRequest, SetupClient, SetupContext, UiHints
from waystation_server.formats.common.setup import TOKEN, USERNAME, snippet
from waystation_server.repos.model import RepoType

SEGMENT = r'^[A-Za-z0-9._-]+$'
ARCH = r'^[a-z0-9_]+$'
Segment = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=SEGMENT)]
Arch = Annotated[str, StringConstraints(min_length=1, max_length=32, pattern=ARCH)]


class ApkOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', title='ApkOptions')

    branch: str = Field(
        default='latest-stable',
        min_length=1,
        max_length=64,
        pattern=SEGMENT,
        description=(
            'Alpine branch for "Set me up", prefetch items without a path and "test upstream" (e.g. `v3.22`, '
            '`edge`, `latest-stable`)'
        ),
    )
    repositories: list[Segment] = Field(
        default_factory=lambda: ['main', 'community'],
        min_length=1,
        max_length=16,
        description='Alpine repositories (below the branch) for "Set me up" and prefetch items without a path',
    )
    architectures: list[Arch] = Field(
        default_factory=lambda: ['x86_64'],
        min_length=1,
        max_length=16,
        description='Architectures that prefetch items without a path warm (e.g. `x86_64`, `aarch64`)',
    )

    @field_validator('branch')
    @classmethod
    def _branch(cls, value: str) -> str:
        if value in ('.', '..'):
            raise ValueError('must be a branch name')
        return value

    @field_validator('repositories')
    @classmethod
    def _repositories(cls, value: list[str]) -> list[str]:
        if any(v in ('.', '..') for v in value):
            raise ValueError('must be repository names')
        return list(dict.fromkeys(value))


def options_of(raw: BaseModel | dict[str, object]) -> ApkOptions:
    if isinstance(raw, ApkOptions):
        return raw
    return ApkOptions.model_validate(raw.model_dump() if isinstance(raw, BaseModel) else raw)


def _probe(options: BaseModel) -> str:
    opts = options_of(options)
    return f'{opts.branch}/{opts.repositories[0]}/{opts.architectures[0]}/{apk.INDEX_NAME}'


def refresh_request(asset_key: str) -> RefreshRequest | None:
    """``APKINDEX.tar.gz`` files are asked for again by their paths."""
    return RefreshRequest(asset_key) if asset_key.endswith(f'/{apk.INDEX_NAME}') else None


def _snippets(ctx: SetupContext) -> list[SetupClient]:
    opts = options_of(ctx.repo.format_options)
    url = ctx.repo_url
    hosted = ctx.repo.type is RepoType.HOSTED
    lines = '\n'.join(f'{url}{opts.branch}/{repo}' for repo in opts.repositories)
    auth_url = url.replace('://', f'://{USERNAME}:{TOKEN}@', 1)
    snippets = [
        snippet(
            'repositories',
            'Repository list',
            lines + '\n',
            language='text',
            filename='/etc/apk/repositories',
            description=(
                'Replaces the mirror lines (apk picks the architecture itself). Change the branch to your Alpine '
                'release (cat /etc/alpine-release; v3.22, edge...).'
                if not hosted
                else 'Upload to <branch>/<repository>/<arch>/ paths below this repository; list each branch/'
                'repository you use.'
            ),
        ),
        snippet(
            'repositories-auth',
            'With credentials',
            f'{auth_url}{opts.branch}/{opts.repositories[0]}\n',
            language='text',
            filename='/etc/apk/repositories',
            description='Only when anonymous reads are off: an API token is the password (keep the file chmod 600).',
        ),
    ]
    if hosted:
        name = apk.key_name(ctx.repo.name, ctx.repo.id)
        snippets.append(
            snippet(
                'key',
                'Trust the signing key',
                f'wget -O /etc/apk/keys/{name} {url}{apk.KEYS_DIR}/{name}',
                description=(
                    "Waystation signs this repository's APKINDEX files with its own RSA key (published after the "
                    'first upload). Check the key before you trust it.'
                ),
            )
        )
        snippets.append(
            snippet(
                'upload',
                'Upload a package',
                f'curl -fS -u {USERNAME}:{TOKEN} --upload-file mypackage-1.0-r0.apk '
                f'{url}{opts.branch}/{opts.repositories[0]}/x86_64/mypackage-1.0-r0.apk',
                description='Needs the write permission. The package architecture must match the path (noarch fits '
                'every one); the APKINDEX is rebuilt and signed within seconds.',
            )
        )
        snippets.append(
            snippet(
                'delete',
                'Delete a package',
                f'curl -fS -u {USERNAME}:{TOKEN} -X DELETE '
                f'{url}{opts.branch}/{opts.repositories[0]}/x86_64/mypackage-1.0-r0.apk',
            )
        )
    snippets.append(snippet('install', 'Update and install', 'apk update && apk add curl'))
    return [SetupClient(id='apk', label='apk', snippets=snippets)]


DESCRIPTOR = FormatDescriptor(
    name='apk',
    label='Alpine apk',
    types=frozenset({RepoType.PROXY, RepoType.HOSTED}),
    options_model=ApkOptions,
    ui=UiHints(icon='alpine', browse='apk'),
    probe_path=_probe,
    setup_snippets=_snippets,
    warm_item=prefetch.check_item,
    refresh_request=refresh_request,
    notes=(
        'The upstream URL is an Alpine mirror root, e.g. https://dl-cdn.alpinelinux.org/alpine/.',
        'APKINDEX files are passed through untouched: apk keeps verifying them with the Alpine keys.',
        'Hosted repositories sign their APKINDEX files with their own RSA key. Group repositories are not supported '
        'for apk (signed indexes cannot be merged).',
    ),
)
