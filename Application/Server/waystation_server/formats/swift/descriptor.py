"""Swift Package Registry repository options, upstream probe and "Set me up" snippets (SCM-backed proxy, hosted).

A proxy's upstream URL is a Git host (``https://github.com/``); package ``<scope>.<name>`` is the Git repository
``<upstream>/<owner>/<name>``, where the owner is the scope itself or what ``scopes`` maps it to.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from waystation_common.swift import valid_scope
from waystation_server.formats.base import FormatDescriptor, SetupClient, SetupContext, SetupSnippet, UiHints
from waystation_server.formats.common.setup import TOKEN, snippet
from waystation_server.repos.model import RepoType
from waystation_server.security.actions import Action

ArchiveLayout = Literal['github', 'gitlab', 'gitea']
MAX_SCOPES = 500
_OWNER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,99}){0,7}$')


class SwiftOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', title='SwiftOptions')

    archive_layout: ArchiveLayout = Field(
        default='github',
        description='How the Git host serves the source tarball of a tag (proxy repositories): `github` '
        '(`<repo>/archive/refs/tags/<tag>.tar.gz`), `gitlab` (`<repo>/-/archive/<tag>/<name>-<tag>.tar.gz`) or '
        '`gitea` (Gitea and Forgejo: `<repo>/archive/<tag>.tar.gz`)',
    )
    scopes: dict[str, str] = Field(
        default_factory=dict,
        description='Scope → owner path on the Git host (proxy repositories), e.g. `{"pointfree": "pointfreeco"}` or '
        'a GitLab group path `group/subgroup`. Scopes not listed are the owner name itself, unless '
        '`mapped_scopes_only` is set',
    )
    mapped_scopes_only: bool = Field(
        default=False,
        description='Serve only the scopes listed in `scopes` (an allowlist of owners on the Git host; proxy '
        'repositories)',
    )

    @field_validator('scopes')
    @classmethod
    def _scopes(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > MAX_SCOPES:
            raise ValueError(f'at most {MAX_SCOPES} scopes')
        out: dict[str, str] = {}
        for scope, owner in value.items():
            if not valid_scope(scope):
                raise ValueError(f'{scope[:60]!r} is not a valid package scope (letters, digits, inner hyphens)')
            owner = owner.strip().strip('/')
            if not _OWNER.match(owner):
                raise ValueError(f'{owner[:60]!r} is not an owner path on the Git host')
            if scope.lower() in out:
                raise ValueError(f'scope {scope!r} is listed twice (scopes are case-insensitive)')
            out[scope.lower()] = owner
        return out


def options_of(raw: BaseModel) -> SwiftOptions:
    return raw if isinstance(raw, SwiftOptions) else SwiftOptions()


def _check_options(options: BaseModel, repo_type: RepoType) -> list[tuple[str, str]]:
    if not isinstance(options, SwiftOptions) or repo_type is RepoType.PROXY:
        return []
    out: list[tuple[str, str]] = []
    if options.archive_layout != 'github':
        out.append(('archive_layout', 'only proxy repositories have a Git host'))
    if options.scopes:
        out.append(('scopes', 'only proxy repositories have a Git host'))
    if options.mapped_scopes_only:
        out.append(('mapped_scopes_only', 'only proxy repositories have a Git host'))
    return out


def _request_action(method: str, path: str) -> Action | None:
    """``swift package-registry login`` POSTs to ``login`` (or the repository URL itself): it only proves the
    credentials, so the router authorises it as a read; the plugin refuses anonymous callers."""
    if method == 'POST' and path in ('', 'login'):
        return Action.READ
    return None


def _set(ctx: SetupContext) -> SetupSnippet:
    url = ctx.repo_url
    return snippet(
        'swift-set',
        'Use this registry (per project, or --global for every project)',
        f'swift package-registry set {url}\n'
        '# or only for one scope\n'
        f'swift package-registry set --scope mona {url}\n'
        '# for every package on this machine\n'
        f'swift package-registry set --global {url}',
        description='Writes .swiftpm/configuration/registries.json (with --global: ~/.swiftpm/configuration/). '
        'Swift Package Manager talks to registries over HTTPS only.',
    )


def _login(ctx: SetupContext) -> SetupSnippet:
    return snippet(
        'swift-login',
        'Sign in with an API token (needed to publish, and to read when anonymous access is off)',
        f'swift package-registry login {ctx.repo_url} --token {TOKEN} --no-confirm',
        description='Swift sends the token as "Authorization: Bearer ...". On Linux it is stored in ~/.netrc; '
        '--username and --password sign in with a password instead.',
    )


def _resolve(ctx: SetupContext, *, proxy: bool) -> list[SetupSnippet]:
    dependency = snippet(
        'swift-dependency',
        'Depend on a package by its registry identity (Package.swift)',
        'dependencies: [\n    .package(id: "apple.swift-argument-parser", from: "1.5.0"),\n],',
        language='swift',
        filename='Package.swift',
        description='The identity is <scope>.<name>: for this proxy, the owner and the repository name on the Git host.'
        if proxy
        else 'The identity is <scope>.<name>, as published.',
    )
    resolve = snippet(
        'swift-resolve',
        'Resolve dependencies through the registry',
        'swift package resolve\n'
        '# keep url: dependencies and let the registry serve them where it knows the URL\n'
        'swift package resolve --replace-scm-with-registry',
        description='--replace-scm-with-registry asks the registry which identity a Git URL has (/identifiers) '
        'and downloads that package from the registry instead of cloning it.',
    )
    return [dependency, resolve]


def _publish(ctx: SetupContext) -> list[SetupSnippet]:
    url = ctx.repo_url
    return [
        snippet(
            'swift-publish',
            'Publish a release',
            f'swift package-registry publish mona.LinkedList 1.0.0 --url {url}\n'
            '# with release metadata (package-metadata.json in the package directory is picked up by default)\n'
            f'swift package-registry publish mona.LinkedList 1.0.1 --url {url} --metadata-path ./metadata.json',
            description='Publishing needs the write permission. Releases are immutable: a version cannot be '
            'published twice, nor again after it was removed.',
        ),
        snippet(
            'swift-unpublish',
            'Remove a release',
            f'curl -fS -X DELETE -H "Authorization: Bearer {TOKEN}" {url}mona/LinkedList/1.0.0',
            description='Needs the delete permission. Clients then see the release as gone (410); the version '
            'stays reserved.',
        ),
    ]


def _snippets(ctx: SetupContext) -> list[SetupClient]:
    items = [_set(ctx), _login(ctx), *_resolve(ctx, proxy=ctx.repo.type is RepoType.PROXY)]
    if ctx.repo.type is RepoType.HOSTED:
        items.extend(_publish(ctx))
    return [SetupClient(id='swiftpm', label='Swift Package Manager', snippets=items)]


DESCRIPTOR = FormatDescriptor(
    name='swift',
    label='Swift Package Registry',
    types=frozenset({RepoType.PROXY, RepoType.HOSTED}),
    options_model=SwiftOptions,
    ui=UiHints(icon='swift', browse='swift'),
    setup_snippets=_snippets,
    check_options=_check_options,
    request_action=_request_action,
    notes=(
        'Proxy repositories serve packages from a Git host: releases are its semantic-version tags, and each '
        "release's source archive is built once from the tag and pinned (its checksum never changes).",
    ),
)
