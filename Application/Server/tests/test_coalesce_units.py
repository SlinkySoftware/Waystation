"""Coalescing helpers (T-S08) that need no Valkey: state parsing, tail decisions, network filesystem detection."""

from pathlib import Path

from redis.asyncio import Redis

from waystation_common.errors import DigestMismatch, NotFound, UpstreamError
from waystation_common.settings import CoalesceTail
from waystation_server.proxy.coalesce import Coalescer, FillState, Status, data_dir_is_network_fs, failure_code
from waystation_server.proxy.engine import UpstreamTimeout

MOUNTS = """\
overlay / overlay rw,relatime 0 0
/dev/sda1 /var/lib/waystation ext4 rw,relatime 0 0
server.example.com:/export /var/lib/waystation/shared nfs4 rw,relatime 0 0
//files.example.com/share /mnt/smb\\040share cifs rw 0 0
sshfs#host: /mnt/remote fuse.sshfs rw 0 0
"""


def test_network_filesystem_detection() -> None:
    assert not data_dir_is_network_fs(Path('/var/lib/waystation'), MOUNTS)
    assert data_dir_is_network_fs(Path('/var/lib/waystation/shared'), MOUNTS)
    assert data_dir_is_network_fs(Path('/var/lib/waystation/shared/data'), MOUNTS)
    assert not data_dir_is_network_fs(Path('/var/lib/waystation/sharedx'), MOUNTS)
    assert data_dir_is_network_fs(Path('/mnt/smb share/x'), MOUNTS)
    assert data_dir_is_network_fs(Path('/mnt/remote'), MOUNTS)
    assert not data_dir_is_network_fs(Path('/srv'), MOUNTS)


def test_fill_state_parse() -> None:
    state = FillState.parse(
        {
            'owner': 'o',
            'status': 'streaming',
            'host': 'h',
            'upload': 'u',
            'written': '10',
            'synced': '5',
            'total': '20',
            'headers': '{"Docker-Content-Digest": "sha256:x"}',
            'followers': '3',
        }
    )
    assert state is not None
    assert state.status is Status.STREAMING
    assert state.active
    assert (state.written, state.synced, state.total, state.followers) == (10, 5, 20, 3)
    assert state.headers == {'Docker-Content-Digest': 'sha256:x'}
    assert state == FillState.parse({'owner': 'o', 'status': 'streaming', 'host': 'h', 'upload': 'u',
                                     'written': '10', 'synced': '5', 'total': '20',
                                     'headers': '{"Docker-Content-Digest": "sha256:x"}', 'followers': '9'})  # fmt: skip
    assert FillState.parse({'owner': 'o', 'status': 'bogus'}) is None
    assert FillState.parse({'status': 'done'}) is None
    assert FillState.parse({'owner': 'o', 'status': 'done', 'size': 'x'}) is None


def test_failure_codes() -> None:
    assert failure_code(NotFound('Not Found'))[:2] == (Status.FAILED, 'not_found')
    assert failure_code(DigestMismatch('bad'))[:2] == (Status.FAILED, 'mismatch')
    assert failure_code(UpstreamTimeout('slow'))[:2] == (Status.FAILED, 'timeout')
    assert failure_code(UpstreamError('down'))[:2] == (Status.FAILED, 'upstream')
    assert failure_code(RuntimeError('boom'))[0] is Status.ABORTED


def test_tail_decisions(tmp_path: Path) -> None:
    coalescer = Coalescer(Redis.from_url('redis://127.0.0.1:1/0'), data_dir=tmp_path)
    coalescer.host = 'here'
    here = FillState(owner='o', status=Status.STREAMING, host='here', written=100, synced=40)
    there = FillState(owner='o', status=Status.STREAMING, host='there', written=100, synced=40)
    assert coalescer.tail_ok(here)
    assert not coalescer.tail_ok(there)
    assert coalescer.readable(here) == 100
    assert coalescer.readable(there) == 40
    coalescer.network_fs = True
    assert not coalescer.tail_ok(here)
    coalescer.mode = CoalesceTail.LOCAL
    assert coalescer.tail_ok(there)
    coalescer.mode = CoalesceTail.WAIT
    assert not coalescer.tail_ok(here)
    done = FillState(owner='o', status=Status.DONE, host='there', written=100, synced=40, size=120)
    assert coalescer.readable(done) == 120
    assert Coalescer.key('r1', 'a/b').startswith('fill:r1:')
