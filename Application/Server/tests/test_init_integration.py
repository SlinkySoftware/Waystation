import os
from pathlib import Path
from typing import Any

import pytest

from waystation_common.db.client import Database
from waystation_common.db.migrations import MIGRATIONS
from waystation_common.settings import Settings
from waystation_server.init import run

pytestmark = pytest.mark.integration


def _logged(caplog: pytest.LogCaptureFixture, message: str) -> dict[str, Any]:
    return next(r.__dict__ for r in caplog.records if r.getMessage() == message)


async def test_init_run_is_idempotent(mongo_db: Database, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(
        mongo_uri=os.environ['WAYSTATION_TEST_MONGO_URI'],  # type: ignore[arg-type]
        mongo_db=mongo_db.name,
        mongo_replset_init=True,
        data_dir=tmp_path / 'data',
        uid=os.geteuid(),
        gid=os.getegid(),
    )
    caplog.set_level('INFO', logger='waystation.init')
    await run(settings)
    assert (tmp_path / 'data' / 'blobs').is_dir()
    assert _logged(caplog, 'schema ready')['indexes_created']
    assert _logged(caplog, 'migrations applied')['migrations'] == [m.id for m in MIGRATIONS]

    caplog.clear()
    await run(settings)
    second = _logged(caplog, 'schema ready')
    assert second['collections_created'] == second['indexes_created'] == []
    assert _logged(caplog, 'migrations applied')['migrations'] == []
    assert _logged(caplog, 'data dir ready')['paths_fixed'] == 0
