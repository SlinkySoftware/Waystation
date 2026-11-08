"""apt repository options, upstream probe and "Set me up" snippets."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from waystation_server.formats.base import FormatDescriptor, SetupClient, SetupContext, UiHints
from waystation_server.formats.common.setup import snippet
from waystation_server.repos.model import RepoType

SUITE = r'^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$'


class AptOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', title='AptOptions')

    distribution: str = Field(max_length=100, pattern=SUITE, description='Informational default suite, e.g. `trixie`')
    flat: bool = Field(default=False, description='Flat repository layout (no `dists/`)')


def _probe(options: BaseModel) -> str:
    opts = options if isinstance(options, AptOptions) else AptOptions.model_validate(options.model_dump())
    if opts.flat:
        return 'Release'
    return f'dists/{opts.distribution}/Release'


def _snippets(ctx: SetupContext) -> list[SetupClient]:
    opts = AptOptions.model_validate(ctx.repo.format_options)
    url = ctx.repo_url
    suite = opts.distribution
    if opts.flat:
        deb822 = f'Types: deb\nURIs: {url}\nSuites: ./\nSigned-By: /etc/apt/keyrings/<vendor>.gpg\n'
        one_line = f'deb [signed-by=/etc/apt/keyrings/<vendor>.gpg] {url} ./'
    else:
        components = (
            'main'
            if not re.search(r'ubuntu', ctx.repo.proxy.upstream_url if ctx.repo.proxy else '')
            else ('main restricted universe multiverse')
        )
        deb822 = (
            f'Types: deb\nURIs: {url}\nSuites: {suite} {suite}-updates\nComponents: {components}\n'
            f'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\n'
        )
        one_line = f'deb {url} {suite} {components}'
    snippets = [
        snippet(
            'apt-sources',
            'deb822 source (Debian 12+, Ubuntu 24.04+)',
            deb822,
            language='text',
            filename=f'/etc/apt/sources.list.d/{ctx.repo.name}.sources',
            description=(
                'Use the keyring of the distribution you proxy in Signed-By (apt verifies the upstream signature).'
            ),
        ),
        snippet(
            'apt-list',
            'One-line source',
            one_line + '\n',
            language='text',
            filename=f'/etc/apt/sources.list.d/{ctx.repo.name}.list',
        ),
        snippet('apt-update', 'Then', 'sudo apt update'),
    ]
    return [SetupClient(id='apt', label='apt', snippets=snippets)]


DESCRIPTOR = FormatDescriptor(
    name='apt',
    label='apt (Debian, Ubuntu)',
    types=frozenset({RepoType.PROXY}),
    options_model=AptOptions,
    ui=UiHints(icon='debian', browse='apt'),
    probe_path=_probe,
    setup_snippets=_snippets,
)
