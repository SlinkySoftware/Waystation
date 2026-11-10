"""Reading the provisioning file: bounded, safe YAML, schema validation, secret references.

- The file is read in a thread, at most ``MAX_FILE_BYTES``; YAML is parsed with a ``SafeLoader`` that refuses
  aliases/anchors (no "billion laughs"), duplicate keys, documents nested deeper than ``MAX_DEPTH`` and more than one
  document. Parse errors report the line and column only, never the text around them (a mistakenly inlined secret
  must not reach a log or the API).
- Schema errors are reported as ``(field, message)`` with a dotted path; the offending value is never echoed.
- ``resolve`` reads a secret reference (``{file: ...}``: at most ``SECRET_MAX_BYTES``, trailing newline removed;
  ``{env: ...}``) in a thread. Waystation's own settings variables (``WAYSTATION_<SETTING>`` and their ``_FILE``
  forms) cannot be referenced, so a file cannot copy the encryption keys or database credentials into a webhook.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from waystation_common.settings import ENV_PREFIX, FILE_SUFFIX, Settings
from waystation_server.provisioning.model import SECRET_MAX_BYTES, ProvisioningFile, SecretRef

MAX_FILE_BYTES = 1024 * 1024
MAX_DEPTH = 32


@dataclass(frozen=True)
class Problem:
    field: str
    message: str


@dataclass
class Loaded:
    path: str
    sha256: str | None = None
    document: ProvisioningFile | None = None
    errors: list[Problem] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return self.document is not None and not self.errors


class _Loader(yaml.SafeLoader):
    """Safe YAML without aliases or duplicate keys, bounded nesting."""

    def compose_node(self, parent: yaml.Node | None, index: int) -> yaml.Node | None:
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()  # type: ignore[no-untyped-call]
            raise yaml.MarkedYAMLError(None, None, 'aliases are not allowed', event.start_mark)
        depth = getattr(self, '_depth', 0)
        if depth > MAX_DEPTH:
            mark = self.peek_event().start_mark  # type: ignore[no-untyped-call]
            raise yaml.MarkedYAMLError(None, None, 'nested too deeply', mark)
        self._depth = depth + 1
        try:
            return super().compose_node(parent, index)
        finally:
            self._depth = depth

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.MarkedYAMLError(None, None, f'duplicate key {key!r}', key_node.start_mark)
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _where(exc: yaml.YAMLError) -> str:
    mark = getattr(exc, 'problem_mark', None)
    problem = getattr(exc, 'problem', None) or 'not valid YAML'
    if mark is None:
        return str(problem)
    return f'line {mark.line + 1}, column {mark.column + 1}: {problem}'


def parse(text: bytes) -> tuple[ProvisioningFile | None, list[Problem]]:
    """Parse and validate the file's bytes (CPU work: call it in a thread for large files)."""
    if len(text) > MAX_FILE_BYTES:
        return None, [Problem('file', f'larger than {MAX_FILE_BYTES} bytes')]
    try:
        data = yaml.load(text.decode('utf-8'), Loader=_Loader)  # noqa: S506 - _Loader is a SafeLoader
    except UnicodeDecodeError:
        return None, [Problem('file', 'not UTF-8 text')]
    except yaml.YAMLError as exc:
        return None, [Problem('file', _where(exc))]
    if not isinstance(data, dict):
        return None, [Problem('file', 'must be a YAML mapping (version: 1, ...)')]
    try:
        return ProvisioningFile.model_validate(data), []
    except ValidationError as exc:
        problems = []
        for err in exc.errors(include_input=False, include_url=False):
            path = '.'.join(str(p) for p in err['loc']) or 'file'
            problems.append(Problem(path, str(err['msg'])))
        return None, problems


def _read(path: Path) -> bytes:
    with path.open('rb') as handle:
        return handle.read(MAX_FILE_BYTES + 1)


async def load(path: str | os.PathLike[str]) -> Loaded:
    """Read and validate the file at ``path`` (never raises for a bad file: see ``Loaded.errors``)."""
    loaded = Loaded(path=str(path))
    try:
        raw = await asyncio.to_thread(_read, Path(path))
    except OSError as exc:
        loaded.errors.append(Problem('file', f'cannot read it: {exc.strerror or type(exc).__name__}'))
        return loaded
    loaded.sha256 = hashlib.sha256(raw).hexdigest()
    loaded.document, loaded.errors = await asyncio.to_thread(parse, raw)
    return loaded


# ---------------------------------------------------------------------------- secrets
def _forbidden_env() -> frozenset[str]:
    names = set()
    for name in Settings.model_fields:
        env = f'{ENV_PREFIX}{name.upper()}'
        names.update({env, env + FILE_SUFFIX})
    return frozenset(names)


FORBIDDEN_ENV = _forbidden_env()


class SecretError(Exception):
    """A secret reference cannot be resolved; the message names the reference, never a value."""


def _resolve(ref: SecretRef) -> str:
    if ref.env is not None:
        if ref.env.upper() in FORBIDDEN_ENV:
            raise SecretError(f'env {ref.env} is a Waystation setting and cannot be referenced')
        value = os.environ.get(ref.env)
        if not value:
            raise SecretError(f'env {ref.env} is not set')
        return value
    assert ref.file is not None
    try:
        with open(ref.file, 'rb') as handle:  # noqa: PTH123
            raw = handle.read(SECRET_MAX_BYTES + 1)
    except OSError as exc:
        raise SecretError(f'file {ref.file}: cannot read it ({exc.strerror or type(exc).__name__})') from None
    if len(raw) > SECRET_MAX_BYTES:
        raise SecretError(f'file {ref.file} is larger than {SECRET_MAX_BYTES} bytes')
    try:
        value = raw.decode('utf-8').rstrip('\r\n')
    except UnicodeDecodeError:
        raise SecretError(f'file {ref.file} is not UTF-8 text') from None
    if not value:
        raise SecretError(f'file {ref.file} is empty')
    return value


async def resolve(ref: SecretRef) -> str:
    return await asyncio.to_thread(_resolve, ref)
