"""Conda repository options, upstream probe and "Set me up" snippets (proxy and hosted; groups are not offered:
merging repodata of several channels is not supported)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from waystation_server.formats.base import FormatDescriptor, SetupClient, SetupContext, UiHints
from waystation_server.formats.common.setup import TOKEN, USERNAME, host_of, on, snippet
from waystation_server.formats.conda import prefetch
from waystation_server.repos.model import RepoType


class CondaOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', title='CondaOptions')


def _probe(options: BaseModel) -> str:
    return 'noarch/repodata.json'


def _snippets(ctx: SetupContext) -> list[SetupClient]:
    channel = ctx.repo_url.rstrip('/')
    base = f'{ctx.base_url}/repository'
    name = ctx.repo.name
    host = host_of(ctx.base_url)
    condarc = snippet(
        'conda-channels',
        'Use this channel',
        f'channels:\n  - {channel}\nchannel_priority: strict\nshow_channel_urls: true\n',
        language='yaml',
        filename='~/.condarc',
        description='conda, mamba and micromamba read ~/.condarc. Remove other channels (defaults) to get everything '
        'through Waystation.',
    )
    custom = snippet(
        'conda-custom-channels',
        'Keep the channel name',
        f'custom_channels:\n  {name}: {base}\nchannels:\n  - {name}\n',
        language='yaml',
        filename='~/.condarc',
        description=f'"-c {name}" and environment files that name the channel "{name}" then resolve to Waystation '
        '(name the repository after the upstream channel, e.g. conda-forge).',
    )
    once = snippet(
        'conda-install',
        'Install from it once',
        f'conda install --override-channels -c {channel} numpy',
    )
    netrc = snippet(
        'conda-netrc',
        'Credentials (repositories that need them)',
        f'machine {host}\n  login {USERNAME}\n  password {TOKEN}\n',
        language='text',
        filename='~/.netrc',
        description='conda sends ~/.netrc credentials for the host (chmod 600). The password is a Waystation API '
        'token. Credentials in the channel URL (https://user:token@host/...) work too.',
    )
    conda = [
        condarc,
        on('windows', condarc, filename='%USERPROFILE%\\.condarc'),
        custom,
        once,
        netrc,
        on('windows', netrc, filename='%USERPROFILE%\\_netrc'),
    ]
    mamba = [
        snippet(
            'micromamba-install',
            'Install',
            f'micromamba create -n demo --override-channels -c {channel} numpy',
            description='micromamba and mamba also read the channels of ~/.condarc (Conda tab).',
        ),
        snippet(
            'micromamba-auth',
            'Credentials',
            f'micromamba create -n demo --override-channels \\\n  -c https://{USERNAME}:{TOKEN}@{host}'
            f'/repository/{name} numpy',
            description='Credentials in the channel URL (a Waystation API token as the password); they are not '
            'stored in the environment.',
        ),
    ]
    pixi = [
        snippet(
            'pixi-channel',
            'Use this channel in a workspace',
            f'[workspace]\nchannels = ["{channel}"]\nplatforms = ["linux-64", "osx-arm64", "win-64"]\n',
            language='toml',
            filename='pixi.toml',
        ),
        snippet(
            'pixi-auth',
            'Credentials',
            f'pixi auth login {host} --token {TOKEN}',
            description='pixi then sends the API token as a bearer token to this host.',
        ),
    ]
    clients = [
        SetupClient(id='conda', label='conda', snippets=conda),
        SetupClient(id='micromamba', label='mamba / micromamba', snippets=mamba),
        SetupClient(id='pixi', label='pixi', snippets=pixi),
    ]
    if ctx.repo.type is RepoType.HOSTED:
        upload = [
            snippet(
                'conda-upload',
                'Upload a package',
                f'curl -fu {USERNAME}:{TOKEN} --upload-file my-pkg-1.0-0.conda {ctx.repo_url}noarch/',
                description="PUT <subdir>/<file>: the subdir must be the one in the package's info/index.json. "
                '.conda and .tar.bz2 packages; the repodata is regenerated within seconds.',
            ),
            snippet(
                'conda-delete',
                'Delete a package',
                f'curl -fu {USERNAME}:{TOKEN} -X DELETE {ctx.repo_url}noarch/my-pkg-1.0-0.conda',
            ),
        ]
        clients.append(SetupClient(id='conda-upload', label='Upload', snippets=upload))
    return clients


DESCRIPTOR = FormatDescriptor(
    name='conda',
    label='Conda',
    types=frozenset({RepoType.PROXY, RepoType.HOSTED}),
    options_model=CondaOptions,
    ui=UiHints(icon='conda', browse='conda'),
    probe_path=_probe,
    setup_snippets=_snippets,
    notes=('Group repositories are not supported: conda merges channels itself (list several channels).',),
    warm_item=prefetch.check_item,
    refresh_request=prefetch.refresh_request,
)
