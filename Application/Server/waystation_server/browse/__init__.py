"""Browse (SPECIFICATION §4.6; OpenAPI tag ``browse``): what a repository holds, for the UI.

Format-neutral on purpose: the REST API (``api.browse``) and the format plugins both import this package, never each
other (ARCHITECTURE §3).

- ``models``: the response models of the contract (``FolderListing``, ``NpmPackagePage``, ``AptSuite``,
  ``DockerManifest``...), the ``BrowseContext`` a view gets, the per-format view protocols a plugin implements on its
  ``browser`` attribute, and opaque cursor helpers.
- ``tree``: the generic folder listing of cached assets, for every format.
"""
