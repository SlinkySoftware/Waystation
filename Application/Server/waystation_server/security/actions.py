"""Permission actions (SPECIFICATION §4.10). Dependency-free so format plugins can name them; decisions are made
only by ``waystation_server.security.access``."""

from __future__ import annotations

from dataclasses import dataclass
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


class TokenScope(StrEnum):
    """What an API token may be used for (SPECIFICATION §4.9). A token only ever narrows its owner's grants:
    ``security.access`` maps each scope to the actions it allows and intersects them with the owner's RBAC."""

    READ = 'read'  # browse + read (pull, download, install)
    WRITE = 'write'  # + write (push, upload)
    DELETE = 'delete'  # + delete
    MANAGE = 'manage'  # + repository settings
    STATS_READ = 'stats-read'
    METRICS = 'metrics'  # the Prometheus endpoint only
    ADMIN = 'admin'  # everything the owner may do


@dataclass(frozen=True)
class Permission:
    """One ``(action, scope)`` pair of a role (SPECIFICATION §4.10)."""

    action: Action
    scope: str


@dataclass(frozen=True)
class Grants:
    """A principal's materialised permissions: its roles' permissions plus the implied ones (``security.access``
    computes them; ``security.rbac`` caches them per ``grant_version``)."""

    is_admin: bool
    permissions: tuple[Permission, ...]
