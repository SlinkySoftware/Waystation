"""The generic folder walk (``db.browse.children``, ``browse.tree.list_folder``) against an in-memory ``assets``
collection: the same pages as a brute-force listing, and few queries (T-P2-PERF: browse_folder p95)."""

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from waystation_server.browse.tree import list_folder
from waystation_server.db import browse as browse_db


class Cursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs
        self._limit = len(docs)

    def sort(self, key: str, direction: int) -> Cursor:
        assert (key, direction) == ('path', 1)
        return self

    def limit(self, n: int) -> Cursor:
        self._limit = n
        return self

    async def to_list(self) -> list[dict[str, Any]]:
        return self._docs[: self._limit]


class Assets:
    """``find`` on ``(repo_id, path)`` ranges, in path (byte) order; counts the queries."""

    def __init__(self, paths: list[str]) -> None:
        self.docs = [{'path': p, 'size': len(p)} for p in sorted(set(paths))]
        self.queries = 0

    def find(self, query: dict[str, Any], projection: dict[str, Any]) -> Cursor:
        self.queries += 1
        bounds = query['path']
        out = []
        for doc in self.docs:
            path = doc['path']
            if '$gte' in bounds and path < bounds['$gte']:
                continue
            if '$gt' in bounds and path <= bounds['$gt']:
                continue
            if '$lt' in bounds and path >= bounds['$lt']:
                continue
            out.append(doc)
        return Cursor(out)


class Db:
    def __init__(self, paths: list[str]) -> None:
        self.assets = Assets(paths)

    def __getitem__(self, name: str) -> Assets:
        return self.assets


def expected(paths: list[str], prefix: str) -> list[tuple[str, str]]:
    names = [p[len(prefix) :] for p in set(paths) if p.startswith(prefix)]
    folders = sorted({n.split('/', 1)[0] for n in names if '/' in n}, key=lambda f: f + '/')  # a folder sorts as f/
    files = sorted(n for n in names if '/' not in n)
    return [('folder', f) for f in folders] + [('file', f) for f in files]


async def walk(db: Db, prefix: str, limit: int) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    cursor = None
    for _ in range(1000):
        page = await list_folder(db, 'r', prefix, cursor=cursor, limit=limit)  # type: ignore[arg-type]
        seen += [(e.kind, e.name) for e in page.entries]
        if page.next_cursor is None:
            return seen
        assert len(page.entries) == limit
        cursor = page.next_cursor
    raise AssertionError('paging did not end')


segment = st.sampled_from(['a', 'a-b', 'a.txt', 'b', 'b0', 'c', 'z'])
path = st.lists(segment, min_size=1, max_size=4).map('/'.join)


@settings(max_examples=150, deadline=None)
@given(paths=st.lists(path, min_size=1, max_size=60), limit=st.integers(1, 7), folder=st.sampled_from(['', 'a/']))
async def test_pages_match_a_brute_force_listing(paths: list[str], limit: int, folder: str) -> None:
    want = expected(paths, folder)
    if not want:
        return
    assert await walk(Db(paths), folder, limit) == want


async def test_many_small_sub_folders_take_a_few_queries() -> None:
    """``group/pkg/<version>/<file>``: 100 one-file sub-folders. The walk used to seek once per sub-folder (51
    queries for a page of 50, and a second walk for the files)."""
    db = Db([f'pkg/{v}.0.0/artifact-{v}.tar.gz' for v in range(100)] + ['pkg/README'])
    page = await list_folder(db, 'r', 'pkg', cursor=None, limit=50)  # type: ignore[arg-type]
    assert [e.name for e in page.entries][:3] == ['0.0.0', '1.0.0', '10.0.0']
    assert page.next_cursor is not None
    assert db.assets.queries <= 4
    db.assets.queries = 0
    page = await list_folder(db, 'r', 'pkg', cursor=None, limit=200)  # type: ignore[arg-type]
    assert len(page.entries) == 101 and page.entries[-1].name == 'README' and page.entries[-1].size == len('pkg/README')
    assert page.next_cursor is None
    assert db.assets.queries <= 5


async def test_big_sub_folders_cost_one_seek_each() -> None:
    db = Db([f'g{g}/f{n:03d}' for g in range(10) for n in range(500)])
    children = [c.name async for c in browse_db.children(db, 'r', '')]  # type: ignore[arg-type]
    assert children == [f'g{g}' for g in range(10)]
    assert db.assets.queries <= 12
