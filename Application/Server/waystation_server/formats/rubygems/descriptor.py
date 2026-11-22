"""RubyGems repository options, upstream probe, prefetch syntax and "Set me up" snippets (proxy, hosted, group)."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from waystation_server.formats.base import (
    FormatDescriptor,
    RefreshRequest,
    SetupClient,
    SetupContext,
    SetupSnippet,
    UiHints,
)
from waystation_server.formats.common.setup import TOKEN, USERNAME, snippet
from waystation_server.formats.rubygems import prefetch, protocol
from waystation_server.repos.model import RepoType


class RubygemsOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', title='RubygemsOptions')


def _probe(options: BaseModel) -> str:
    return protocol.NAMES


def refresh_request(asset_key: str) -> RefreshRequest | None:
    """``versions``, ``names`` and info files are asked for again by their paths."""
    if asset_key in (protocol.VERSIONS, protocol.NAMES):
        return RefreshRequest(asset_key)
    head, _, rest = asset_key.partition('/')
    if head == protocol.INFO and rest and '/' not in rest:
        return RefreshRequest(asset_key)
    return None


def _with_credentials(url: str) -> str:
    parts = urlsplit(url)
    return f'{parts.scheme}://{USERNAME}:{TOKEN}@{parts.netloc}{parts.path}'


def _snippets(ctx: SetupContext) -> list[SetupClient]:
    repo = ctx.repo
    url = ctx.repo_url
    host = url.rstrip('/')
    members = ''
    if repo.type is RepoType.GROUP and repo.group is not None:
        members = f' One source for the members, in order: {", ".join(repo.group.members)}.'
    gem: list[SetupSnippet] = [
        snippet(
            'gem-sources',
            'Use this repository with gem',
            f'gem sources --remove https://rubygems.org/\ngem sources --add {url}',
            description='Replaces rubygems.org as the source of gem install.' + members,
        ),
        snippet(
            'gem-sources-auth',
            'With credentials',
            f'gem sources --add {_with_credentials(url)}',
            description='An API token is the password; leave the credentials out when anonymous reads are allowed.',
        ),
        snippet('gem-install', 'Install a gem', f'gem install rake --clear-sources --source {url}'),
    ]
    bundler: list[SetupSnippet] = [
        snippet(
            'bundler-source',
            'Gemfile source',
            f"source '{url}'\n\ngem 'rake'\n",
            language='ruby',
            filename='Gemfile',
            description='Bundler uses the compact index (versions, info) of this repository.' + members,
        ),
        snippet(
            'bundler-mirror',
            'Or mirror rubygems.org for every project',
            f'bundle config set --global mirror.https://rubygems.org {url}',
            description='Keeps source "https://rubygems.org" in Gemfiles and fetches through this repository.',
        ),
        snippet(
            'bundler-auth',
            'Credentials',
            f'bundle config set --global {url} {USERNAME}:{TOKEN}',
            description='Or the environment variable BUNDLE_<HOST>__<PATH> with the same value.',
        ),
    ]
    clients = [
        SetupClient(id='gem', label='gem', snippets=gem),
        SetupClient(id='bundler', label='Bundler', snippets=bundler),
    ]
    if repo.type is RepoType.HOSTED:
        clients.append(
            SetupClient(
                id='gem-push',
                label='Publish',
                snippets=[
                    snippet(
                        'gem-credentials',
                        'API key (~/.gem/credentials)',
                        f"mkdir -p ~/.gem\nprintf ':waystation: %s\\n' '{TOKEN}' >> ~/.gem/credentials\n"
                        'chmod 600 ~/.gem/credentials',
                        description='An API token with the write permission (or set GEM_HOST_API_KEY).',
                    ),
                    snippet(
                        'gem-push',
                        'Push a gem',
                        f'gem build my_gem.gemspec\ngem push my_gem-1.0.0.gem --host {host} --key waystation',
                        description='The token is sent as the Authorization header. A gemspec may also set '
                        f"spec.metadata['allowed_push_host'] = '{host}'.",
                    ),
                    snippet(
                        'gem-yank',
                        'Yank a version',
                        f'gem yank my_gem --version 1.0.0 --host {host} --key waystation',
                        description='Removes the version from the index and its files. Needs the delete permission.',
                    ),
                ],
            )
        )
    return clients


DESCRIPTOR = FormatDescriptor(
    name='rubygems',
    label='RubyGems',
    types=frozenset({RepoType.PROXY, RepoType.HOSTED, RepoType.GROUP}),
    options_model=RubygemsOptions,
    ui=UiHints(icon='rubygems', browse='rubygems'),
    probe_path=_probe,
    setup_snippets=_snippets,
    warm_item=prefetch.check_item,
    refresh_request=refresh_request,
    notes=('The upstream URL is a RubyGems server with the compact index, e.g. https://rubygems.org.',),
)
