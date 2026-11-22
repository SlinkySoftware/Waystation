"""Maven prefetch (T-O12; FORMATS.md "Maven", "Prefetch").

Warm items: ``<groupId>:<artifactId>:<version>[:<extension>[:<classifier>]]``, e.g.
``org.apache.commons:commons-lang3:3.17.0`` or ``io.netty:netty-transport-native-epoll:4.1.115.Final:jar:linux-x86_64``.
Warming fetches the POM and then the artifact file: the given extension (and classifier), else the one the POM's
``<packaging>`` implies (``pom`` packaging: the POM only; ``jar``, ``bundle``, ``maven-plugin``...: the jar).
Snapshots are not warmed (their file names are timestamped by the upstream). Files go through the repository's own
routes, so they are verified against the upstream's checksum files as for a client.

Refresh: asset keys are request paths (``maven-metadata.xml``), asked for again as they are.
"""

from __future__ import annotations

import re

from waystation_server.formats.base import WarmContext

POM_MAX_BYTES = 4 * 1024 * 1024
_PART = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}')
_VERSION = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.+-]{0,99}')
_EXTENSION = re.compile(r'[a-z0-9][a-z0-9.]{0,19}')
_PACKAGING = re.compile(rb'<packaging>\s*([A-Za-z0-9._-]{1,40})\s*</packaging>')
OWN_EXTENSION = frozenset({'war', 'ear', 'aar', 'rar', 'zip'})


def parse_item(item: str) -> tuple[str, str, str, str | None, str | None]:
    """``(group, artifact, version, extension, classifier)``; raises ``ValueError`` with a readable message."""
    parts = item.strip().split(':')
    if not 3 <= len(parts) <= 5:
        raise ValueError('use `groupId:artifactId:version[:extension[:classifier]]`')
    group, artifact, version = parts[:3]
    extension = parts[3] if len(parts) > 3 else None
    classifier = parts[4] if len(parts) > 4 else None
    if not _PART.fullmatch(group) or '..' in group or not _PART.fullmatch(artifact):
        raise ValueError('not a valid groupId or artifactId')
    if not _VERSION.fullmatch(version) or '..' in version:
        raise ValueError('not a valid version')
    if version.endswith('-SNAPSHOT'):
        raise ValueError('snapshots are not warmed')
    if extension is not None and not _EXTENSION.fullmatch(extension):
        raise ValueError('not a valid extension')
    if classifier is not None and not _PART.fullmatch(classifier):
        raise ValueError('not a valid classifier')
    return group, artifact, version, extension, classifier


def check_item(item: str) -> str | None:
    try:
        parse_item(item)
    except ValueError as exc:
        return str(exc)
    return None


def extension_for(pom: bytes) -> str | None:
    """The artifact file a POM's packaging implies (None: ``pom`` packaging, nothing besides the POM)."""
    match = _PACKAGING.search(pom)
    packaging = match.group(1).decode() if match else 'jar'
    if packaging == 'pom':
        return None
    return packaging if packaging in OWN_EXTENSION else 'jar'


async def warm(ctx: WarmContext, item: str) -> None:
    group, artifact, version, extension, classifier = parse_item(item)
    base = f'{group.replace(".", "/")}/{artifact}/{version}/{artifact}-{version}'
    pom = await ctx.get(f'{base}.pom')
    if extension is None:
        extension = extension_for(await ctx.read(pom, max_bytes=POM_MAX_BYTES))
        if extension is None:
            return
    if extension == 'pom' and classifier is None:
        return
    await ctx.get(f'{base}{f"-{classifier}" if classifier else ""}.{extension}')
