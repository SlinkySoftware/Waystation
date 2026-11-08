"""Helpers for "Set me up" snippets (SPECIFICATION §4.12)."""

from __future__ import annotations

from urllib.parse import urlsplit

from waystation_server.formats.base import SetupSnippet

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
