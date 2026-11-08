"""Cleanup policy shapes (DATA_MODEL ``cleanup_policies``, SPECIFICATION §4.8) and what a plan is made of."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from waystation_common.jobs.schedule import parse_schedule

CandidateKind = Literal['asset', 'tag', 'manifest', 'layer']
KIND_ORDER: dict[str, int] = {'tag': 0, 'manifest': 1, 'layer': 2, 'asset': 3}
REGEX_MAX = 256
DAYS_MAX = 36500


class Criteria(BaseModel):
    """Every criterion that is set must match (AND). See OpenAPI ``CleanupCriteria``."""

    model_config = ConfigDict(extra='forbid', frozen=True)

    last_downloaded_older_than_days: int | None = Field(default=None, ge=1, le=DAYS_MAX)
    published_older_than_days: int | None = Field(default=None, ge=1, le=DAYS_MAX)
    keep_latest_n_versions: int | None = Field(default=None, ge=1, le=1000)
    name_include: str | None = Field(default=None, max_length=REGEX_MAX)
    name_exclude: str | None = Field(default=None, max_length=REGEX_MAX)
    version_include: str | None = Field(default=None, max_length=REGEX_MAX)
    version_exclude: str | None = Field(default=None, max_length=REGEX_MAX)
    prerelease_only: bool = False
    untagged_older_than_days: int | None = Field(default=None, ge=1, le=DAYS_MAX)

    @field_validator('name_include', 'name_exclude', 'version_include', 'version_exclude')
    @classmethod
    def _regex(cls, value: str | None) -> str | None:
        if value is None or value == '':
            return None
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f'not a valid regular expression: {exc.msg}') from None
        return value

    @property
    def selects_versions(self) -> bool:
        """True when the version criteria select anything (they are evaluated only then)."""
        return any(
            (
                self.last_downloaded_older_than_days,
                self.published_older_than_days,
                self.keep_latest_n_versions,
                self.name_include,
                self.version_include,
                self.prerelease_only,
            )
        )

    @property
    def selects_anything(self) -> bool:
        return self.selects_versions or self.untagged_older_than_days is not None


def check_schedule(value: str | None) -> str | None:
    """Normalise a schedule (``None``/blank = manual only); ``ValueError`` when it does not parse."""
    if value is None or not value.strip():
        return None
    value = ' '.join(value.split())
    parse_schedule(value)
    return value


@dataclass(frozen=True)
class Candidate:
    """One thing a cleanup run removes. ``path`` is the asset path (docker tags without an asset: ``image:tag``)."""

    kind: CandidateKind
    name: str
    version: str | None
    path: str
    size: int
    created_at: datetime | None
    last_downloaded_at: datetime | None
    reason: str
    asset_id: str | None = None
    blob_sha256: str | None = None  # the asset's blob when planned: a changed asset is left alone
    image: str | None = None  # docker
    tag: str | None = None
    digest: str | None = None

    def sort_key(self) -> tuple[int, str, str, str]:
        return (KIND_ORDER[self.kind], self.name, self.version or '', self.path)
