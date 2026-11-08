"""Shared test configuration for waystation_common."""

from hypothesis import settings

# Property tests must not fail because the machine is busy (CI runners, parallel worktrees): no per-example deadline.
settings.register_profile('waystation', deadline=None)
settings.load_profile('waystation')
