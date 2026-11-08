"""Built-in formats: the registry the server starts with (``Services.create``).

Lives outside ``waystation_server.formats`` so the format packages never import each other (ARCHITECTURE §3). A
format package exposes ``DESCRIPTOR`` (``<format>.descriptor``) and, once implemented, a plugin; register both here
(``registry.register(DESCRIPTOR, plugin)``). M0: descriptors for the docker, npm, apt and raw proxy repositories.
"""

from __future__ import annotations

from waystation_server.formats.apt.descriptor import DESCRIPTOR as APT
from waystation_server.formats.apt.plugin import AptPlugin
from waystation_server.formats.docker.descriptor import DESCRIPTOR as DOCKER
from waystation_server.formats.npm.descriptor import DESCRIPTOR as NPM
from waystation_server.formats.raw.descriptor import DESCRIPTOR as RAW
from waystation_server.formats.raw.plugin import RawPlugin
from waystation_server.formats.registry import FormatRegistry


def default_registry() -> FormatRegistry:
    registry = FormatRegistry()
    for descriptor in (DOCKER, NPM, APT, RAW):
        registry.register(descriptor)
    registry.set_plugin(RawPlugin())  # T-P2-RAW
    registry.set_plugin(AptPlugin())  # T-P2-APT
    return registry
