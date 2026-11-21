"""Helpers for "Set me up" snippets (SPECIFICATION §4.12).

OS variants: a snippet without ``os`` applies everywhere; a snippet with the same ``id`` and an ``os`` replaces it on
that operating system (the UI shows a Linux / macOS / Windows switch when a client has any).
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from waystation_server.formats.base import SetupSnippet

OS = Literal['linux', 'macos', 'windows']

TOKEN = '{{TOKEN}}'  # noqa: S105 - a placeholder the UI fills in, not a secret
USERNAME = '{{USERNAME}}'


def host_of(url: str) -> str:
    return urlsplit(url).netloc


def path_of(url: str) -> str:
    """URL without the scheme (``//host/path``), the form npm uses for per-registry auth keys."""
    parts = urlsplit(url)
    return f'//{parts.netloc}{parts.path}'


def snippet(
    id: str,
    title: str,
    content: str,
    *,
    language: str = 'shell',
    description: str | None = None,
    filename: str | None = None,
) -> SetupSnippet:
    return SetupSnippet(
        id=id, title=title, description=description, language=language, filename=filename, content=content
    )


def on(
    os: OS,
    base: SetupSnippet,
    *,
    content: str | None = None,
    filename: str | None = None,
    language: str | None = None,
    description: str | None = None,
) -> SetupSnippet:
    """``base`` as it reads on ``os`` (same id and title; the given fields replaced)."""
    update: dict[str, object] = {'os': os}
    if content is not None:
        update['content'] = content
    if filename is not None:
        update['filename'] = filename
    if language is not None:
        update['language'] = language
    if description is not None:
        update['description'] = description
    return base.model_copy(update=update)


def powershell_env(lines: list[tuple[str, str]]) -> str:
    """``export NAME=value`` lines as PowerShell (``$env:NAME = "value"``)."""
    return '\n'.join(f'$env:{name} = "{value}"' for name, value in lines)
