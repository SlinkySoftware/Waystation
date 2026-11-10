"""Planning or applying the file outside a request (``init``, ``python -m waystation_server.provisioning``).

Builds the server's ``Services`` without starting their background tasks, except the audit writer and the webhook
notifier, so the changes are audited (actor ``system``) and repository changes still reach webhooks. Needs MongoDB
and Valkey (repository updates reset the upstream breaker; account changes revoke cached tokens on every replica).
"""

from __future__ import annotations

import os

from waystation_common.audit import SYSTEM
from waystation_common.db.client import Client
from waystation_common.settings import Settings
from waystation_server.provisioning import engine, loader
from waystation_server.provisioning.report import ProvisioningPlan
from waystation_server.security.principal import Principal, PrincipalKind
from waystation_server.services import Services

PRINCIPAL = Principal(PrincipalKind.USER, 'provisioning', 'provisioning', roles=('admin',))


async def run(
    settings: Settings,
    path: str | os.PathLike[str],
    *,
    apply: bool,
    by: str,
    mongo_client: Client | None = None,
) -> ProvisioningPlan:
    svc = Services.create(settings, mongo_client=mongo_client, check_schema_on_start=False)
    svc.audit.start()
    svc.webhooks.start()
    try:
        ctx = engine.Context(svc=svc, principal=PRINCIPAL, by=by, actor=SYSTEM, writer=svc.audit, notifier=svc.webhooks)
        loaded = await loader.load(path)
        return await (engine.apply if apply else engine.plan)(ctx, loaded)
    finally:
        await svc.webhooks.aclose()
        await svc.audit.aclose()
        await svc.upstream.aclose()
        await svc.valkey.aclose()
        if mongo_client is None and svc.mongo_client is not None:
            await svc.mongo_client.close()


def render(result: ProvisioningPlan) -> str:
    """A plan or apply result as text (the CLI and init's log)."""
    lines = [f'provisioning file {result.file} (sha256 {result.sha256 or "-"})']
    for problem in result.errors:
        lines.append(f'  error  {problem.field}: {problem.message}')
    for item in result.items:
        outcome = f' [{item.outcome}{": " + item.error if item.error else ""}]' if item.outcome else ''
        note = f' ({item.note})' if item.note else ''
        lines.append(f'  {item.action:<9} {item.kind} {item.name}{note}{outcome}')
        if item.action != 'unchanged':
            for change in item.changes:
                if change.redacted:
                    lines.append(f'      {change.field}: (secret changed)')
                else:
                    lines.append(f'      {change.field}: {change.before!r} -> {change.after!r}')
    s = result.summary
    state = 'applied' if result.applied else 'invalid: nothing applied' if not result.valid else 'plan only'
    lines.append(
        f'  {state}: {s.create} create, {s.update} update, {s.unchanged} unchanged, {s.prune} prune, {s.failed} failed'
    )
    return '\n'.join(lines) + '\n'
