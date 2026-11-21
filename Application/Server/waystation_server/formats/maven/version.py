"""Maven version ordering: moved to ``waystation_common.versionsort.maven`` (the worker's component indexer needs it
too); re-exported here for the plugin."""

from __future__ import annotations

from waystation_common.versionsort.maven import compare, is_snapshot, sort_key, sort_versions, version_order

__all__ = ['compare', 'is_snapshot', 'sort_key', 'sort_versions', 'version_order']
