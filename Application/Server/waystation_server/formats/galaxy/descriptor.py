"""Ansible Galaxy repository options, upstream probe and "Set me up" snippets (proxy, hosted, group)."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from waystation_server.formats.base import FormatDescriptor, SetupClient, SetupContext, SetupSnippet, UiHints
from waystation_server.formats.common.setup import TOKEN, snippet
from waystation_server.repos.model import RepoType

DEFAULT_COLLECTIONS_PATH = 'api/v3/plugin/ansible/content/published/collections/index/'
_PATH = re.compile(r'^(?:[A-Za-z0-9._~-]+/)*$')


class GalaxyOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', title='GalaxyOptions')

    collections_path: str = Field(
        default=DEFAULT_COLLECTIONS_PATH,
        max_length=200,
        description='Upstream path (relative to the upstream URL) under which `<namespace>/<name>/` collection '
        'documents live (proxy repositories). Galaxy NG and galaxy.ansible.com: '
        '`api/v3/plugin/ansible/content/published/collections/index/`; servers with the plain v3 layout: '
        '`api/v3/collections/`',
    )
    roles: bool = Field(
        default=True,
        description='Proxy the legacy v1 roles API read-only, role archives cached as content (proxy repositories)',
    )

    @field_validator('collections_path')
    @classmethod
    def _relative_path(cls, value: str) -> str:
        value = value.strip().lstrip('/')
        if value and not value.endswith('/'):
            value += '/'
        if not _PATH.match(value) or any(part in ('.', '..') for part in value.split('/')):
            raise ValueError('a relative path of letters, digits, ".", "_", "~" and "-" segments')
        return value


def upstream_root(upstream_url: str) -> str:
    """The upstream's API host root with a trailing slash: ``https://galaxy.ansible.com/`` also when the URL was given
    with its ``/api/`` suffix."""
    base = upstream_url.rstrip('/')
    if base.endswith('/api'):
        base = base[: -len('/api')]
    return base + '/'


def _probe(options: BaseModel) -> str:
    return 'api/'


def _check_options(options: BaseModel, repo_type: RepoType) -> list[tuple[str, str]]:
    if not isinstance(options, GalaxyOptions) or repo_type is RepoType.PROXY:
        return []
    out: list[tuple[str, str]] = []
    if options.collections_path != DEFAULT_COLLECTIONS_PATH:
        out.append(('collections_path', 'only proxy repositories have an upstream'))
    return out


def _cfg(ctx: SetupContext) -> SetupSnippet:
    return snippet(
        'ansible-cfg',
        'Use this repository for every ansible-galaxy command (ansible.cfg)',
        '[galaxy]\n'
        'server_list = waystation\n\n'
        '[galaxy_server.waystation]\n'
        f'url = {ctx.repo_url}\n'
        '# only when anonymous reads are not allowed (and to publish): an API token\n'
        f'token = {TOKEN}\n',
        language='ini',
        filename='ansible.cfg',
        description='In the project directory, ~/.ansible.cfg or the file ANSIBLE_CONFIG names. ansible-galaxy sends '
        'the token as "Authorization: Token ...".',
    )


def _install(ctx: SetupContext) -> list[SetupSnippet]:
    url = ctx.repo_url
    return [
        snippet(
            'galaxy-install',
            'Install a collection',
            f'ansible-galaxy collection install community.general -s {url}\n'
            '# a version range, or from a requirements file\n'
            f"ansible-galaxy collection install 'community.general:>=9.0.0,<10' -s {url}\n"
            f'ansible-galaxy collection install -r requirements.yml -s {url}',
        ),
        snippet(
            'galaxy-env',
            'Or with environment variables (CI)',
            'export ANSIBLE_GALAXY_SERVER_LIST=waystation\n'
            f'export ANSIBLE_GALAXY_SERVER_WAYSTATION_URL={url}\n'
            f'export ANSIBLE_GALAXY_SERVER_WAYSTATION_TOKEN={TOKEN}',
        ),
        snippet(
            'galaxy-requirements',
            'Per project (requirements.yml)',
            f'collections:\n  - name: community.general\n    version: ">=9.0.0"\n    source: {url}\n',
            language='yaml',
            filename='requirements.yml',
        ),
    ]


def _publish(ctx: SetupContext, *, into: str | None = None) -> SetupSnippet:
    where = f' (stored in {into})' if into else ''
    return snippet(
        'galaxy-publish',
        f'Build and publish a collection{where}',
        'ansible-galaxy collection build\n'
        f'ansible-galaxy collection publish ./my_namespace-my_collection-1.0.0.tar.gz -s {ctx.repo_url} --token {TOKEN}',
        description='Publishing needs the write permission. The import is checked within seconds '
        '(MANIFEST.json, FILES.json and every file checksum); versions cannot be replaced.',
    )


def _roles(ctx: SetupContext) -> SetupSnippet:
    return snippet(
        'galaxy-role-install',
        'Install a role (legacy roles API, read-only)',
        f'ansible-galaxy role install geerlingguy.docker -s {ctx.repo_url}',
        description='Role archives are fetched from the source the upstream names (usually GitHub tag archives) '
        'and cached unverified: the roles API publishes no checksums.',
    )


def _snippets(ctx: SetupContext) -> list[SetupClient]:
    repo = ctx.repo
    items = [_cfg(ctx), *_install(ctx)]
    if repo.type is RepoType.HOSTED:
        items.append(_publish(ctx))
        items.append(
            snippet(
                'galaxy-delete',
                'Delete a collection version',
                f'curl -fS -H "Authorization: Token {TOKEN}" -X DELETE '
                f'{ctx.repo_url}api/v3/collections/my_namespace/my_collection/versions/1.0.0/',
            )
        )
    elif repo.type is RepoType.GROUP and repo.group is not None:
        items[0] = _cfg(ctx).model_copy(
            update={'description': f'One server for the members, in order: {", ".join(repo.group.members)}.'}
        )
        if repo.group.writable_member:
            items.append(_publish(ctx, into=repo.group.writable_member))
        items.append(_roles(ctx))
    else:
        items.append(_roles(ctx))
    return [SetupClient(id='ansible-galaxy', label='ansible-galaxy', snippets=items)]


DESCRIPTOR = FormatDescriptor(
    name='galaxy',
    label='Ansible Galaxy',
    types=frozenset({RepoType.PROXY, RepoType.HOSTED, RepoType.GROUP}),
    writable_group_member=True,
    options_model=GalaxyOptions,
    ui=UiHints(icon='ansible', browse='galaxy'),
    probe_path=_probe,
    setup_snippets=_snippets,
    check_options=_check_options,
    notes=(
        'Collections use the Galaxy v3 API; roles are proxied read-only through the legacy v1 API (archives from '
        'the source the upstream names, unverified).',
    ),
)
