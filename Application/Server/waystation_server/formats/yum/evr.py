"""RPM version ordering: moved to ``waystation_common.versionsort.rpm`` (the worker's component indexer needs it
too); re-exported here for the plugin."""

from __future__ import annotations

from waystation_common.versionsort.rpm import evr_key, evr_string, version_key

__all__ = ['evr_key', 'evr_string', 'version_key']
