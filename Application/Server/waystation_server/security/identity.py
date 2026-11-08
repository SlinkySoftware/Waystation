"""User and session records (DATA_MODEL ``users``, ``sessions``)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Preferences(BaseModel):
    model_config = ConfigDict(extra='forbid', json_schema_serialization_defaults_required=True)

    theme: Literal['light', 'dark', 'system'] = 'system'
    density: Literal['comfortable', 'compact'] = 'comfortable'


class UserRecord(BaseModel):
    model_config = ConfigDict(extra='ignore', populate_by_name=True)

    id: str = Field(alias='_id')
    username: str  # lower-case
    display_name: str
    email: str | None = None
    source: str = 'local'
    password_hash: str | None = None
    disabled: bool = False
    roles: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    preferences: Preferences = Field(default_factory=Preferences)
    must_change_password: bool = False
    failed_logins: int = 0
    locked_until: datetime | None = None
    last_login: datetime | None = None
    created_at: datetime


class SessionRecord(BaseModel):
    """``_id`` is the sha256 of the cookie value: a database dump does not reveal usable session ids."""

    model_config = ConfigDict(extra='ignore', populate_by_name=True)

    id: str = Field(alias='_id')
    user_id: str
    csrf: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    ip: str | None = None
    user_agent: str | None = None
