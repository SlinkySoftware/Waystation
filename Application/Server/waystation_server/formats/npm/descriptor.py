"""npm repository options, upstream probe and "Set me up" snippets."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from waystation_server.formats.base import FormatDescriptor, SetupClient, SetupContext, UiHints
from waystation_server.formats.common.setup import TOKEN, path_of, snippet
from waystation_server.repos.model import RepoType


class NpmOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', title='NpmOptions')

    audit_passthrough: bool = Field(default=True, description='Proxy npm audit requests to the upstream')


def _probe(options: BaseModel) -> str:
    return '-/ping'


def _snippets(ctx: SetupContext) -> list[SetupClient]:
    url = ctx.repo_url
    auth = f'{path_of(url)}:_authToken={TOKEN}'
    npm = [
        snippet('npm-config', 'Use this registry', f'npm config set registry {url}'),
        snippet(
            'npmrc',
            'Or per project (.npmrc)',
            f'registry={url}\n# only when anonymous reads are not allowed:\n# {auth}\n',
            language='ini',
            filename='.npmrc',
        ),
    ]
    pnpm = [snippet('pnpm-config', 'Use this registry', f'pnpm config set registry {url}')]
    yarn = [
        snippet('yarn1', 'Yarn 1', f'yarn config set registry {url}'),
        snippet(
            'yarn-berry',
            'Yarn 2+ (.yarnrc.yml)',
            f'npmRegistryServer: "{url}"\n',
            language='yaml',
            filename='.yarnrc.yml',
        ),
    ]
    bun = [
        snippet('bunfig', 'bunfig.toml', f'[install]\nregistry = "{url}"\n', language='toml', filename='bunfig.toml')
    ]
    return [
        SetupClient(id='npm', label='npm', snippets=npm),
        SetupClient(id='pnpm', label='pnpm', snippets=pnpm),
        SetupClient(id='yarn', label='Yarn', snippets=yarn),
        SetupClient(id='bun', label='Bun', snippets=bun),
    ]


DESCRIPTOR = FormatDescriptor(
    name='npm',
    label='npm',
    types=frozenset({RepoType.PROXY}),
    options_model=NpmOptions,
    ui=UiHints(icon='npm', browse='npm'),
    probe_path=_probe,
    setup_snippets=_snippets,
)
