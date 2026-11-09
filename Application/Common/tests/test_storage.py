import asyncio
import hashlib
import os
import signal
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from waystation_common import storage
from waystation_common.datadir import prepare_data_dir
from waystation_common.errors import DigestMismatch, InsufficientStorage, NotFound, PayloadTooLarge, ValidationFailed
from waystation_common.settings import Settings
from waystation_common.storage import BlobStore, check_sha256

DATA = os.urandom(3 * 1024 * 1024 + 17)
DIGEST = hashlib.sha256(DATA).hexdigest()


@pytest.fixture
def store(tmp_path: Path) -> BlobStore:
    root = tmp_path / 'data'
    prepare_data_dir(root, os.geteuid(), os.getegid())
    return BlobStore(root, chunk_size=64 * 1024)


def files_under(path: Path) -> list[Path]:
    return [p for p in path.rglob('*') if p.is_file()]


async def write_all(store: BlobStore, data: bytes, chunk: int = 100_000, **kwargs: object) -> storage.BlobRef:
    async with await store.open_writer(**kwargs) as writer:  # type: ignore[arg-type]
        for i in range(0, len(data), chunk):
            await writer.write(data[i : i + chunk])
        return await store.commit(writer)


async def test_write_commit_layout_and_modes(store: BlobStore) -> None:
    ref = await write_all(store, DATA, expected_sha256=DIGEST, expected_size=len(DATA))
    assert ref == storage.BlobRef(DIGEST, len(DATA), created=True)
    path = store.blob_path(DIGEST)
    assert path == store.root / 'blobs' / 'sha256' / DIGEST[:2] / DIGEST[2:4] / DIGEST
    assert path.read_bytes() == DATA
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o2750
    assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o2750
    assert files_under(store.uploads_dir) == []
    assert await store.exists(DIGEST)
    assert await store.stat(DIGEST) == storage.BlobRef(DIGEST, len(DATA))
    assert store.path_for_xaccel(DIGEST) == f'/_blobs/sha256/{DIGEST[:2]}/{DIGEST[2:4]}/{DIGEST}'


async def test_second_commit_of_same_content_is_not_created(store: BlobStore) -> None:
    assert (await write_all(store, b'abc')).created
    again = await write_all(store, b'abc')
    assert not again.created
    assert again.sha256 == hashlib.sha256(b'abc').hexdigest()
    assert files_under(store.uploads_dir) == []


async def test_concurrent_identical_writes_commit_once(store: BlobStore) -> None:
    refs = await asyncio.gather(*(write_all(store, DATA, chunk=50_000) for _ in range(8)))
    assert sum(ref.created for ref in refs) == 1
    assert {ref.sha256 for ref in refs} == {DIGEST}
    assert store.blob_path(DIGEST).read_bytes() == DATA
    assert files_under(store.uploads_dir) == []
    assert store._locks == {}


async def test_digest_and_size_mismatch_leave_nothing(store: BlobStore) -> None:
    with pytest.raises(DigestMismatch, match='sha256'):
        await write_all(store, b'abc', expected_sha256='0' * 64)
    with pytest.raises(DigestMismatch, match='size'):
        await write_all(store, b'abc', expected_size=4)
    assert files_under(store.root) == []


async def test_size_limits(store: BlobStore) -> None:
    with pytest.raises(PayloadTooLarge):
        await write_all(store, b'x' * 10, max_size=5)
    with pytest.raises(PayloadTooLarge):
        await write_all(store, b'x' * 10, expected_size=5)
    with pytest.raises(PayloadTooLarge):
        await store.open_writer(max_size=5, expected_size=6)
    limited = BlobStore(store.root, max_size=3)
    with pytest.raises(PayloadTooLarge):
        await write_all(limited, b'abcd')
    assert files_under(store.root) == []


async def test_free_space_guard(store: BlobStore) -> None:
    free = await store.free_bytes()
    assert free > 0
    greedy = BlobStore(store.root, min_free_bytes=free * 2)
    refused: list[int] = []
    greedy.on_refused = lambda: refused.append(1)
    with pytest.raises(InsufficientStorage):
        await greedy.open_writer()
    assert refused == [1]  # counted (waystation_storage_refusals_total)
    with pytest.raises(InsufficientStorage):
        await store.open_writer(expected_size=free * 2)


