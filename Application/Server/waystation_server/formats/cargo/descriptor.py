"""Cargo repository options, upstream probe, request actions and "Set me up" snippets (proxy, hosted, group)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from waystation_server.formats.base import FormatDescriptor, SetupClient, SetupContext, UiHints
from waystation_server.formats.cargo import prefetch
from waystation_server.formats.common.setup import TOKEN, on, powershell_env, snippet
from waystation_server.repos.model import RepoType
from waystation_server.security.actions import Action

REGISTRY = 'waystation'


class CargoOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', title='CargoOptions')


def _probe(options: BaseModel) -> str:
    return 'config.json'


def _request_action(method: str, path: str) -> Action | None:
    """Yank (a ``DELETE``) and owner changes modify the registry: ``write``, like publishing."""
    if path.startswith('api/v1/crates/') and method in ('DELETE', 'PUT'):
        return Action.WRITE
    return None


def _snippets(ctx: SetupContext) -> list[SetupClient]:
    index = f'sparse+{ctx.repo_url}index/'
    env = f'CARGO_REGISTRIES_{REGISTRY.upper()}_TOKEN'
    replace = snippet(
        'cargo-replace',
        'Use this repository instead of crates.io',
        f'[registries.{REGISTRY}]\nindex = "{index}"\n\n[source.crates-io]\nreplace-with = "{REGISTRY}"\n',
        language='toml',
        filename='~/.cargo/config.toml',
        description='Every crates.io dependency then comes through Waystation (source replacement).',
    )
    registry = snippet(
        'cargo-registry',
        'Use it as a named registry',
        f'[registries.{REGISTRY}]\nindex = "{index}"\n\n'
        f'# in Cargo.toml: my-crate = {{ version = "1", registry = "{REGISTRY}" }}\n',
        language='toml',
        filename='.cargo/config.toml',
    )
    providers = snippet(
        'cargo-credentials',
        'Let cargo send tokens (once per machine)',
        '[registry]\nglobal-credential-providers = ["cargo:token"]\n',
        language='toml',
        filename='~/.cargo/config.toml',
        description='Cargo talks to registries that ask for credentials only with a credential provider configured; '
        'cargo:token keeps tokens in ~/.cargo/credentials.toml (cargo:libsecret, cargo:macos-keychain and '
        'cargo:wincred use the system keyring).',
    )
    login = snippet(
        'cargo-login',
        'Credentials (private repositories and publishing)',
        f'cargo login --registry {REGISTRY}\n# paste: {TOKEN}\n# or, for one shell / CI:\nexport {env}={TOKEN}',
        description='The token is a Waystation API token (read scope to install, write to publish). Cargo sends it '
        'on every request once the repository asks for credentials.',
    )
    cargo = [
        replace,
        on('windows', replace, filename='%USERPROFILE%\\.cargo\\config.toml'),
        registry,
        providers,
        on('windows', providers, filename='%USERPROFILE%\\.cargo\\config.toml'),
        login,
        on('windows', login, content=f'cargo login --registry {REGISTRY}\n' + powershell_env([(env, TOKEN)])),
    ]
    clients = [SetupClient(id='cargo', label='Cargo', snippets=cargo)]
    repo = ctx.repo
    writable = repo.type is RepoType.HOSTED or (
        repo.type is RepoType.GROUP and repo.group is not None and bool(repo.group.writable_member)
    )
    if writable:
        publish = [
            snippet(
                'cargo-publish',
                'Publish',
                f'cargo publish --registry {REGISTRY}',
                description='Needs write permission on this repository. Add publish = ["waystation"] under [package] '
                'in Cargo.toml to keep the crate off crates.io.',
            ),
            snippet('cargo-yank', 'Yank a version', f'cargo yank --registry {REGISTRY} --version 1.0.0 my-crate'),
        ]
        clients.append(SetupClient(id='cargo-publish', label='cargo publish', snippets=publish))
    return clients


DESCRIPTOR = FormatDescriptor(
    name='cargo',
    label='Cargo',
    types=frozenset({RepoType.PROXY, RepoType.HOSTED, RepoType.GROUP}),
    options_model=CargoOptions,
    ui=UiHints(icon='cargo', browse='cargo'),
    probe_path=_probe,
    setup_snippets=_snippets,
    writable_group_member=True,
    request_action=_request_action,
    warm_item=prefetch.check_item,
    refresh_request=prefetch.refresh_request,
)
