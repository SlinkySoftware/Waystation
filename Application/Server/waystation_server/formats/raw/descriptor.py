"""Raw repository options and "Set me up" snippets."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from waystation_server.formats.base import FormatDescriptor, SetupClient, SetupContext, SetupSnippet, UiHints
from waystation_server.formats.common.setup import TOKEN, USERNAME, snippet
from waystation_server.repos.model import RepoType


class RawOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', title='RawOptions')

    metadata_path_regex: str | None = Field(
        default=None, max_length=512, description='Paths matching this regex are mutable metadata (revalidated)'
    )
    content_disposition: Literal['inline', 'attachment'] = 'attachment'

    @field_validator('metadata_path_regex')
    @classmethod
    def _regex(cls, value: str | None) -> str | None:
        if value:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f'not a valid regular expression: {exc.msg}') from None
        return value or None


def _check_options(options: BaseModel, repo_type: RepoType) -> list[tuple[str, str]]:
    assert isinstance(options, RawOptions)
    if repo_type is not RepoType.PROXY and options.metadata_path_regex:
        return [('metadata_path_regex', 'only proxy repositories revalidate metadata from an upstream')]
    return []


def _download(url: str) -> list[SetupSnippet]:
    return [
        snippet('curl-get', 'Download a file', f'curl -fSLO {url}path/to/file'),
        snippet(
            'curl-auth',
            'With credentials (when anonymous reads are not allowed)',
            f'curl -fSLO -u {USERNAME}:{TOKEN} {url}path/to/file',
        ),
    ]


def _upload(url: str, *, into: str | None = None) -> list[SetupSnippet]:
    where = f' (stored in {into})' if into else ''
    return [
        snippet(
            'curl-put',
            f'Upload a file{where}',
            f'curl -fS -u {USERNAME}:{TOKEN} --upload-file app-1.0.tar.gz {url}releases/1.0/app-1.0.tar.gz',
            description='Uploads need the write permission; an API token is the password.',
        ),
        snippet(
            'curl-put-checksum',
            'Upload with a checksum the server verifies',
            f'curl -fS -u {USERNAME}:{TOKEN} \\\n'
            f'  -H "X-Checksum-Sha256: $(sha256sum app-1.0.tar.gz | cut -d\' \' -f1)" \\\n'
            f'  --upload-file app-1.0.tar.gz {url}releases/1.0/app-1.0.tar.gz',
            description='X-Checksum-Sha1 and X-Checksum-Md5 are verified too; a mismatch stores nothing.',
        ),
    ]


def _snippets(ctx: SetupContext) -> list[SetupClient]:
    url = ctx.repo_url
    repo = ctx.repo
    curl = _download(url)
    if repo.type is RepoType.HOSTED:
        curl += _upload(url)
        curl += [
            snippet('curl-delete', 'Delete a file', f'curl -fS -u {USERNAME}:{TOKEN} -X DELETE {url}path/to/file'),
            snippet(
                'curl-list',
                'List a folder (JSON)',
                f'curl -fsS -H "Accept: application/json" {url}releases/',
                description='Folders answer an HTML index in a browser.',
            ),
        ]
    elif repo.type is RepoType.GROUP and repo.group is not None:
        members = ', '.join(repo.group.members)
        curl[0] = snippet(
            'curl-get', 'Download a file', f'curl -fSLO {url}path/to/file', description=f'Members in order: {members}.'
        )
        if repo.group.writable_member:
            curl += _upload(url, into=repo.group.writable_member)
    wget = [snippet('wget-get', 'Download a file', f'wget {url}path/to/file')]
    if repo.type is not RepoType.PROXY:
        wget.append(
            snippet('wget-auth', 'With credentials', f'wget --header "Authorization: Bearer {TOKEN}" {url}path/to/file')
        )
    return [SetupClient(id='curl', label='curl', snippets=curl), SetupClient(id='wget', label='wget', snippets=wget)]


DESCRIPTOR = FormatDescriptor(
    name='raw',
    label='Raw files',
    types=frozenset({RepoType.PROXY, RepoType.HOSTED, RepoType.GROUP}),
    writable_group_member=True,
    options_model=RawOptions,
    ui=UiHints(icon='raw', browse='tree'),
    setup_snippets=_snippets,
    check_options=_check_options,
)
