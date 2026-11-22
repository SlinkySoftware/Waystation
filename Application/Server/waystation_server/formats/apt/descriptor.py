"""apt repository options, upstream probe and "Set me up" snippets (proxies: the distribution's own keyring in
``Signed-By``; hosted repositories, T-P5-APTH: the repository's own signing key, upload and delete with curl)."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from waystation_server.formats.base import FormatDescriptor, SetupClient, SetupContext, UiHints
from waystation_server.formats.common.setup import TOKEN, USERNAME, path_of, snippet
from waystation_server.repos.model import RepoType

SUITE = r'^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$'
HOSTED_SUITE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')  # hosted: one directory below dists/
COMPONENT = re.compile(r'^[a-z0-9][a-z0-9.+-]{0,31}$')
ARCHITECTURE = re.compile(r'^[a-z0-9][a-z0-9-]{0,31}$')
# The hosted repository's public signing key: armored (Signed-By with a .asc file) and binary (a .gpg keyring).
KEY_ASC = 'public.asc'
KEY_GPG = 'public.gpg'
# Signed-By: the distribution's own archive keyring (apt keeps verifying the upstream signature).
DEBIAN_KEYRING = '/usr/share/keyrings/debian-archive-keyring.gpg'
UBUNTU_KEYRING = '/usr/share/keyrings/ubuntu-archive-keyring.gpg'


class AptOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', title='AptOptions')

    distribution: str = Field(
        max_length=100,
        pattern=SUITE,
        description=(
            'Proxy: informational default suite, e.g. `trixie`. Hosted: the distribution the repository publishes '
            '(`dists/<distribution>/`; one directory, no `/`)'
        ),
    )
    flat: bool = Field(default=False, description='Flat repository layout (no `dists/`); proxy only')
    components: list[str] = Field(
        default_factory=lambda: ['main'],
        min_length=1,
        max_length=16,
        description='Hosted: the components uploads may go to (`?component=`, default the first); each is published',
    )
    architectures: list[str] = Field(
        default_factory=lambda: ['amd64', 'arm64'],
        min_length=1,
        max_length=16,
        description=(
            'Hosted: the architectures always published (`binary-<arch>`; `Architecture: all` packages are listed '
            'in each); architectures of uploaded packages are added'
        ),
    )

    @field_validator('components')
    @classmethod
    def _components(cls, value: list[str]) -> list[str]:
        for item in value:
            if not COMPONENT.match(item):
                raise ValueError(f'invalid component {item!r} (lower-case letters, digits, `.+-`)')
        if len(set(value)) != len(value):
            raise ValueError('components are listed twice')
        return value

    @field_validator('architectures')
    @classmethod
    def _architectures(cls, value: list[str]) -> list[str]:
        for item in value:
            if not ARCHITECTURE.match(item) or item == 'all':
                raise ValueError(f'invalid architecture {item!r} (e.g. amd64, arm64, armhf; not all)')
        if len(set(value)) != len(value):
            raise ValueError('architectures are listed twice')
        return value


def _check_options(options: BaseModel, repo_type: RepoType) -> list[tuple[str, str]]:
    assert isinstance(options, AptOptions)
    if repo_type is not RepoType.HOSTED:
        return []
    errors = []
    if not HOSTED_SUITE.match(options.distribution):
        errors.append(('distribution', 'a hosted distribution is one name (letters, digits, `._-`), no `/`'))
    if options.flat:
        errors.append(('flat', 'hosted repositories publish dists/<distribution>/ (no flat layout)'))
    return errors


def _probe(options: BaseModel) -> str:
    opts = options if isinstance(options, AptOptions) else AptOptions.model_validate(options.model_dump())
    if opts.flat:
        return 'Release'
    return f'dists/{opts.distribution}/Release'


def _hosted_snippets(ctx: SetupContext) -> list[SetupClient]:
    opts = AptOptions.model_validate(ctx.repo.format_options)
    name, url = ctx.repo.name, ctx.repo_url
    keyring = f'/etc/apt/keyrings/{name}.asc'
    components = ' '.join(opts.components)
    sample = 'mypackage_1.0-1_amd64.deb'
    snippets = [
        snippet(
            'apt-key',
            "Install the repository's signing key",
            f'sudo install -d -m 0755 /etc/apt/keyrings\n'
            f'curl -fsSL -u {USERNAME}:{TOKEN} {url}{KEY_ASC} | sudo tee {keyring} > /dev/null\n'
            f'gpg --show-keys {keyring}',
            description=(
                "Waystation signs InRelease and Release.gpg with this repository's own key. Compare the fingerprint "
                f'with the one shown in Waystation; a binary keyring is at {url}{KEY_GPG}.'
            ),
        ),
        snippet(
            'apt-sources',
            'deb822 source (Debian 12+, Ubuntu 24.04+)',
            f'Types: deb\nURIs: {url}\nSuites: {opts.distribution}\nComponents: {components}\nSigned-By: {keyring}\n',
            language='text',
            filename=f'/etc/apt/sources.list.d/{name}.sources',
        ),
        snippet(
            'apt-auth',
            'Credentials (an API token as the password)',
            f'machine {path_of(url).removeprefix("//")}\nlogin {USERNAME}\npassword {TOKEN}\n',
            language='text',
            filename=f'/etc/apt/auth.conf.d/{name}.conf',
            description='Only when anonymous reads are off. Keep the file chmod 600; apt sends it over https only '
            '(write the machine line with http:// for a plain-http URL).',
        ),
        snippet(
            'upload',
            'Upload a package',
            f'curl -fS -u {USERNAME}:{TOKEN} --upload-file {sample} {url}',
            description=(
                f'Uploads need the write permission. The package goes to component {opts.components[0]} (add '
                f'?component=<name> to the URL for another of: {components}); the indexes are rebuilt and signed '
                'within seconds.'
            ),
        ),
        snippet(
            'delete',
            'Delete a package',
            f'curl -fS -u {USERNAME}:{TOKEN} -X DELETE {url}pool/{opts.components[0]}/m/mypackage/{sample}',
            description='The path is the one the upload answered (Location); deletes need the delete permission.',
        ),
        snippet('apt-update', 'Then', 'sudo apt update && sudo apt install mypackage'),
    ]
    return [SetupClient(id='apt', label='apt', snippets=snippets)]


def _snippets(ctx: SetupContext) -> list[SetupClient]:
    if ctx.repo.type is RepoType.HOSTED:
        return _hosted_snippets(ctx)
    opts = AptOptions.model_validate(ctx.repo.format_options)
    url = ctx.repo_url
    suite = opts.distribution
    if opts.flat:
        deb822 = f'Types: deb\nURIs: {url}\nSuites: ./\nSigned-By: /etc/apt/keyrings/<vendor>.gpg\n'
        one_line = f'deb [signed-by=/etc/apt/keyrings/<vendor>.gpg] {url} ./'
    else:
        ubuntu = bool(re.search(r'ubuntu', ctx.repo.proxy.upstream_url if ctx.repo.proxy else ''))
        components = 'main restricted universe multiverse' if ubuntu else 'main'
        keyring = UBUNTU_KEYRING if ubuntu else DEBIAN_KEYRING
        # A release suite (trixie) usually comes with its -updates suite; security and other suites stand alone.
        suites = suite if '-' in suite else f'{suite} {suite}-updates'
        deb822 = f'Types: deb\nURIs: {url}\nSuites: {suites}\nComponents: {components}\nSigned-By: {keyring}\n'
        one_line = f'deb [signed-by={keyring}] {url} {suite} {components}'
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
    types=frozenset({RepoType.PROXY, RepoType.HOSTED}),
    options_model=AptOptions,
    ui=UiHints(icon='debian', browse='apt'),
    probe_path=_probe,
    setup_snippets=_snippets,
    check_options=_check_options,
    notes=(
        "Proxies pass the distribution's signed InRelease through: apt keeps verifying it with the distribution "
        'keyring.',
        'Hosted repositories: upload .deb packages; Waystation builds Packages and Release and signs them '
        "(InRelease, Release.gpg) with the repository's own key. Group repositories are not supported for apt.",
    ),
)
