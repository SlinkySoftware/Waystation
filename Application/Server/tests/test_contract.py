"""Contract test: the server's generated OpenAPI matches the contract-first M0 document (TASKS T-V01).

The contract lives with the UI (``Application/Web/openapi/waystation-m0.yaml``; override with
``WAYSTATION_OPENAPI_CONTRACT``). For every operation the server implements, compare paths, methods, parameter
names, success status codes and the *shape* of request and response bodies (property names, required properties,
types, enums, arrays, unions), after resolving ``$ref``s on both sides. Read-only properties are ignored in requests
and write-only ones in responses; formats, titles and descriptions are ignored. A response must provide at least the
contract's required properties; a request may not require more than the contract.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from waystation_server.app import create_app

ROOT = Path(__file__).resolve().parents[3]
CONTRACT = Path(os.environ.get('WAYSTATION_OPENAPI_CONTRACT') or ROOT / 'Application/Web/openapi/waystation-m0.yaml')
PREFIX = '/api/v1'

# Operations the server implements (grows with each task). Every one must match the contract.
IMPLEMENTED: set[tuple[str, str]] = {
    ('/config', 'get'),
    ('/auth/providers', 'get'),
    ('/auth/login', 'post'),
    ('/auth/logout', 'post'),
    ('/auth/me', 'get'),
    ('/auth/me/preferences', 'put'),
    ('/auth-providers', 'get'),
    ('/auth-providers', 'post'),
    ('/auth-providers/test-connection', 'post'),
    ('/auth-providers/test-user', 'post'),
    ('/auth-providers/{id}', 'get'),
    ('/auth-providers/{id}', 'put'),
    ('/auth-providers/{id}', 'delete'),
    ('/roles', 'get'),
    ('/roles', 'post'),
    ('/roles/{role_id}', 'get'),
    ('/roles/{role_id}', 'put'),
    ('/roles/{role_id}', 'delete'),
    ('/anonymous-access', 'get'),
    ('/anonymous-access', 'put'),
    ('/auth/me', 'patch'),
    ('/auth/me/password', 'post'),
    ('/auth/me/permissions', 'get'),
    ('/auth/sessions', 'get'),
    ('/auth/sessions', 'delete'),
    ('/auth/sessions/{session_id}', 'delete'),
    ('/users', 'get'),
    ('/users', 'post'),
    ('/users/{user_id}', 'get'),
    ('/users/{user_id}', 'patch'),
    ('/users/{user_id}', 'delete'),
    ('/users/{user_id}/password', 'post'),
    ('/users/{user_id}/unlock', 'post'),
    ('/users/{user_id}/totp', 'delete'),
    ('/auth/login/totp', 'post'),
    ('/auth/me/totp', 'get'),
    ('/auth/me/totp/setup', 'post'),
    ('/auth/me/totp/confirm', 'post'),
    ('/auth/me/totp/recovery-codes', 'post'),
    ('/auth/me/totp/disable', 'post'),
    ('/users/{user_id}/permissions', 'get'),
    ('/groups', 'get'),
    ('/groups', 'post'),
    ('/groups/{group_id}', 'get'),
    ('/groups/{group_id}', 'patch'),
    ('/groups/{group_id}', 'delete'),
    ('/groups/{group_id}/members', 'get'),
    ('/groups/{group_id}/members', 'post'),
    ('/groups/{group_id}/members/{user_id}', 'delete'),
    ('/service-accounts/{account_id}', 'get'),
    ('/service-accounts/{account_id}/permissions', 'get'),
    ('/auth/me/tokens', 'get'),
    ('/auth/me/tokens', 'post'),
    ('/auth/me/tokens/{token_id}', 'delete'),
    ('/tokens', 'get'),
    ('/tokens/{token_id}', 'delete'),
    ('/service-accounts', 'get'),
    ('/service-accounts', 'post'),
    ('/service-accounts/{account_id}', 'patch'),
    ('/service-accounts/{account_id}', 'delete'),
    ('/users/{user_id}/sessions', 'get'),
    ('/users/{user_id}/sessions', 'delete'),
    ('/settings', 'get'),
    ('/settings', 'put'),
    ('/system', 'get'),
    ('/service-accounts/{account_id}/tokens', 'get'),
    ('/service-accounts/{account_id}/tokens', 'post'),
    ('/service-accounts/{account_id}/tokens/{token_id}', 'delete'),
    ('/repositories', 'get'),
    ('/repositories', 'post'),
    ('/repositories/test-upstream', 'post'),
    ('/repositories/{name}', 'get'),
    ('/repositories/{name}', 'put'),
    ('/repositories/{name}', 'delete'),
    ('/repositories/{name}/setup', 'get'),
    ('/import/nexus/preview', 'post'),
    ('/import/nexus/apply', 'post'),
    ('/repositories/{name}/stats/traffic', 'get'),
    ('/repositories/{name}/browse', 'get'),
    ('/repositories/{name}/npm/packages', 'get'),
    ('/repositories/{name}/npm/versions', 'get'),
    ('/repositories/{name}/pypi/projects', 'get'),
    ('/repositories/{name}/pypi/versions', 'get'),
    ('/repositories/{name}/pypi/yank', 'post'),
    ('/repositories/{name}/go/modules', 'get'),
    ('/repositories/{name}/go/versions', 'get'),
    ('/repositories/{name}/apt/suites', 'get'),
    ('/repositories/{name}/apt/packages', 'get'),
    ('/repositories/{name}/docker/images', 'get'),
    ('/repositories/{name}/docker/tags', 'get'),
    ('/repositories/{name}/cleanup', 'get'),
    ('/repositories/{name}/cleanup', 'put'),
    ('/cleanup-policies', 'get'),
    ('/cleanup-policies', 'post'),
    ('/cleanup-policies/preview', 'post'),
    ('/cleanup-policies/{policy_id}', 'get'),
    ('/cleanup-policies/{policy_id}', 'put'),
    ('/cleanup-policies/{policy_id}', 'delete'),
    ('/cleanup-policies/{policy_id}/run', 'post'),
    ('/cleanup-policies/{policy_id}/runs', 'get'),
    ('/maintenance/gc', 'get'),
    ('/maintenance/gc/run', 'post'),
    ('/tasks/scheduled', 'get'),
    ('/tasks/scheduled/{task_id}', 'patch'),
    ('/tasks/scheduled/{task_id}/run', 'post'),
    ('/tasks/runs', 'get'),
    ('/tasks/runs/{run_id}', 'get'),
    ('/tasks/workers', 'get'),
    ('/tasks/dead-letters', 'get'),
    ('/tasks/dead-letters/{letter_id}', 'delete'),
    ('/tasks/dead-letters/{letter_id}/retry', 'post'),
    ('/webhooks', 'get'),
    ('/webhooks', 'post'),
    ('/webhooks/{webhook_id}', 'get'),
    ('/webhooks/{webhook_id}', 'put'),
    ('/webhooks/{webhook_id}', 'delete'),
    ('/webhooks/{webhook_id}/test', 'post'),
    ('/webhooks/{webhook_id}/deliveries', 'get'),
    ('/webhooks/{webhook_id}/deliveries/{delivery_id}', 'get'),
    ('/webhooks/{webhook_id}/deliveries/{delivery_id}/redeliver', 'post'),
    ('/repositories/{name}/docker/manifest', 'get'),
    ('/repositories/{name}/maven/artifacts', 'get'),
    ('/repositories/{name}/maven/versions', 'get'),
    ('/search', 'get'),
    ('/stats/summary', 'get'),
    ('/stats/traffic', 'get'),
    ('/stats/storage', 'get'),
    ('/storage/guard', 'get'),
    ('/storage/guard', 'put'),
    ('/stats/upstreams', 'get'),
    ('/transfers', 'get'),
    ('/activity', 'get'),
    ('/activity/export', 'get'),
    ('/events/stream', 'get'),
    ('/audit', 'get'),
    ('/audit/export', 'get'),
}

# Deliberate, reviewed differences: (path, method, where) -> reason. Keep this short.
KNOWN_DIFFERENCES: dict[tuple[str, str, str], str] = {}

Shape = Any


class Doc:
    def __init__(self, doc: dict[str, Any]) -> None:
        self.doc = doc

    def resolve(self, node: Any) -> Any:
        seen = 0
        while isinstance(node, dict) and '$ref' in node:
            ref = node['$ref']
            assert ref.startswith('#/'), ref
            target: Any = self.doc
            for part in ref[2:].split('/'):
                target = target[part]
            node = target
            seen += 1
            assert seen < 50, 'reference loop'
        return node

    def shape(self, schema: Any, mode: str, depth: int = 0) -> Shape:
        schema = self.resolve(schema)
        if depth > 30 or not isinstance(schema, dict):
            return 'any'
        if 'allOf' in schema and len(schema['allOf']) == 1:
            return self.shape(schema['allOf'][0], mode, depth + 1)
        for key in ('anyOf', 'oneOf'):
            if key in schema:
                variants = [self.resolve(v) for v in schema[key]]
                real = [v for v in variants if not (isinstance(v, dict) and v.get('type') == 'null')]
                unique = {repr(s): s for s in (self.shape(v, mode, depth + 1) for v in real)}
                if len(unique) == 1:
                    return next(iter(unique.values()))
                return ('union', tuple(sorted(unique.values(), key=_union_key)))
        if 'const' in schema:
            return ('const', schema['const'])
        if 'enum' in schema:
            return ('enum', tuple(sorted(str(v) for v in schema['enum'] if v is not None)))
        kind = schema.get('type')
        if isinstance(kind, list):
            kinds = [k for k in kind if k != 'null']
            kind = kinds[0] if len(kinds) == 1 else tuple(sorted(kinds))
        if kind == 'object' or 'properties' in schema:
            skip = 'readOnly' if mode == 'request' else 'writeOnly'
            props = {
                name: self.shape(sub, mode, depth + 1)
                for name, sub in schema.get('properties', {}).items()
                if not self.resolve(sub).get(skip)
            }
            if not props and isinstance(schema.get('additionalProperties'), dict):
                return ('map', self.shape(schema['additionalProperties'], mode, depth + 1))
            if not props and 'properties' not in schema:
                return ('map', 'any')
            required = tuple(sorted(r for r in schema.get('required', []) if r in props))
            return ('object', tuple(sorted(props.items())), required)
        if kind == 'array':
            return ('array', self.shape(schema.get('items', {}), mode, depth + 1))
        if kind in ('string', 'integer', 'number', 'boolean'):
            return kind
        return 'any'


def _union_key(shape: Shape) -> str:
    """Order union variants by their ``type`` constant (discriminated unions), else by their structure."""
    if isinstance(shape, tuple) and shape[0] == 'object':
        props = dict(shape[1])
        kind = props.get('type')
        if isinstance(kind, tuple) and kind[0] == 'const':
            return f'0:{kind[1]}'
        return f'1:{sorted(props)}'
    return f'2:{shape!r}'


def compare(contract: Shape, server: Shape, mode: str, where: str, out: list[str]) -> None:
    if contract == server:
        return
    if isinstance(contract, tuple) and isinstance(server, tuple) and contract[0] == server[0] == 'union':
        if len(contract[1]) != len(server[1]):
            out.append(f'{where}: union of {len(contract[1])} in the contract, {len(server[1])} on the server')
            return
        for i, (c, s) in enumerate(zip(contract[1], server[1], strict=True)):
            compare(c, s, mode, f'{where}<{_union_key(c)[2:] or i}>', out)
        return
    if isinstance(contract, tuple) and isinstance(server, tuple) and contract[0] == server[0] == 'object':
        c_props, s_props = dict(contract[1]), dict(server[1])
        for name in sorted(set(c_props) - set(s_props)):
            out.append(f'{where}: server lacks property {name!r}')
        for name in sorted(set(s_props) - set(c_props)):
            out.append(f'{where}: server has extra property {name!r}')
        for name in sorted(set(c_props) & set(s_props)):
            compare(c_props[name], s_props[name], mode, f'{where}.{name}', out)
        c_req, s_req = set(contract[2]), set(server[2])
        if mode == 'response' and not c_req <= s_req:
            out.append(f'{where}: server does not guarantee {sorted(c_req - s_req)}')
        if mode == 'request' and not s_req <= c_req:
            out.append(f'{where}: server requires {sorted(s_req - c_req)} beyond the contract')
        return
    if isinstance(contract, tuple) and isinstance(server, tuple) and contract[0] == server[0] == 'array':
        compare(contract[1], server[1], mode, f'{where}[]', out)
        return
    if isinstance(contract, tuple) and isinstance(server, tuple) and contract[0] == server[0] == 'map':
        compare(contract[1], server[1], mode, f'{where}{{}}', out)
        return
    if contract == 'any' or server == 'any':
        return
    if {contract, server} <= {'integer', 'number'}:
        return
    out.append(f'{where}: contract {contract!r} != server {server!r}')


def _body(doc: Doc, node: Any) -> Any:
    node = doc.resolve(node)
    content = node.get('content', {}) if isinstance(node, dict) else {}
    for media in ('application/json', 'text/event-stream'):
        if media in content:
            return content[media].get('schema', {})
    return None


def _params(doc: Doc, op: dict[str, Any], path_item: dict[str, Any]) -> set[str]:
    names = set()
    for param in [*path_item.get('parameters', []), *op.get('parameters', [])]:
        param = doc.resolve(param)
        if param['in'] in ('query', 'path'):
            names.add(f'{param["in"]}:{param["name"]}')
    return names


@pytest.fixture(scope='module')
def docs() -> tuple[Doc, Doc]:
    if not CONTRACT.exists():
        pytest.skip(f'contract not present: {CONTRACT} (merged from the UI lane)')
    contract = yaml.safe_load(CONTRACT.read_text(encoding='utf-8'))
    server = create_app().openapi()
    return Doc(contract), Doc(server)


def test_implemented_operations_are_in_the_contract(docs: tuple[Doc, Doc]) -> None:
    contract, _ = docs
    for path, method in IMPLEMENTED:
        assert method in contract.doc['paths'].get(path, {}), f'{method.upper()} {path} is not in the contract'


@pytest.mark.parametrize(('path', 'method'), sorted(IMPLEMENTED))
def test_operation_matches_contract(docs: tuple[Doc, Doc], path: str, method: str) -> None:
    contract, server = docs
    c_item = contract.doc['paths'][path]
    s_item = server.doc['paths'].get(PREFIX + path)
    assert s_item is not None and method in s_item, f'server lacks {method.upper()} {PREFIX}{path}'
    c_op, s_op = c_item[method], s_item[method]
    problems: list[str] = []
    missing = _params(contract, c_op, c_item) - _params(server, s_op, s_item)
    if missing:
        problems.append(f'parameters missing on the server: {sorted(missing)}')
    c_req, s_req = c_op.get('requestBody'), s_op.get('requestBody')
    if c_req is not None:
        if s_req is None:
            problems.append('server has no request body')
        else:
            compare(
                contract.shape(_body(contract, c_req), 'request'),
                server.shape(_body(server, s_req), 'request'),
                'request',
                'body',
                problems,
            )
    c_ok = {code for code in c_op['responses'] if code.startswith('2')}
    s_ok = {code for code in s_op['responses'] if code.startswith('2')}
    if c_ok != s_ok:
        problems.append(f'success codes: contract {sorted(c_ok)} != server {sorted(s_ok)}')
    for code in sorted(c_ok & s_ok):
        c_schema = _body(contract, c_op['responses'][code])
        s_schema = _body(server, s_op['responses'][code])
        if c_schema is None:
            continue
        if s_schema is None:
            problems.append(f'{code}: server documents no body')
            continue
        compare(contract.shape(c_schema, 'response'), server.shape(s_schema, 'response'), 'response', code, problems)
    allowed = {reason for (p, m, _), reason in KNOWN_DIFFERENCES.items() if (p, m) == (path, method)}
    problems = [
        p for p in problems if not any(p.startswith(k[2]) for k in KNOWN_DIFFERENCES if k[:2] == (path, method))
    ]
    assert not problems, '\n'.join([f'{method.upper()} {path}:', *problems, *allowed])
