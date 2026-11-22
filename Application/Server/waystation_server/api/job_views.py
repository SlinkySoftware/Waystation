"""API shapes shared by every route that starts or lists jobs (cleanup, maintenance, tasks, repository prefetch)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from waystation_common.db.client import Document

RunStatus = Literal['running', 'succeeded', 'failed', 'dead', 'interrupted']


class JobAccepted(BaseModel):
    job_id: str
    status: Literal['enqueued', 'merged', 'rerun', 'running']


def run_trigger(doc: Document) -> Literal['schedule', 'manual', 'retry']:
    """Why a ``job_runs`` document ran (runs recorded before the trigger existed: from ``definition_id``)."""
    match doc.get('trigger'):
        case 'schedule':
            return 'schedule'
        case 'manual':
            return 'manual'
        case 'retry':
            return 'retry'
    return 'schedule' if doc.get('definition_id') else 'manual'
