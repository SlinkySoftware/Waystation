"""SSRF guard (SPECIFICATION §4.1): the implementation lives in ``waystation_common.ssrf`` so the worker's outbound
calls (webhook deliveries, T-O05) apply the same rules. Re-exported here for the proxy engine and its callers."""

from __future__ import annotations

from waystation_common.ssrf import GuardedBackend, SsrfGuard, UpstreamBlocked

__all__ = ['GuardedBackend', 'SsrfGuard', 'UpstreamBlocked']
