"""The committed REST API reference (``Documents/API/``) matches the server's OpenAPI document (``make api-docs``)."""

from __future__ import annotations

from pathlib import Path

import api_docs

ROOT = Path(__file__).resolve().parents[3]


def test_api_reference_is_current() -> None:
    files = api_docs.render(api_docs.build_document())
    assert api_docs.stale_files(ROOT / api_docs.OUT_DIR, files) == [], 'run `make api-docs` and commit Documents/API/'


def test_reference_lists_every_operation() -> None:
    doc = api_docs.build_document()
    markdown = api_docs.render_markdown(doc)
    for path, item in doc['paths'].items():
        for method in api_docs.METHODS:
            if method in item:
                assert f'### `{method.upper()} {path}`' in markdown
    assert doc['info']['version'] == api_docs.API_VERSION


def test_type_rendering() -> None:
    assert api_docs.type_of({'$ref': '#/components/schemas/Repo'}) == '[`Repo`](#schema-repo)'
    assert api_docs.type_of({'anyOf': [{'type': 'string'}, {'type': 'null'}]}) == 'string \\| null'
    assert api_docs.type_of({'type': 'array', 'items': {'type': 'integer'}}) == 'array of integer'
    assert api_docs.type_of({'type': 'object', 'additionalProperties': {'type': 'string'}}) == 'object of string'
    assert api_docs.type_of({'enum': ['a', 'b']}) == '`"a"` \\| `"b"`'
    assert api_docs.type_of(None) == 'any'
