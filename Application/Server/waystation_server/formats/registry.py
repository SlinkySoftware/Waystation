"""Format registry: descriptors (for the REST API and validation) and plugins (for serving), by format name.

The registry is filled at start-up by ``waystation_server.plugins`` (outside the ``formats`` package, so formats
never import each other, ARCH §3). A format can be registered with a descriptor only: repositories of that format
can then be created and configured, and their protocol endpoints answer 404 until the plugin lands.
"""

from __future__ import annotations

from waystation_server.formats.base import FormatDescriptor, FormatPlugin


class FormatRegistry:
    def __init__(self) -> None:
        self._descriptors: dict[str, FormatDescriptor] = {}
        self._plugins: dict[str, FormatPlugin] = {}

    def register(self, descriptor: FormatDescriptor, plugin: FormatPlugin | None = None) -> None:
        if descriptor.name in self._descriptors:
            raise ValueError(f'format {descriptor.name!r} registered twice')
        self._descriptors[descriptor.name] = descriptor
        if plugin is not None:
            if plugin.descriptor is not descriptor:
                raise ValueError(f'plugin for {descriptor.name!r} has a different descriptor')
            self._plugins[descriptor.name] = plugin

    def set_plugin(self, plugin: FormatPlugin) -> None:
        """Attach (or replace) the plugin of an already registered format."""
        name = plugin.descriptor.name
        if name not in self._descriptors:
            self._descriptors[name] = plugin.descriptor
        self._plugins[name] = plugin

    def descriptor(self, name: str) -> FormatDescriptor | None:
        return self._descriptors.get(name)

    def plugin(self, name: str) -> FormatPlugin | None:
        return self._plugins.get(name)

    def descriptors(self) -> list[FormatDescriptor]:
        return [self._descriptors[name] for name in sorted(self._descriptors)]
