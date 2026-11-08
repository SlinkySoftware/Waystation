"""The single authorization decision point (ARCHITECTURE §7). T-S01 skeleton: the RBAC engine is T-S03."""

from __future__ import annotations

from enum import StrEnum

from starlette.requests import Request

from waystation_server.repos.model import RepoConfig
from waystation_server.security.principal import ANONYMOUS, Principal


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


def method_action(method: str) -> Action:
    if method in ('GET', 'HEAD'):
        return Action.READ
    if method == 'DELETE':
        return Action.DELETE
    return Action.WRITE


async def authorize_repo(request: Request, repo: RepoConfig, action: Action) -> Principal:
    return ANONYMOUS


def require_repo(principal: Principal, action: Action, repo: RepoConfig) -> None:
    return None


def capabilities(principal: Principal) -> list[str]:
    """UI hints (OpenAPI ``Session.capabilities``); never a substitute for server-side checks."""
    if 'admin' in principal.roles:
        return ['admin', 'repositories.create', 'repositories.manage', 'stats.read']
    return []
