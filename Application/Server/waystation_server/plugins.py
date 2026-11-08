"""Built-in formats: the registry the server starts with (``Services.create``).

Lives outside ``waystation_server.formats`` so the format packages never import each other (ARCHITECTURE §3). A
format plugin package exposes its descriptor and (once implemented) its plugin; register both here.
"""

from __future__ import annotations

from waystation_server.formats.registry import FormatRegistry


def default_registry() -> FormatRegistry:
    return FormatRegistry()
