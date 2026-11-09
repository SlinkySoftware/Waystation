"""Cleanup policies (SPECIFICATION §4.8): criteria, version ordering and planning, shared by the server (dry-run
preview) and the worker (runs), so both evaluate a policy with the same code. Persistence and the removal itself
live in ``waystation_common.db.cleanup``; runs are ``CleanupRun`` jobs (``waystation_worker.cleanup``)."""

from waystation_common.cleanup.model import Candidate, CandidateKind, Criteria, check_schedule
from waystation_common.cleanup.plan import AssetRow, ManifestRow, RepoData, TagRow, coordinates, plan_repository

__all__ = [
    'AssetRow',
    'Candidate',
    'CandidateKind',
    'Criteria',
    'ManifestRow',
    'RepoData',
    'TagRow',
    'check_schedule',
    'coordinates',
    'plan_repository',
]
