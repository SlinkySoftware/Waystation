"""Built-in formats: the registry the server starts with (``Services.create``).

Lives outside ``waystation_server.formats`` so the format packages never import each other (ARCHITECTURE §3). A
format package exposes ``DESCRIPTOR`` (``<format>.descriptor``) and, once implemented, a plugin; register both here
(``registry.register(DESCRIPTOR, plugin)``). Built in: docker, npm, apt, raw and yum
(T-P2-YUM) proxy repositories, pypi (T-P5-PYPI) proxy, hosted and group repositories.
"""

from __future__ import annotations

from waystation_server.formats.apt.descriptor import DESCRIPTOR as APT
from waystation_server.formats.apt.plugin import AptPlugin
from waystation_server.formats.docker.descriptor import DESCRIPTOR as DOCKER
from waystation_server.formats.docker.plugin import DockerPlugin
from waystation_server.formats.npm.descriptor import DESCRIPTOR as NPM
from waystation_server.formats.npm.plugin import NpmPlugin
from waystation_server.formats.pypi.descriptor import DESCRIPTOR as PYPI
from waystation_server.formats.pypi.plugin import PypiPlugin
from waystation_server.formats.raw.descriptor import DESCRIPTOR as RAW
from waystation_server.formats.raw.plugin import RawPlugin
from waystation_server.formats.registry import FormatRegistry
from waystation_server.formats.yum.descriptor import DESCRIPTOR as YUM
from waystation_server.formats.yum.plugin import YumPlugin


def default_registry() -> FormatRegistry:
    registry = FormatRegistry()
    for descriptor in (DOCKER, NPM, APT, RAW, YUM, PYPI):
        registry.register(descriptor)
    registry.set_plugin(RawPlugin())  # T-P2-RAW
    registry.set_plugin(AptPlugin())  # T-P2-APT
    registry.set_plugin(NpmPlugin())  # T-P2-NPM
    registry.set_plugin(DockerPlugin())  # docker / OCI proxy (T-P2-DOCKERP)
    registry.set_plugin(YumPlugin())  # T-P2-YUM
    registry.set_plugin(PypiPlugin())  # T-P5-PYPI: proxy, hosted, group
    return registry
