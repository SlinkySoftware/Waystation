"""The worker's job system (ARCHITECTURE §10): ``JobSystem`` = ``Runner`` (consume, run, retry, dead-letter,
reclaim) + ``Coordinator`` (leader: outbox dispatcher, scheduler, delayed jobs) + presence. Handlers register with
``@handles(PayloadType)``; built-ins are in ``builtin``. The protocol shared with the server is
``waystation_common.jobs``."""

from waystation_worker.jobs import builtin
from waystation_worker.jobs.config import JobConfig
from waystation_worker.jobs.context import JobContext
from waystation_worker.jobs.registry import HANDLERS, Handler, handles
from waystation_worker.jobs.system import JobSystem

BUILTIN_DEFINITIONS = builtin.BUILTIN_DEFINITIONS

__all__ = ['BUILTIN_DEFINITIONS', 'HANDLERS', 'Handler', 'JobConfig', 'JobContext', 'JobSystem', 'handles']