async def test_abort_and_context_manager(store: BlobStore) -> None:
    writer = await store.open_writer()
    await writer.write(b'data')
    await writer.sync()
    assert writer.path.exists()
    await writer.abort()
    await writer.abort()  # idempotent
    assert not writer.path.exists()
    with pytest.raises(ValueError, match='closed'):
        await writer.write(b'more')
    with pytest.raises(ValueError, match='closed'):
        await store.commit(writer)
    with pytest.raises(ValueError, match='closed'):
        await writer.sync()

    async def fail_midway() -> None:
        async with await store.open_writer() as w:
            await w.write(b'x')
            raise RuntimeError

    with pytest.raises(RuntimeError):
        await fail_midway()
    assert files_under(store.uploads_dir) == []

    async with await store.open_writer() as w:
        await w.write(b'')  # no-op
        ref = await store.commit(w)
    assert ref.size == 0
    assert ref.sha256 == hashlib.sha256(b'').hexdigest()


async def test_delete(store: BlobStore) -> None:
    ref = await write_all(store, b'gone soon')
    assert await store.delete(ref.sha256)
    assert not await store.delete(ref.sha256)
    assert await store.stat(ref.sha256) is None


def test_bad_digests_rejected(store: BlobStore) -> None:
    for bad in ('', 'A' * 64, '../' + 'a' * 61, 'a' * 63, 'g' * 64):
        with pytest.raises(ValidationFailed):
            check_sha256(bad)
        with pytest.raises(ValidationFailed):
            store.path_for_xaccel(bad)


async def test_open_writer_rejects_bad_expected_digest(store: BlobStore) -> None:
    with pytest.raises(ValidationFailed):
        await store.open_writer(expected_sha256='nope')


async def test_sweep_uploads(store: BlobStore, tmp_path: Path) -> None:
    orphan = store.uploads_dir / 'orphan'
    orphan.write_bytes(b'x')
    old = time.time() - 3600
    os.utime(orphan, (old, old))
    fresh = store.uploads_dir / 'fresh'
    fresh.write_bytes(b'y')
    (store.uploads_dir / 'subdir').mkdir()
    active = await store.open_writer()
    os.utime(active.path, (old, old))
    assert await store.sweep_uploads(older_than_s=60) == 1
    assert not orphan.exists()
    assert fresh.exists()
    assert active.path.exists()  # this process's open writer is never swept
    await active.abort()
    assert await BlobStore(tmp_path / 'missing').sweep_uploads() == 0


async def test_from_settings(tmp_path: Path) -> None:
    s = Settings(data_dir=tmp_path, min_free_gb=1, max_upload_gb=2)
    bs = BlobStore.from_settings(s)
    assert (bs.root, bs.min_free_bytes, bs.max_size) == (tmp_path, 1024**3, 2 * 1024**3)


async def test_event_loop_stays_responsive(store: BlobStore) -> None:
    """Large writes run in threads: a ticker keeps ticking while 64 MiB are hashed and written."""
    ticks = 0
    stop = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.001)

    task = asyncio.create_task(ticker())
    big = os.urandom(1024 * 1024)
    started = time.monotonic()
    async with await BlobStore(store.root).open_writer() as writer:
        for _ in range(64):
            await writer.write(big)
        await store.commit(writer)
    elapsed = time.monotonic() - started
    stop.set()
    await task
    assert ticks >= max(5, int(elapsed / 0.001 * 0.2))


def test_kill_mid_write_leaves_no_blob(store: BlobStore) -> None:
    script = textwrap.dedent(
        f"""
        import asyncio, os, sys, time
        from pathlib import Path
        from waystation_common.storage import BlobStore

        async def main():
            store = BlobStore(Path({str(store.root)!r}), chunk_size=65536)
            writer = await store.open_writer()
            for _ in range(32):
                await writer.write(os.urandom(65536))
            await writer.sync()
            print('ready', flush=True)
            time.sleep(60)

        asyncio.run(main())
        """
    )
    proc = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == 'ready'
    finally:
        proc.send_signal(signal.SIGKILL)
        proc.wait(10)
    assert files_under(store.blobs_dir) == []
    orphans = files_under(store.uploads_dir)
    assert len(orphans) == 1
    assert orphans[0].stat().st_size == 32 * 65536
    assert asyncio.run(store.sweep_uploads(older_than_s=0)) == 1
    assert files_under(store.uploads_dir) == []


