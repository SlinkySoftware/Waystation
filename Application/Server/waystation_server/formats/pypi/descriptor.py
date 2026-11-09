"""PyPI repository options, upstream probe and "Set me up" snippets (proxy, hosted, group)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from waystation_server.formats.base import FormatDescriptor, SetupClient, SetupContext, SetupSnippet, UiHints
from waystation_server.formats.common.setup import TOKEN, USERNAME, host_of, snippet
from waystation_server.repos.model import RepoType


class PypiOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', title='PypiOptions')

    json_api: bool = Field(
        default=True,
        description='Proxy the legacy JSON API (`/pypi/<project>/json`) to the upstream (proxy repositories)',
    )


def simple_root(upstream_url: str) -> str:
    """The upstream's simple API root: the URL itself when it ends in ``/simple`` (or ``/+simple``, devpi), else
    ``<url>/simple/`` (an index root such as ``https://pypi.org``, as in Nexus)."""
    base = upstream_url.rstrip('/')
    if base.endswith(('/simple', '/+simple')):
        return base + '/'
    return base + '/simple/'


def _check_options(options: BaseModel, repo_type: RepoType) -> list[tuple[str, str]]:
    return []


def _install(ctx: SetupContext) -> list[SetupClient]:
    index = f'{ctx.repo_url}simple/'
    host = host_of(ctx.base_url)
    scheme, _, rest = index.partition('://')
    with_auth = f'{scheme}://{USERNAME}:{TOKEN}@{rest}'
    pip = [
        snippet('pip-install', 'Install a package', f'pip install --index-url {index} requests'),
        snippet(
            'pip-conf',
            'Use this index for every pip command (pip.conf)',
            f'[global]\nindex-url = {index}\n'
            '# only when anonymous reads are not allowed (an API token as the password):\n'
            f'# index-url = {with_auth}\n',
            language='ini',
            filename='pip.conf',
            description='~/.config/pip/pip.conf (Linux/macOS) or %APPDATA%\\pip\\pip.ini (Windows); '
            f'add trusted-host = {host} when the base URL is plain HTTP.',
        ),
    ]
    uv = [
        snippet('uv-pip', 'uv pip', f'uv pip install --index-url {index} requests'),
        snippet(
            'uv-project',
            'Per project (pyproject.toml)',
            f'[[tool.uv.index]]\nname = "waystation"\nurl = "{index}"\ndefault = true\n',
            language='toml',
            filename='pyproject.toml',
        ),
        snippet(
            'uv-auth',
            'Credentials (when anonymous reads are not allowed)',
            f'export UV_INDEX_WAYSTATION_USERNAME={USERNAME}\nexport UV_INDEX_WAYSTATION_PASSWORD={TOKEN}',
        ),
    ]
    poetry = [
        snippet(
            'poetry-source',
            'Add this index as the primary source',
            f'poetry source add --priority=primary waystation {index}\n'
            f'poetry config http-basic.waystation {USERNAME} {TOKEN}',
        ),
    ]
    pdm = [
        snippet(
            'pdm-config',
            'Use this index',
            f'pdm config pypi.url {index}\npdm config pypi.username {USERNAME}\npdm config pypi.password {TOKEN}',
        ),
    ]
    return [
        SetupClient(id='pip', label='pip', snippets=pip),
        SetupClient(id='uv', label='uv', snippets=uv),
        SetupClient(id='poetry', label='Poetry', snippets=poetry),
        SetupClient(id='pdm', label='PDM', snippets=pdm),
    ]


def _publish(ctx: SetupContext) -> list[SetupClient]:
    url = ctx.repo_url
    note = 'Uploading needs write permission on this repository: use an API token with the write scope.'
    twine = [
        snippet(
            'pypirc',
            'Configure twine (~/.pypirc)',
            f'[distutils]\nindex-servers = waystation\n\n[waystation]\nrepository = {url}\n'
            f'username = {USERNAME}\npassword = {TOKEN}\n',
            language='ini',
            filename='.pypirc',
            description=note,
        ),
        snippet('twine-upload', 'Upload', 'twine upload --repository waystation dist/*'),
    ]
    uv: list[SetupSnippet] = [
        snippet(
            'uv-publish',
            'Publish with uv',
            f'uv publish --publish-url {url} --username {USERNAME} --password {TOKEN} dist/*',
            description=note,
        ),
    ]
    poetry = [
        snippet(
            'poetry-publish',
            'Publish with Poetry',
            f'poetry config repositories.waystation {url}\npoetry publish --build -r waystation '
            f'--username {USERNAME} --password {TOKEN}',
        ),
    ]
    return [
        SetupClient(id='twine', label='twine', snippets=twine),
        SetupClient(id='uv-publish', label='uv publish', snippets=uv),
        SetupClient(id='poetry-publish', label='Poetry publish', snippets=poetry),
    ]


def _snippets(ctx: SetupContext) -> list[SetupClient]:
    clients = _install(ctx)
    repo = ctx.repo
    writable = repo.type is RepoType.HOSTED or (
        repo.type is RepoType.GROUP and repo.group is not None and bool(repo.group.writable_member)
    )
    return clients + _publish(ctx) if writable else clients


DESCRIPTOR = FormatDescriptor(
    name='pypi',
    label='PyPI',
    types=frozenset({RepoType.PROXY, RepoType.HOSTED, RepoType.GROUP}),
    options_model=PypiOptions,
    ui=UiHints(icon='pypi', browse='pypi'),
    setup_snippets=_snippets,
    writable_group_member=True,
    check_options=_check_options,
)
