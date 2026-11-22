"""Build ``.gem`` files for tests (RubyGems format): a plain tar of ``metadata.gz`` (the YAML specification as
RubyGems' Psych writes it), ``data.tar.gz`` and ``checksums.yaml.gz``."""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from collections.abc import Sequence


def spec_yaml(
    name: str,
    version: str,
    *,
    platform: str = 'ruby',
    deps: Sequence[tuple[str, str, str, str]] = (),  # (name, op, version, :runtime/:development)
    ruby: tuple[str, str] = ('>=', '2.7.0'),
    summary: str = 'A demo gem',
) -> str:
    lines = [
        '--- !ruby/object:Gem::Specification',
        f'name: {name}',
        'version: !ruby/object:Gem::Version',
        f"  version: '{version}'",
        f'platform: {platform}',
        'authors:',
        '- Example Author',
        'autorequire:',
        'bindir: bin',
        'cert_chain: []',
        'date: 2026-11-01 00:00:00.000000000 Z',
        'dependencies:' + ('' if deps else ' []'),
    ]
    for anchor, (dep, op, dep_version, kind) in enumerate(deps, start=1):
        lines += [
            '- !ruby/object:Gem::Dependency',
            f'  name: {dep}',
            f'  requirement: &{anchor} !ruby/object:Gem::Requirement',
            '    requirements:',
            f'    - - "{op}"',
            '      - !ruby/object:Gem::Version',
            f"        version: '{dep_version}'",
            f'  type: :{kind}',
            '  prerelease: false',
            f'  version_requirements: *{anchor}',
        ]
    lines += [
        f'description: {summary} for tests',
        'email:',
        '- author@example.com',
        'executables: []',
        'extensions: []',
        'extra_rdoc_files: []',
        'files:',
        f'- lib/{name}.rb',
        'homepage: https://example.com/demo',
        'licenses:',
        '- MIT',
        'metadata:',
        '  source_code_uri: https://example.com/demo',
        'rdoc_options: []',
        'require_paths:',
        '- lib',
        'required_ruby_version: !ruby/object:Gem::Requirement',
        '  requirements:',
        f'  - - "{ruby[0]}"',
        '    - !ruby/object:Gem::Version',
        f"      version: '{ruby[1]}'",
        'required_rubygems_version: !ruby/object:Gem::Requirement',
        '  requirements:',
        '  - - ">="',
        '    - !ruby/object:Gem::Version',
        "      version: '0'",
        'requirements: []',
        'rubygems_version: 3.6.2',
        'signing_key:',
        'specification_version: 4',
        f'summary: {summary}',
        'test_files: []',
    ]
    return '\n'.join(lines) + '\n'


def _tar(members: Sequence[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w', format=tarfile.USTAR_FORMAT) as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o444
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def make_gem(
    name: str,
    version: str,
    *,
    yaml_text: str | None = None,
    checksums: bool = True,
    tamper_checksum: bool = False,
    extra: Sequence[tuple[str, bytes]] = (),
    **spec: object,
) -> bytes:
    metadata = gzip.compress((yaml_text or spec_yaml(name, version, **spec)).encode(), mtime=0)  # type: ignore[arg-type]
    data = gzip.compress(_tar([(f'lib/{name}.rb', f'module Demo; VERSION = "{version}"; end\n'.encode())]), mtime=0)
    members = [('metadata.gz', metadata), ('data.tar.gz', data)]
    if checksums:
        meta_sha = hashlib.sha256(metadata).hexdigest()
        data_sha = '0' * 64 if tamper_checksum else hashlib.sha256(data).hexdigest()
        text = f'---\nSHA256:\n  metadata.gz: {meta_sha}\n  data.tar.gz: {data_sha}\n'
        members.append(('checksums.yaml.gz', gzip.compress(text.encode(), mtime=0)))
    return _tar([*members, *extra])
