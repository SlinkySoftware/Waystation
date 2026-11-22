"""NuGet repository options, upstream probe, prefetch syntax and "Set me up" snippets (proxy, hosted, group)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from waystation_server.formats.base import (
    FormatDescriptor,
    RefreshRequest,
    SetupClient,
    SetupContext,
    SetupSnippet,
    UiHints,
)
from waystation_server.formats.common.setup import TOKEN, USERNAME, snippet
from waystation_server.formats.nuget import prefetch
from waystation_server.formats.nuget.protocol import FLAT, REGISTRATION, SERVICE_INDEX
from waystation_server.repos.model import RepoType


class NugetOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', title='NugetOptions')

    verify_catalog: bool = Field(
        default=True,
        description='Verify each package downloaded from the upstream against the SHA-512 hash and size its catalog '
        'entry publishes, when the upstream has a catalog (nuget.org does). Every package is also checked to be a '
        'readable NuGet package whose .nuspec names the requested id and version (proxy repositories)',
    )
    delete_mode: Literal['unlist', 'delete'] = Field(
        default='unlist',
        description='`dotnet nuget delete` on a hosted repository: `unlist` hides the version from search and version '
        'resolution but keeps it restorable (as nuget.org does); `delete` removes it',
    )


def _probe(options: BaseModel) -> str:
    return ''  # the upstream URL is the service index itself


def refresh_request(asset_key: str) -> RefreshRequest | None:
    """Version lists and registration indexes are asked for again as clients ask for them."""
    parts = asset_key.split('/')
    if asset_key.startswith(FLAT + '/') and len(parts) == 3 and parts[2] == 'index.json':
        return RefreshRequest(asset_key)
    if asset_key.startswith(REGISTRATION + '/') and len(parts) == 4 and parts[3] == 'index.json':
        return RefreshRequest(asset_key)
    return None


def _nuget_config(source: str, *, credentials: bool) -> str:
    creds = (
        '  <packageSourceCredentials>\n'
        '    <waystation>\n'
        f'      <add key="Username" value="{USERNAME}" />\n'
        f'      <add key="ClearTextPassword" value="{TOKEN}" />\n'
        '    </waystation>\n'
        '  </packageSourceCredentials>\n'
        if credentials
        else ''
    )
    secure = '' if source.startswith('https://') else ' allowInsecureConnections="true"'
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<configuration>\n'
        '  <packageSources>\n'
        '    <clear />\n'
        f'    <add key="waystation" value="{source}" protocolVersion="3"{secure} />\n'
        '  </packageSources>\n'
        f'{creds}'
        '</configuration>\n'
    )


def _push(source: str, *, into: str | None) -> tuple[list[SetupSnippet], list[SetupSnippet]]:
    where = f' (stored in {into})' if into else ''
    note = (
        'Pushing needs the write permission on this repository: use an API token with the write scope as the API key.'
    )
    dotnet = [
        snippet(
            'dotnet-push',
            f'Push a package{where}',
            f'dotnet pack -c Release\ndotnet nuget push bin/Release/MyLib.1.0.0.nupkg --source {source} '
            f'--api-key {TOKEN}',
            description=note,
        ),
        snippet(
            'dotnet-delete',
            'Unlist or delete a version',
            f'dotnet nuget delete MyLib 1.0.0 --source {source} --api-key {TOKEN} --non-interactive',
            description='The repository option "delete mode" decides whether the version is unlisted (default) or '
            'removed. Needs the delete permission.',
        ),
    ]
    nuget_exe = [
        snippet(
            'nuget-exe-push',
            f'Push a package{where}',
            f'nuget push MyLib.1.0.0.nupkg -Source {source} -ApiKey {TOKEN}',
            description=note,
        ),
    ]
    return dotnet, nuget_exe


def _snippets(ctx: SetupContext) -> list[SetupClient]:
    repo = ctx.repo
    source = f'{ctx.repo_url}{SERVICE_INDEX}'
    insecure = '' if source.startswith('https://') else ' --allow-insecure-connections'
    config = snippet(
        'nuget-config',
        'Use this repository for a solution (nuget.config)',
        _nuget_config(source, credentials=True),
        language='xml',
        filename='nuget.config',
        description='Put it next to the solution. Leave out packageSourceCredentials when anonymous reads are '
        'allowed; the password is an API token.',
    )
    dotnet = [
        snippet(
            'dotnet-source',
            'Add the source (user-wide)',
            f'dotnet nuget add source {source} --name waystation{insecure} '
            f'--username {USERNAME} --password {TOKEN} --store-password-in-clear-text',
            description='Drop --username, --password and --store-password-in-clear-text when anonymous reads are '
            'allowed. Plain-HTTP sources need --allow-insecure-connections (.NET 9 and later).',
        ),
        config,
        snippet('dotnet-add', 'Add a package to a project', 'dotnet add package Newtonsoft.Json\ndotnet restore'),
    ]
    nuget_exe = [
        snippet(
            'nuget-exe-source',
            'Add the source',
            f'nuget sources add -Name waystation -Source {source} -Username {USERNAME} -Password {TOKEN}',
            description='NuGet.exe (Windows, or Mono elsewhere) uses the same NuGet v3 protocol.',
        ),
        snippet('nuget-exe-install', 'Install a package', 'nuget install Newtonsoft.Json -Source waystation'),
    ]
    writable = repo.type is RepoType.HOSTED or (
        repo.type is RepoType.GROUP and repo.group is not None and bool(repo.group.writable_member)
    )
    if writable:
        into = repo.group.writable_member if repo.type is RepoType.GROUP and repo.group is not None else None
        push_dotnet, push_exe = _push(source, into=into)
        dotnet += push_dotnet
        nuget_exe += push_exe
    if repo.type is RepoType.GROUP and repo.group is not None:
        dotnet[0] = dotnet[0].model_copy(
            update={'description': f'One source for the members, in order: {", ".join(repo.group.members)}.'}
        )
    return [
        SetupClient(id='dotnet', label='dotnet CLI', snippets=dotnet),
        SetupClient(id='nuget-exe', label='NuGet.exe', snippets=nuget_exe),
    ]


DESCRIPTOR = FormatDescriptor(
    name='nuget',
    label='NuGet',
    types=frozenset({RepoType.PROXY, RepoType.HOSTED, RepoType.GROUP}),
    options_model=NugetOptions,
    ui=UiHints(icon='nuget', browse='nuget'),
    probe_path=_probe,
    setup_snippets=_snippets,
    writable_group_member=True,
    warm_item=prefetch.check_item,
    refresh_request=refresh_request,
    notes=('The upstream URL is a NuGet v3 service index, e.g. https://api.nuget.org/v3/index.json.',),
)
