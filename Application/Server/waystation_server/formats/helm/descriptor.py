"""Helm (classic chart repositories) options, upstream probe and "Set me up" snippets."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from waystation_server.formats.base import FormatDescriptor, SetupClient, SetupContext, SetupSnippet, UiHints
from waystation_server.formats.common.setup import TOKEN, USERNAME, snippet
from waystation_server.formats.helm.index import INDEX_PATH
from waystation_server.repos.model import RepoType


class HelmOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', title='HelmOptions')


def _probe(options: BaseModel) -> str:
    return INDEX_PATH


def _use(ctx: SetupContext) -> list[SetupSnippet]:
    name, url = ctx.repo.name, ctx.repo_url
    return [
        snippet('helm-repo-add', 'Add the repository', f'helm repo add {name} {url}\nhelm repo update {name}'),
        snippet(
            'helm-repo-add-auth',
            'With credentials (when anonymous reads are not allowed)',
            f'helm repo add {name} {url} --username {USERNAME} --password {TOKEN}',
            description='An API token is the password. Helm sends it only to this host.',
        ),
        snippet('helm-search', 'List the charts', f'helm search repo {name}/ --versions'),
        snippet(
            'helm-install',
            'Install or download a chart',
            f'helm install my-release {name}/<chart> --version <version>\nhelm pull {name}/<chart> --version <version>',
        ),
    ]


def _push(ctx: SetupContext, *, into: str | None = None) -> list[SetupSnippet]:
    name, url = ctx.repo.name, ctx.repo_url
    context_path = urlsplit(url).path.rstrip('/')
    where = f' (stored in {into})' if into else ''
    return [
        snippet(
            'helm-package-push',
            f'Package and upload a chart{where}',
            f'helm package ./mychart\ncurl -fS -u {USERNAME}:{TOKEN} --upload-file mychart-0.1.0.tgz {url}',
            description='Uploads need the write permission. The index is rebuilt within seconds; then run '
            f'helm repo update {name}.',
        ),
        snippet(
            'helm-cm-push',
            f'Or with the helm-push plugin (ChartMuseum API){where}',
            'helm plugin install https://github.com/chartmuseum/helm-push\n'
            f'helm cm-push mychart-0.1.0.tgz {name} --context-path={context_path}',
            description='The repository must have been added with --username and --password.',
        ),
    ]


def _snippets(ctx: SetupContext) -> list[SetupClient]:
    repo = ctx.repo
    helm = _use(ctx)
    if repo.type is RepoType.HOSTED:
        helm += _push(ctx)
        helm.append(
            snippet(
                'helm-delete',
                'Delete a chart version',
                f'curl -fS -u {USERNAME}:{TOKEN} -X DELETE {ctx.repo_url}mychart-0.1.0.tgz',
            )
        )
    elif repo.type is RepoType.GROUP and repo.group is not None:
        helm[0] = snippet(
            'helm-repo-add',
            'Add the repository',
            f'helm repo add {repo.name} {ctx.repo_url}\nhelm repo update {repo.name}',
            description=f'One index for the members, in order: {", ".join(repo.group.members)}.',
        )
        if repo.group.writable_member:
            helm += _push(ctx, into=repo.group.writable_member)
    return [SetupClient(id='helm', label='Helm', snippets=helm)]


DESCRIPTOR = FormatDescriptor(
    name='helm',
    label='Helm',
    types=frozenset({RepoType.PROXY, RepoType.HOSTED, RepoType.GROUP}),
    writable_group_member=True,
    options_model=HelmOptions,
    ui=UiHints(icon='helm', browse='helm'),
    probe_path=_probe,
    setup_snippets=_snippets,
    notes=('OCI charts (helm push oci://...) are stored in docker repositories.',),
)
