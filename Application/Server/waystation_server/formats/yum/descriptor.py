"""yum repository options, upstream probe and "Set me up" snippets (a ``.repo`` file for dnf/yum).

The snippets keep the distribution's ``$releasever``/``$basearch`` layout under the repository and its own GPG key
(``gpgcheck=1``): dnf keeps verifying every package against the distribution's signature. Well-known upstreams
(Rocky Linux, AlmaLinux, Oracle Linux, EPEL, Fedora) get one section per usual repository; anything else one section
at ``repo_path`` (default: the repository root holds ``repodata/``).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from waystation_common import openpgp
from waystation_common.errors import ValidationFailed
from waystation_server.formats.base import FormatDescriptor, SetupClient, SetupContext, UiHints
from waystation_server.formats.common.setup import TOKEN, USERNAME, snippet
from waystation_server.repos.model import RepoType

REPO_PATH = r'^[A-Za-z0-9$_.{}+/-]*$'
GPG_KEY = r'^(file|https?)://[^\s]+$'
VENDOR_KEY = 'file:///etc/pki/rpm-gpg/RPM-GPG-KEY-<vendor>'
REPOMD_KEYS_MAX = 64 * 1024


class YumOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', title='YumOptions')

    repo_path: str = Field(
        default='',
        max_length=200,
        pattern=REPO_PATH,
        description=(
            'Path of the yum repository (the directory holding `repodata/`) below the upstream URL, as used in the '
            '`.repo` `baseurl`, e.g. `$releasever/BaseOS/$basearch/os/`. Empty: derived for well-known upstreams, '
            'else the repository root. Only used by "Set me up" and "test upstream"'
        ),
    )
    gpg_key: str | None = Field(
        default=None,
        max_length=500,
        pattern=GPG_KEY,
        description='`gpgkey` for the "Set me up" `.repo` file. Empty: the distribution key for well-known upstreams',
    )
    repomd_keys: str | None = Field(
        default=None,
        max_length=REPOMD_KEYS_MAX,
        description=(
            "Armored OpenPGP public key block(s) of the upstream (e.g. the distribution's `RPM-GPG-KEY-*`). When "
            'set, a `repomd.xml` is cached only when its `repomd.xml.asc` verifies with one of these keys, and the '
            'verified `.asc` is served with it. Only for upstreams that sign `repomd.xml` (Rocky Linux, AlmaLinux); '
            'EPEL and Oracle Linux do not'
        ),
    )

    @field_validator('repo_path')
    @classmethod
    def _repo_path(cls, value: str) -> str:
        parts = [p for p in value.strip('/').split('/') if p not in ('', '.')]
        if '..' in parts:
            raise ValueError('must not contain `..`')
        return '/'.join(parts) + '/' if parts else ''

    @field_validator('repomd_keys')
    @classmethod
    def _repomd_keys(cls, value: str | None) -> str | None:
        text = (value or '').strip()
        if not text:
            return None
        try:
            openpgp.read_public_keys(text)
        except ValidationFailed as exc:
            raise ValueError(exc.detail) from None
        return text + '\n'


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    path: str  # below the repository URL, ends with '/'
    enabled: bool = True


@dataclass(frozen=True)
class Layout:
    name: str
    key: str
    sections: tuple[Section, ...]
    stock_ids: tuple[str, ...] = ()  # the distribution's own repository ids, to turn off


def _el(name: str, key: str) -> Layout:
    return Layout(
        name=name,
        key=key,
        sections=(
            Section('baseos', 'BaseOS', '$releasever/BaseOS/$basearch/os/'),
            Section('appstream', 'AppStream', '$releasever/AppStream/$basearch/os/'),
            Section('extras', 'Extras', '$releasever/extras/$basearch/os/'),
        ),
        stock_ids=('baseos', 'appstream', 'extras'),
    )


def layout(upstream_url: str) -> Layout | None:
    """The usual repositories under a well-known upstream URL (by host and path), else None."""
    parts = urlsplit(upstream_url)
    where = f'{parts.netloc.lower()}{parts.path.lower()}'
    if 'rockylinux' in where:
        return _el('Rocky Linux', 'file:///etc/pki/rpm-gpg/RPM-GPG-KEY-Rocky-$releasever')
    if 'almalinux' in where:
        return _el('AlmaLinux', 'file:///etc/pki/rpm-gpg/RPM-GPG-KEY-AlmaLinux-$releasever')
    if 'oracle' in where:
        base = '' if where.rstrip('/').endswith('/oraclelinux') else 'repo/OracleLinux/'
        return Layout(
            name='Oracle Linux',
            key='file:///etc/pki/rpm-gpg/RPM-GPG-KEY-oracle',
            sections=(
                Section('baseos', 'BaseOS Latest', f'{base}OL$releasever/baseos/latest/$basearch/'),
                Section('appstream', 'AppStream', f'{base}OL$releasever/appstream/$basearch/'),
            ),
            stock_ids=('ol9_baseos_latest', 'ol9_appstream'),
        )
    if '/epel' in where:
        return Layout(
            name='EPEL',
            key='file:///etc/pki/rpm-gpg/RPM-GPG-KEY-EPEL-$releasever',
            sections=(Section('epel', 'Everything', '$releasever/Everything/$basearch/'),),
            stock_ids=('epel',),
        )
    if 'fedora' in where:
        return Layout(
            name='Fedora',
            key='file:///etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-$releasever-$basearch',
            sections=(
                Section('fedora', 'Everything', 'releases/$releasever/Everything/$basearch/os/'),
                Section('updates', 'Updates', 'updates/$releasever/Everything/$basearch/'),
            ),
            stock_ids=('fedora', 'updates'),
        )
    return None


def _options(raw: BaseModel | dict[str, object]) -> YumOptions:
    if isinstance(raw, YumOptions):
        return raw
    return YumOptions.model_validate(raw.model_dump() if isinstance(raw, BaseModel) else raw)


def _probe(options: BaseModel) -> str:
    """A concrete ``repo_path``: its ``repomd.xml``. Otherwise (unset, or with dnf variables) the upstream URL
    itself: it is often the tree above the repositories (``.../pub/rocky/``), not a repository."""
    opts = _options(options)
    if not opts.repo_path or '$' in opts.repo_path:
        return ''
    return f'{opts.repo_path}repodata/repomd.xml'


def repo_file(ctx: SetupContext) -> tuple[str, Layout | None]:
    """The ``.repo`` file for the repository, and the well-known layout it follows (None: generic)."""
    opts = _options(ctx.repo.format_options)
    upstream = ctx.repo.proxy.upstream_url if ctx.repo.proxy else ''
    known = None if opts.repo_path else layout(upstream)
    sections = known.sections if known else (Section('repo', 'Packages', opts.repo_path),)
    name = known.name if known else ctx.repo.name
    key = opts.gpg_key or (known.key if known else VENDOR_KEY)
    blocks = []
    for section in sections:
        repo_id = f'{ctx.repo.name}-{section.id}' if known else ctx.repo.name
        title = f'{name} $releasever - {section.title}' if known else name
        blocks.append(
            f'[{repo_id}]\n'
            f'name={title} (Waystation)\n'
            f'baseurl={ctx.repo_url}{section.path}\n'
            f'enabled={1 if section.enabled else 0}\n'
            'gpgcheck=1\n'
            'repo_gpgcheck=0\n'
            f'gpgkey={key}\n'
            f'#username={USERNAME}\n'
            f'#password={TOKEN}\n'
        )
    return '\n'.join(blocks), known


def _snippets(ctx: SetupContext) -> list[SetupClient]:
    content, known = repo_file(ctx)
    description = (
        'Packages stay signed by the distribution: dnf verifies them with its own key (gpgcheck stays on; set '
        'repo_gpgcheck=1 to also verify repomd.xml). Uncomment the credentials only if anonymous reads are off.'
    )
    if not known:
        description += f' Replace {VENDOR_KEY} with the key of the repository you proxy.'
    snippets = [
        snippet(
            'repo',
            'Repository file',
            content,
            language='ini',
            filename=f'/etc/yum.repos.d/{ctx.repo.name}.repo',
            description=description,
        )
    ]
    if known and known.stock_ids:
        snippets.append(
            snippet(
                'disable',
                "Turn off the distribution's own mirrors",
                f'sudo dnf config-manager --set-disabled {" ".join(known.stock_ids)}',
                description='So dnf does not also fetch from the internet (mirrorlist/metalink are not proxied).',
            )
        )
    snippets.append(snippet('refresh', 'Refresh and install', 'sudo dnf makecache && sudo dnf upgrade'))
    return [SetupClient(id='dnf', label='dnf / yum', snippets=snippets)]


DESCRIPTOR = FormatDescriptor(
    name='yum',
    label='yum / dnf (RHEL, Rocky, AlmaLinux, Oracle, EPEL, Fedora)',
    types=frozenset({RepoType.PROXY}),
    options_model=YumOptions,
    ui=UiHints(icon='redhat'),
    probe_path=_probe,
    setup_snippets=_snippets,
    notes=(
        'Configure a concrete base URL as the upstream: mirrorlist and metalink URLs are not proxied.',
        'dnf keeps verifying package signatures (gpgcheck) with the distribution key; Waystation never re-signs.',
    ),
)
