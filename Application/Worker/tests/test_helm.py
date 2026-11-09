"""Hosted Helm index generation (``waystation_worker.helm``, job ``index.helm``): the index built from records."""

import json
from datetime import UTC, datetime

from waystation_common.jobs import JOB_TYPES, HelmIndex
from waystation_worker.helm import build_index, rfc3339
from waystation_worker.jobs.registry import HANDLERS

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def test_build_index_from_records() -> None:
    docs = [
        {
            'name': 'demo',
            'version': '0.2.0',
            'path': 'demo-0.2.0.tgz',
            'digest': 'a' * 64,
            'created': datetime(2026, 1, 2, 3, 4, 5),  # naive from MongoDB: UTC
            'entry': {'apiVersion': 'v2', 'name': 'demo', 'version': '0.2.0', 'appVersion': '1.0'},
        },
        {'name': 'demo', 'version': '0.1.0', 'path': 'demo-0.1.0.tgz', 'entry': {'apiVersion': 'v2'}},
        {'name': 'other', 'version': '1.0.0', 'path': 'other-1.0.0.tgz', 'digest': 'b' * 64},
    ]
    body, charts, versions = build_index(docs, NOW)
    index = json.loads(body)
    assert (charts, versions) == (2, 3)
    assert index['apiVersion'] == 'v1' and index['generated'] == '2026-11-10T12:00:00Z'
    first = index['entries']['demo'][0]
    assert first == {
        'apiVersion': 'v2',
        'name': 'demo',
        'version': '0.2.0',
        'appVersion': '1.0',
        'urls': ['demo-0.2.0.tgz'],
        'digest': 'a' * 64,
        'created': '2026-01-02T03:04:05Z',
    }
    assert 'digest' not in index['entries']['demo'][1] and 'created' not in index['entries']['demo'][1]
    assert json.loads(build_index([], NOW)[0])['entries'] == {}


def test_job_is_registered() -> None:
    assert JOB_TYPES['index.helm'] is HelmIndex
    assert HelmIndex(repo_id='r1').dedupe_key() == 'index.helm:r1'
    assert HelmIndex.queue == 'index'
    assert 'index.helm' in HANDLERS
    assert rfc3339(datetime(2026, 1, 1, tzinfo=UTC)) == '2026-01-01T00:00:00Z'