# ---------------------------------------------------------------------------- resumable uploads
async def test_resumable_upload_across_writers(store: BlobStore) -> None:
    upload_id = await store.start_upload()
    assert await store.upload_size(upload_id) == 0
    first = await store.resume_writer(upload_id, offset=0)
    await first.write(DATA[:1000])
    assert await first.suspend() == 1000
    with pytest.raises(storage.UploadOffsetMismatch) as wrong:
        await store.resume_writer(upload_id, offset=10)
    assert wrong.value.size == 1000
    second = await store.resume_writer(upload_id, offset=1000)
    assert not second._rehash  # this process kept the hash state
    await second.write(DATA[1000:])
    ref = await store.commit(second)
    assert ref.sha256 == DIGEST and ref.size == len(DATA)
    assert store.blob_path(DIGEST).read_bytes() == DATA
    assert await store.upload_size(upload_id) is None


async def test_resumable_upload_without_hash_state_rehashes(store: BlobStore) -> None:
    upload_id = await store.start_upload()
    writer = await store.resume_writer(upload_id, offset=0)
    await writer.write(DATA[:5000])
    await writer.suspend()
    store._partials.clear()  # as if another replica wrote the first part
    resumed = await store.resume_writer(upload_id, offset=5000)
    assert resumed._rehash
    await resumed.write(DATA[5000:])
    assert (await store.commit(resumed)).sha256 == DIGEST


async def test_resumable_rollback_and_discard(store: BlobStore) -> None:
    upload_id = await store.start_upload()
    writer = await store.resume_writer(upload_id, offset=0)
    await writer.write(b'a' * 100)
    await writer.suspend()
    failed = await store.resume_writer(upload_id, offset=100)
    await failed.write(b'b' * 70_000)
    await failed.flush()
    await failed.rollback()
    await failed.rollback()  # idempotent
    assert await store.upload_size(upload_id) == 100
    again = await store.resume_writer(upload_id, offset=100)
    await again.write(b'c')
    assert (await store.commit(again)).sha256 == hashlib.sha256(b'a' * 100 + b'c').hexdigest()
    other = await store.start_upload()
    await store.discard_upload(other)
    assert await store.upload_size(other) is None
    with pytest.raises(NotFound):
        await store.resume_writer(other, offset=0)
    with pytest.raises(ValidationFailed):
        await store.upload_size('../../etc/passwd')
    with pytest.raises(ValueError, match='closed'):
        await again.suspend()


async def test_resumable_size_limit_discards(store: BlobStore) -> None:
    upload_id = await store.start_upload()
    writer = await store.resume_writer(upload_id, offset=0, max_size=10)
    with pytest.raises(PayloadTooLarge):
        await writer.write(b'x' * 11)
    assert await store.upload_size(upload_id) is None


async def test_partial_hash_cache_is_bounded(store: BlobStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(storage, 'PARTIAL_HASHES', 2)
    ids = [await store.start_upload() for _ in range(3)]
    for upload_id in ids:
        writer = await store.resume_writer(upload_id, offset=0)
        await writer.write(b'z')
        await writer.suspend()
    assert list(store._partials) == ids[1:]


async def test_resume_repairs_unacknowledged_bytes(store: BlobStore) -> None:
    upload_id = await store.start_upload()
    writer = await store.resume_writer(upload_id, offset=0)
    await writer.write(b'abcdef')
    await writer.suspend()
    with pytest.raises(storage.UploadOffsetMismatch):
        await store.resume_writer(upload_id, offset=3)
    repaired = await store.resume_writer(upload_id, offset=3, repair=True)
    assert repaired._rehash  # the cached state was for 6 bytes
    await repaired.write(b'xyz')
    assert (await store.commit(repaired)).sha256 == hashlib.sha256(b'abcxyz').hexdigest()
