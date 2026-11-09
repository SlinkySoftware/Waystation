"""Maven repository options, upstream probe and "Set me up" snippets (FORMATS.md "Maven")."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from waystation_server.formats.base import FormatDescriptor, SetupClient, SetupContext, UiHints
from waystation_server.formats.common.setup import TOKEN, USERNAME, snippet
from waystation_server.repos.model import RepoType

PREEMPTIVE = '-Daether.connector.http.preemptiveAuth=true'
VersionPolicy = Literal['release', 'snapshot', 'mixed']
LayoutPolicy = Literal['strict', 'permissive']


class MavenOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', title='MavenOptions')

    version_policy: VersionPolicy = Field(
        default='release',
        description='Which versions the repository holds: releases, SNAPSHOTs or both (groups: not used)',
    )
    layout_policy: LayoutPolicy = Field(
        default='strict',
        description='strict: only Maven layout paths (artifacts, checksums, signatures, maven-metadata.xml); '
        'permissive: any path',
    )


def _probe(options: BaseModel) -> str:
    # A tiny, always-present file of Maven Central and most mirrors: the parent of all Apache projects' metadata.
    return 'org/apache/apache/maven-metadata.xml'


def _server(repo: str) -> str:
    return (
        '<servers>\n'
        '  <server>\n'
        f'    <id>{repo}</id>\n'
        f'    <username>{USERNAME}</username>\n'
        f'    <password>{TOKEN}</password>\n'
        '  </server>\n'
        '</servers>\n'
    )


SNAPSHOT_PROFILE = (
    '  <!-- Maven Central (which the mirror replaces) has SNAPSHOTs turned off: turn them on -->\n'
    '  <profiles>\n'
    '    <profile>\n'
    '      <id>waystation</id>\n'
    '      <repositories>\n'
    '        <repository>\n'
    '          <id>central</id>\n'
    '          <url>http://central</url>\n'
    '          <snapshots><enabled>true</enabled></snapshots>\n'
    '        </repository>\n'
    '      </repositories>\n'
    '    </profile>\n'
    '  </profiles>\n'
    '  <activeProfiles><activeProfile>waystation</activeProfile></activeProfiles>\n'
)


def _settings(ctx: SetupContext, snapshots: bool) -> str:
    name = ctx.repo.name
    return (
        '<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0">\n'
        '  <mirrors>\n'
        '    <mirror>\n'
        f'      <id>{name}</id>\n'
        '      <mirrorOf>*</mirrorOf>\n'
        f'      <url>{ctx.repo_url}</url>\n'
        '    </mirror>\n'
        '  </mirrors>\n'
        '  <!-- only when anonymous reads are not allowed: your username and an API token -->\n'
        '  <servers>\n'
        '    <server>\n'
        f'      <id>{name}</id>\n'
        f'      <username>{USERNAME}</username>\n'
        f'      <password>{TOKEN}</password>\n'
        '    </server>\n'
        '  </servers>\n' + (SNAPSHOT_PROFILE if snapshots else '') + '</settings>\n'
    )


def _distribution(ctx: SetupContext, policy: VersionPolicy) -> str:
    name, url = ctx.repo.name, ctx.repo_url
    tags = {
        'release': ['repository'],
        'snapshot': ['snapshotRepository'],
        'mixed': ['repository', 'snapshotRepository'],
    }
    body = ''.join(f'  <{t}>\n    <id>{name}</id>\n    <url>{url}</url>\n  </{t}>\n' for t in tags[policy])
    return f'<distributionManagement>\n{body}</distributionManagement>\n'


def _gradle_read(ctx: SetupContext) -> str:
    return (
        'repositories {\n'
        '    maven {\n'
        f'        url = uri("{ctx.repo_url}")\n'
        '        // only when anonymous reads are not allowed:\n'
        '        // credentials {\n'
        '        //     username = providers.gradleProperty("wsUser").get()\n'
        '        //     password = providers.gradleProperty("wsToken").get()\n'
        '        //     // send them up front: Waystation answers 404, not a challenge, to anonymous requests\n'
        '        // authentication { create<BasicAuthentication>("basic") }\n'
        '        // }\n'
        '    }\n'
        '}\n'
    )


def _gradle_publish(ctx: SetupContext) -> str:
    return (
        'plugins {\n'
        '    `maven-publish`\n'
        '}\n\n'
        'publishing {\n'
        '    repositories {\n'
        '        maven {\n'
        f'            name = "{_identifier(ctx.repo.name)}"\n'
        f'            url = uri("{ctx.repo_url}")\n'
        '            credentials {\n'
        '                username = providers.gradleProperty("wsUser").get()\n'
        '                password = providers.gradleProperty("wsToken").get()\n'
        '            }\n'
        '            authentication { create<BasicAuthentication>("basic") }  // send them up front\n'
        '        }\n'
        '    }\n'
        '}\n'
    )


def _identifier(name: str) -> str:
    out = ''.join(c if c.isalnum() else ' ' for c in name).title().replace(' ', '')
    return out or 'Waystation'


def _snippets(ctx: SetupContext) -> list[SetupClient]:
    options = DESCRIPTOR.parse_options(ctx.repo.format_options)
    policy: VersionPolicy = options.version_policy if isinstance(options, MavenOptions) else 'release'
    hosted = ctx.repo.type is RepoType.HOSTED
    maven = [
        snippet(
            'settings-mirror',
            'Resolve everything through this repository (~/.m2/settings.xml)',
            _settings(ctx, snapshots=ctx.repo.type is RepoType.GROUP or policy != 'release'),
            language='xml',
            filename='settings.xml',
        ),
        snippet(
            'maven-config',
            'Send the credentials up front (.mvn/maven.config, or on the command line)',
            PREEMPTIVE + '\n',
            filename='.mvn/maven.config',
            description='Needed when anonymous users cannot read this repository: Waystation answers 404, not a '
            'challenge, to anonymous requests for it, and Maven only sends credentials after a challenge.',
        ),
    ]
    if hosted:
        maven.append(
            snippet(
                'pom-distribution',
                'Deploy with mvn deploy (pom.xml)',
                _distribution(ctx, policy),
                language='xml',
                filename='pom.xml',
                description='Add the matching <server> (your username and an API token) to settings.xml.',
            )
        )
        maven.append(
            snippet(
                'settings-server',
                'Deploy credentials (~/.m2/settings.xml)',
                _server(ctx.repo.name),
                language='xml',
                filename='settings.xml',
            )
        )
        maven.append(
            snippet(
                'mvn-deploy-alt',
                'Deploy to this repository without a distributionManagement section',
                f'mvn deploy -DaltDeploymentRepository={ctx.repo.name}::{ctx.repo_url}',
            )
        )
    gradle = [
        snippet(
            'gradle-repositories',
            'Resolve (build.gradle.kts)',
            _gradle_read(ctx),
            language='kotlin',
            filename='build.gradle.kts',
        ),
    ]
    if hosted:
        gradle.append(
            snippet(
                'gradle-publishing',
                'Publish (build.gradle.kts)',
                _gradle_publish(ctx),
                language='kotlin',
                filename='build.gradle.kts',
            )
        )
        gradle.append(
            snippet(
                'gradle-properties',
                'Credentials (~/.gradle/gradle.properties)',
                f'wsUser={USERNAME}\nwsToken={TOKEN}\n',
                language='ini',
                filename='gradle.properties',
            )
        )
    sbt = [
        snippet(
            'sbt-resolver',
            'Resolve (build.sbt)',
            f'resolvers += "{ctx.repo.name}" at "{ctx.repo_url}"\n',
            language='scala',
            filename='build.sbt',
        )
    ]
    return [
        SetupClient(id='maven', label='Maven', snippets=maven),
        SetupClient(id='gradle', label='Gradle', snippets=gradle),
        SetupClient(id='sbt', label='sbt', snippets=sbt),
    ]


DESCRIPTOR = FormatDescriptor(
    name='maven',
    label='Maven',
    types=frozenset({RepoType.PROXY, RepoType.HOSTED, RepoType.GROUP}),
    options_model=MavenOptions,
    ui=UiHints(icon='maven', browse='maven'),
    probe_path=_probe,
    setup_snippets=_snippets,
)
