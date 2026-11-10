"""Deleting a repository drops every per-repository record (blobs are reclaimed later by GC)."""

from waystation_common.db.collections import REPO_USAGE, REPOSITORIES, SPECS
from waystation_server.db.repositories import CONTENT_COLLECTIONS

# Keyed by repo_id but deliberately not content: the usage counters are removed by their own code path.
NOT_CONTENT = {REPOSITORIES, REPO_USAGE}  # repo_usage: usage_db.drop() in delete()


def test_every_repo_keyed_collection_is_dropped_with_its_repository() -> None:
    keyed = {spec.name for spec in SPECS if any(next(iter(ix.document['key'])) == 'repo_id' for ix in spec.indexes)}
    missing = sorted(keyed - set(CONTENT_COLLECTIONS) - NOT_CONTENT)
    assert not missing, f'add these to db.repositories.CONTENT_COLLECTIONS (or NOT_CONTENT with a reason): {missing}'
