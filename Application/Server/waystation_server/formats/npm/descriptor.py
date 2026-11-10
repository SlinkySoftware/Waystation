"""npm repository options, upstream probe, request rules and "Set me up" snippets (proxy, hosted, group)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from waystation_server.formats.base import FormatDescriptor, SetupClient, SetupContext, SetupSnippet, UiHints
from waystation_server.formats.common.setup import TOKEN, path_of, snippet
from waystation_server.repos.model import RepoType
from waystation_server.security.actions import Action

AUDIT_PATHS = frozenset(
    {
        '-/npm/v1/security/advisories/bulk',  # npm 7+
        '-/npm/v1/security/audits/quick',  # npm 6 (and npm 7+ fallback)
        '-/npm/v1/security/audits',
    }
)
LOGIN_PREFIX = '-/user/org.couchdb.user:'
LOGOUT_PREFIX = '-/user/token/'
DIST_TAG_PREFIX = '-/package/'


class NpmOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', title='NpmOptions')

    audit_passthrough: bool = Field(default=True, description='Proxy npm audit requests to the upstream')


def _probe(options: BaseModel) -> str:
    return '-/ping'


def client_login(method: str, path: str) -> bool:
    """``npm login --auth-type=legacy`` and ``npm logout``: the plugin signs the client in or out itself."""
    return (method == 'PUT' and path.startswith(LOGIN_PREFIX)) or (
        method == 'DELETE' and path.startswith(LOGOUT_PREFIX)
    )


def request_action(method: str, path: str) -> Action | None:
    """``npm dist-tag rm`` (DELETE ``-/package/<pkg>/dist-tags/<tag>``) changes a tag: ``write``, not ``delete``."""
    if method == 'DELETE' and path.startswith(DIST_TAG_PREFIX) and '/dist-tags/' in path:
        return Action.WRITE
    return None


def _install(ctx: SetupContext) -> list[SetupClient]:
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


def _publish(ctx: SetupContext) -> list[SetupClient]:
    url = ctx.repo_url
    note = 'Publishing needs write permission on this repository: use an API token with the write scope.'
    snippets: list[SetupSnippet] = [
        snippet(
            'npmrc-publish',
            'Authenticate (.npmrc)',
            f'{path_of(url)}:_authToken={TOKEN}\n',
            language='ini',
            filename='.npmrc',
            description=f"{note} Put it in ~/.npmrc or the project's .npmrc (never commit a token).",
        ),
        snippet(
            'npm-login',
            'Or sign in with your password (creates an API token)',
            f'npm login --auth-type=legacy --registry={url}',
            description='Stores a new API token in ~/.npmrc; it is listed (and can be revoked) on your profile. '
            'Accounts with two-factor sign-in use an API token instead.',
        ),
        snippet(
            'publish-config',
            'Publish here by default (package.json)',
            f'"publishConfig": {{\n  "registry": "{url}"\n}}\n',
            language='json',
            filename='package.json',
        ),
        snippet('npm-publish', 'Publish', f'npm publish --registry {url}'),
        snippet(
            'npm-manage',
            'Tags, deprecation, unpublish',
            f'npm dist-tag add my-package@1.0.0 beta --registry {url}\n'
            f'npm deprecate my-package@1.0.0 "use 2.x" --registry {url}\n'
            f'npm unpublish my-package@1.0.0 --registry {url}',
            description='Unpublishing needs delete permission.',
        ),
    ]
    pnpm = [snippet('pnpm-publish', 'Publish with pnpm', f'pnpm publish --registry {url} --no-git-checks')]
    return [
        SetupClient(id='npm-publish', label='npm publish', snippets=snippets),
        SetupClient(id='pnpm-publish', label='pnpm publish', snippets=pnpm),
    ]


def _snippets(ctx: SetupContext) -> list[SetupClient]:
    clients = _install(ctx)
    repo = ctx.repo
    writable = repo.type is RepoType.HOSTED or (
        repo.type is RepoType.GROUP and repo.group is not None and bool(repo.group.writable_member)
    )
    return clients + _publish(ctx) if writable else clients


DESCRIPTOR = FormatDescriptor(
    name='npm',
    label='npm',
    types=frozenset({RepoType.PROXY, RepoType.HOSTED, RepoType.GROUP}),
    options_model=NpmOptions,
    ui=UiHints(icon='npm', browse='npm'),
    probe_path=_probe,
    setup_snippets=_snippets,
    read_posts=AUDIT_PATHS,  # audit POSTs only query advisories: authorised as reads
    writable_group_member=True,
    client_login=client_login,
    request_action=request_action,
)
