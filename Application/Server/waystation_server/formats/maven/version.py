"""Maven version ordering: a port of Maven's ``ComparableVersion`` (maven-artifact 3.6-3.9 rules).

A version is parsed into a tree of items: integers, string qualifiers and sub-lists. ``.`` separates items, ``-``
(and a switch between letters and digits) starts a sub-list. Trailing "null" items (``0``, ``ga``/``final``/
``release``, empty lists) are dropped, so ``1.0.0`` equals ``1``. Qualifiers order as
``alpha < beta < milestone < rc = cr < snapshot < (release) < sp``; unknown qualifiers come after those, by text;
``a1``/``b1``/``m1`` are ``alpha-1``/``beta-1``/``milestone-1``. An integer is greater than a list, which is greater
than a string.

``compare`` follows Maven exactly. ``sort_key`` is the index-friendly string form (``components.version_sort``): it
orders like ``compare`` for every ordinary version (numbers, qualifiers, snapshots, timestamps, build numbers); Maven's
order is not transitive for a few odd mixes (``1.0.alpha`` against ``1.alpha`` and ``1.sp``), where the key picks one
consistent order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cmp_to_key, lru_cache

QUALIFIERS = ('alpha', 'beta', 'milestone', 'rc', 'snapshot', '', 'sp')
ALIASES = {'ga': '', 'final': '', 'release': '', 'cr': 'rc'}
RELEASE_INDEX = str(QUALIFIERS.index(''))
MAX_KEY = 400


def _comparable_qualifier(value: str) -> str:
    try:
        return str(QUALIFIERS.index(value))
    except ValueError:
        return f'{len(QUALIFIERS)}-{value}'


@dataclass(frozen=True)
class IntItem:
    value: int

    def is_null(self) -> bool:
        return self.value == 0


@dataclass(frozen=True)
class StringItem:
    value: str

    @classmethod
    def of(cls, value: str, followed_by_digit: bool) -> StringItem:
        if followed_by_digit and len(value) == 1:
            value = {'a': 'alpha', 'b': 'beta', 'm': 'milestone'}.get(value, value)
        return cls(ALIASES.get(value, value))

    def is_null(self) -> bool:
        return _comparable_qualifier(self.value) == RELEASE_INDEX


@dataclass
class ListItem:
    items: list[Item] = field(default_factory=list)

    def is_null(self) -> bool:
        return not self.items

    def normalize(self) -> None:
        for i in range(len(self.items) - 1, -1, -1):
            last = self.items[i]
            if last.is_null():
                del self.items[i]
            elif not isinstance(last, ListItem):
                break


Item = IntItem | StringItem | ListItem


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


def _compare(left: Item, right: Item | None) -> int:
    """``left.compareTo(right)`` of ComparableVersion's items."""
    if isinstance(left, IntItem):
        if right is None:
            return 0 if left.value == 0 else 1
        if isinstance(right, IntItem):
            return _sign(left.value - right.value)
        return 1  # an int is greater than a string or a list
    if isinstance(left, StringItem):
        if right is None:
            mine = _comparable_qualifier(left.value)
            return (mine > RELEASE_INDEX) - (mine < RELEASE_INDEX)
        if isinstance(right, StringItem):
            a, b = _comparable_qualifier(left.value), _comparable_qualifier(right.value)
            return (a > b) - (a < b)
        return -1  # a string is less than an int or a list
    if right is None:
        return _compare(left.items[0], None) if left.items else 0
    if isinstance(right, IntItem):
        return -1
    if isinstance(right, StringItem):
        return 1
    for i in range(max(len(left.items), len(right.items))):
        lhs = left.items[i] if i < len(left.items) else None
        rhs = right.items[i] if i < len(right.items) else None
        result = (0 if rhs is None else -_compare(rhs, None)) if lhs is None else _compare(lhs, rhs)
        if result:
            return result
    return 0


def _item(digits: bool, text: str, followed_by_digit: bool = False) -> Item:
    if digits:
        return IntItem(int(text))
    return StringItem.of(text, followed_by_digit)


@lru_cache(maxsize=4096)
def _parse_cached(version: str) -> ListItem:
    return _parse(version)


def _parse(version: str) -> ListItem:
    version = version.lower()
    root = ListItem()
    current = root
    stack = [root]
    is_digit = False
    start = 0
    for i, c in enumerate(version):
        if c == '.':
            current.items.append(IntItem(0) if i == start else _item(is_digit, version[start:i]))
            start = i + 1
        elif c == '-':
            current.items.append(IntItem(0) if i == start else _item(is_digit, version[start:i]))
            start = i + 1
            sub = ListItem()
            current.items.append(sub)
            current = sub
            stack.append(sub)
        elif c.isdigit() and c.isascii():
            if not is_digit and i > start:
                current.items.append(StringItem.of(version[start:i], True))
                start = i
                sub = ListItem()
                current.items.append(sub)
                current = sub
                stack.append(sub)
            is_digit = True
        else:
            if is_digit and i > start:
                current.items.append(_item(True, version[start:i]))
                start = i
                sub = ListItem()
                current.items.append(sub)
                current = sub
                stack.append(sub)
            is_digit = False
    if len(version) > start:
        current.items.append(_item(is_digit, version[start:]))
    while stack:
        stack.pop().normalize()
    return root


def compare(a: str, b: str) -> int:
    """Maven's ordering of two versions: negative, zero or positive."""
    return _compare(_parse_cached(a), _parse_cached(b))


version_order = cmp_to_key(compare)


def sort_versions(versions: list[str]) -> list[str]:
    """Ascending in Maven order (ties: by text, so the result is deterministic)."""
    return sorted(sorted(set(versions)), key=version_order)


def is_snapshot(version: str) -> bool:
    return version.endswith('-SNAPSHOT')


def _encode(item: Item, out: list[str]) -> None:
    # Token order: low qualifiers "a" < list below release "b" < end "c" < high qualifiers "d" < list above "e"
    # < int "f".
    if isinstance(item, IntItem):
        digits = str(item.value)
        out.append(f'f{min(len(digits), 99):02d}{digits}')
    elif isinstance(item, StringItem):
        qualifier = _comparable_qualifier(item.value)
        if qualifier < RELEASE_INDEX:
            out.append('a' + qualifier)
        elif qualifier == RELEASE_INDEX:
            out.append('c')
        else:
            out.append('d' + qualifier + '!')
    else:
        out.append('b' if _compare(item, None) < 0 else 'e')
        for child in item.items:
            _encode(child, out)
        out.append('c')


def sort_key(version: str) -> str:
    """``components.version_sort``: a string whose byte order follows Maven's version order (bounded length)."""
    out: list[str] = []
    for item in _parse_cached(version).items:
        _encode(item, out)
    out.append('c')
    return ''.join(out)[:MAX_KEY]
