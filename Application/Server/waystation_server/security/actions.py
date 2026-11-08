"""Permission actions (SPECIFICATION §4.10). Dependency-free so format plugins can name them; decisions are made
only by ``waystation_server.security.access``."""

from __future__ import annotations

from enum import StrEnum


class Action(StrEnum):
    BROWSE = 'browse'
    READ = 'read'
    WRITE = 'write'
    DELETE = 'delete'
    MANAGE = 'manage'
    ADMIN = 'admin'
    USER_ADMIN = 'user-admin'
    AUDIT_READ = 'audit-read'
    STATS_READ = 'stats-read'
    SETTINGS_ADMIN = 'settings-admin'


GLOBAL_ACTIONS = frozenset(
    {Action.ADMIN, Action.USER_ADMIN, Action.AUDIT_READ, Action.STATS_READ, Action.SETTINGS_ADMIN}
)
