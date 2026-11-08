"""Raw repository options and "Set me up" snippets."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from waystation_server.formats.base import FormatDescriptor, SetupClient, SetupContext, UiHints
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


def _snippets(ctx: SetupContext) -> list[SetupClient]:
    url = ctx.repo_url
    curl = [
        snippet('curl-get', 'Download a file', f'curl -fSLO {url}path/to/file'),
        snippet(
            'curl-auth',
            'With credentials (when anonymous reads are not allowed)',
            f'curl -fSLO -u {USERNAME}:{TOKEN} {url}path/to/file',
        ),
    ]
    wget = [snippet('wget-get', 'Download a file', f'wget {url}path/to/file')]
    return [SetupClient(id='curl', label='curl', snippets=curl), SetupClient(id='wget', label='wget', snippets=wget)]


DESCRIPTOR = FormatDescriptor(
    name='raw',
    label='Raw files',
    types=frozenset({RepoType.PROXY}),
    options_model=RawOptions,
    ui=UiHints(icon='raw', browse='tree'),
    setup_snippets=_snippets,
)
