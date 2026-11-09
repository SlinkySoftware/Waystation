"""Docker repository options, upstream probe and "Set me up" snippets (proxy and hosted)."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from waystation_server.formats.base import FormatDescriptor, SetupClient, SetupContext, SetupSnippet, UiHints
from waystation_server.formats.common.setup import TOKEN, USERNAME, snippet
from waystation_server.repos.model import RepoType

_HOST = re.compile(
    r'^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*(:\d{1,5})?$'
)

Alias = Annotated[str, StringConstraints(max_length=253, strip_whitespace=True, to_lower=True)]


class DockerOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', title='DockerOptions')

    aliases: list[Alias] = Field(
        default_factory=list,
        max_length=20,
        description='Upstream aliases routed to this repo, e.g. `docker.io`, `ghcr.io` (SPEC §4.4)',
    )
    docker_hub: bool = Field(
        default=False, description='Upstream is Docker Hub: token auth, `library/` prefix, rate-limit tracking'
    )
    default_repo: bool = Field(
        default=False, description='Make this the default docker repository (mirror mode, unprefixed names)'
    )

    @field_validator('aliases')
    @classmethod
    def _aliases(cls, value: list[str]) -> list[str]:
        for alias in value:
            if not _HOST.fullmatch(alias):
                raise ValueError(f'{alias!r} is not a registry host name')
        if len(set(value)) != len(value):
            raise ValueError('aliases must be unique')
        return value


def _probe(options: BaseModel) -> str:
    return 'v2/'


def _check_options(options: BaseModel, repo_type: RepoType) -> list[tuple[str, str]]:
    assert isinstance(options, DockerOptions)
    errors: list[tuple[str, str]] = []
    if repo_type is not RepoType.PROXY:
        if options.aliases:
            errors.append(('aliases', 'upstream aliases are for proxy repositories'))
        if options.docker_hub:
            errors.append(('docker_hub', 'only a proxy repository can have Docker Hub as its upstream'))
    return errors


def _login(host: str) -> SetupSnippet:
    return snippet(
        'docker-login',
        'Sign in (an API token as the password)',
        f'echo "{TOKEN}" | docker login {host} --username {USERNAME} --password-stdin',
        description='Pushing needs write permission on this repository; use an API token with the write scope.',
    )


def _hosted_snippets(ctx: SetupContext) -> list[SetupClient]:
    host, name = ctx.registry_host, ctx.repo.name
    image = f'{host}/{name}/myapp'
    docker = [
        _login(host),
        snippet('docker-push', 'Tag and push an image', f'docker tag myapp:1.0 {image}:1.0\ndocker push {image}:1.0'),
        snippet(
            'docker-buildx',
            'Build and push a multi-platform image',
            f'docker buildx build --platform linux/amd64,linux/arm64 -t {image}:1.0 --push .',
        ),
        snippet('docker-pull', 'Pull', f'docker pull {image}:1.0'),
    ]
    podman = [
        snippet(
            'podman-push',
            'Push and pull with podman',
            f'podman login {host}\npodman push myapp:1.0 {image}:1.0\npodman pull {image}:1.0',
        )
    ]
    tools = [
        snippet(
            'crane-copy',
            'Copy an image from another registry (crane, skopeo)',
            f'crane copy registry.example.com/team/app:1.0 {image}:1.0\n'
            f'skopeo copy --all docker://registry.example.com/team/app:1.0 docker://{image}:1.0',
        ),
        snippet(
            'oras-push',
            'Push and pull an OCI artifact (oras)',
            f'oras push {image}-docs:1.0 README.md:text/markdown\noras pull {image}-docs:1.0',
        ),
        snippet(
            'cosign-sign',
            'Sign and verify an image (cosign; signatures are stored next to the image)',
            f'cosign sign --key cosign.key {image}@sha256:<digest>\ncosign verify --key cosign.pub {image}:1.0',
        ),
    ]
    return [
        SetupClient(id='docker', label='Docker', snippets=docker),
        SetupClient(id='podman', label='Podman', snippets=podman),
        SetupClient(id='oci-tools', label='crane, skopeo, oras, cosign', snippets=tools),
    ]


def _snippets(ctx: SetupContext) -> list[SetupClient]:
    if ctx.repo.type is RepoType.HOSTED:
        return _hosted_snippets(ctx)
    opts = DockerOptions.model_validate(ctx.repo.format_options)
    host, name = ctx.registry_host, ctx.repo.name
    image = 'library/nginx:latest' if opts.docker_hub else 'org/image:tag'
    lines = [f'docker pull {host}/{name}/{image}']
    lines += [f'docker pull {host}/{alias}/{image}' for alias in opts.aliases[:3]]
    docker = [
        snippet('docker-pull', 'Pull through this repository', '\n'.join(lines)),
        snippet(
            'docker-login',
            'Sign in (only needed when anonymous pulls are not allowed)',
            f'echo "{TOKEN}" | docker login {host} --username {USERNAME} --password-stdin',
        ),
    ]
    if opts.default_repo:
        docker.append(
            snippet(
                'docker-mirror',
                'Use as a Docker Hub mirror',
                f'{{\n  "registry-mirrors": ["{ctx.base_url}"]\n}}',
                language='json',
                filename='/etc/docker/daemon.json',
                description='Then restart the Docker daemon. Mirror mode only ever asks for Docker Hub images.',
            )
        )
    upstream_host = 'docker.io' if opts.docker_hub else (opts.aliases[0] if opts.aliases else 'registry.example.com')
    containerd = snippet(
        'containerd-hosts',
        f'Mirror {upstream_host} for containerd / nerdctl',
        f'server = "https://{upstream_host}"\n\n'
        f'[host."{ctx.base_url}/v2/{name}"]\n'
        f'  capabilities = ["pull", "resolve"]\n'
        f'  override_path = true\n',
        language='toml',
        filename=f'/etc/containerd/certs.d/{upstream_host}/hosts.toml',
    )
    podman = snippet(
        'podman-pull',
        'Pull with podman',
        f'podman pull {host}/{name}/{image}',
    )
    return [
        SetupClient(id='docker', label='Docker', snippets=docker),
        SetupClient(id='podman', label='Podman', snippets=[podman]),
        SetupClient(id='containerd', label='containerd', snippets=[containerd]),
    ]


DESCRIPTOR = FormatDescriptor(
    name='docker',
    label='Docker / OCI',
    types=frozenset({RepoType.PROXY, RepoType.HOSTED}),
    options_model=DockerOptions,
    ui=UiHints(icon='docker', browse='docker'),
    probe_path=_probe,
    setup_snippets=_snippets,
    check_options=_check_options,
)
