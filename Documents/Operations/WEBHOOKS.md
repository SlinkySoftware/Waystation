# Webhooks

Waystation can call your systems when something happens to stored content: CI starts a deployment when a release
is published, a chat channel hears about every image pushed, an inventory learns about deletions. Each call is an
HTTP `POST` of a JSON document, signed with a secret only you and Waystation know, and retried until your receiver
accepts it.

Set them up under **Administration → Webhooks** (administrators only) or through the API
(`/api/v1/webhooks`, OpenAPI tag `webhooks`). Monitoring: [MONITORING.md](MONITORING.md).

## 1. Events

| Event | When |
|---|---|
| `content.upload` | An image manifest pushed, a package or file published to a hosted repository (docker, raw, PyPI, Maven, Helm...) |
| `content.delete` | A tag, manifest, blob, package or file deleted from a hosted repository |
| `content.update` | Stored content changed in place (a PyPI release yanked or un-yanked) |
| `repository.create` | A repository was created |
| `repository.update` | A repository's settings changed |
| `repository.delete` | A repository was deleted |
| `webhook.ping` | **Send test event** (always delivered, whatever the filter, even while the webhook is off) |

Events are the audit records of the same actions (SPECIFICATION §4.11), so anything that shows up as an upload or
a delete in the audit log is also a webhook event. A webhook subscribes to event names, whole categories
(`content.*`, `repository.*`) or everything (`*`, which also includes events added later).

**Repository scope**: a list of repository names or shell-style patterns (`docker-*`, `pypi-?`), matched
case-insensitively. Empty means every repository.

## 2. The request

```http
POST /hooks/waystation HTTP/1.1
Host: ci.example.com
Content-Type: application/json
User-Agent: Waystation-Webhooks (+https://github.com/SlinkySoftware/Waystation)
Waystation-Event: content.upload
Waystation-Event-Id: 01JD2Q6V7K3M9X4R8T5W0Y1Z2A
Waystation-Delivery: 01JD2Q6V7K3M9X4R8T5W0Y1Z2A-01JD2P0000000000000000000W
Idempotency-Key: 01JD2Q6V7K3M9X4R8T5W0Y1Z2A-01JD2P0000000000000000000W
Waystation-Attempt: 1
Waystation-Timestamp: 1762761600
Waystation-Signature: t=1762761600,v1=5f0c3c4b...

{"id":"01JD2Q6V7K3M9X4R8T5W0Y1Z2A","type":"content.upload","occurred_at":"2026-11-10T08:00:00.123456Z",
 "repository":"raw-releases","actor":{"kind":"service_account","name":"release-bot"},
 "target":{"kind":"raw_file","id":"01JD2Q6V...","name":"raw-releases/tools/cli-1.18.0.tar.gz"},
 "details":{"repository":"raw-releases","path":"tools/cli-1.18.0.tar.gz","size":18311022,"sha256":"..."},
 "source":"https://registry.example.com"}
```

- `id` / `Waystation-Event-Id`: the event. It stays the same when a delivery is sent again.
- `Waystation-Delivery` and `Idempotency-Key`: one delivery (one event to one webhook). Every retry of it carries
  the same value, so a receiver that processed it once can ignore repeats. A manual **Redeliver** from the log is a
  new delivery (new id, same event id and body).
- `details` are the audit record's details (format-specific: image, tag, digest; project, version, file name;
  path, size, sha256...); `repository.update` also carries `changes` (field, before, after; secrets are never
  included). Secrets never appear in an event.
- Bodies are at most 256 KiB; a larger event is sent without its details (`"details":{"truncated":true}`).
- Custom headers you configure (for example `Authorization` for a receiver that wants a token) are added to every
  request. Their values, like the secret, are stored encrypted and never shown again.

## 3. Verifying the signature

`Waystation-Signature: t=<unix seconds>,v1=<hex>`, where `<hex>` is HMAC-SHA256, keyed with the webhook's secret,
over the timestamp, a dot, and the raw request body bytes: `HMAC_SHA256(secret, "<t>." + body)`. Each attempt is
signed afresh with the time it was sent.

A receiver should:

1. read the **raw** body (before any JSON parsing or re-encoding);
2. recompute the HMAC and compare it with every `v1=` value **in constant time**;
3. refuse a timestamp more than 5 minutes from its own clock: a captured request cannot be replayed later;
4. then answer `2xx` quickly (do the work afterwards); deduplicate on `Idempotency-Key`.

Python (standard library only):

```python
import hashlib
import hmac
import time


def verify(secret: str, body: bytes, header: str, tolerance_s: int = 300) -> bool:
    fields = [part.strip().partition('=') for part in header.split(',')]
    timestamps = [value for key, _, value in fields if key == 't' and value.isdigit()]
    signatures = [value for key, _, value in fields if key == 'v1']
    if not timestamps or not signatures or abs(time.time() - int(timestamps[0])) > tolerance_s:
        return False
    expected = hmac.new(secret.encode(), f'{timestamps[0]}.'.encode() + body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in signatures)
```

Node.js:

```js
const crypto = require('node:crypto')

function verify(secret, rawBody, header, toleranceS = 300) {
  const parts = Object.fromEntries(header.split(',').map((p) => p.trim().split('=', 2)))
  const t = Number(parts.t)
  if (!Number.isInteger(t) || !parts.v1 || Math.abs(Date.now() / 1000 - t) > toleranceS) return false
  const expected = crypto.createHmac('sha256', secret).update(`${t}.`).update(rawBody).digest()
  const given = Buffer.from(parts.v1, 'hex')
  return given.length === expected.length && crypto.timingSafeEqual(given, expected)
}
```

The same function is `waystation_common.webhooks.verify` in the source tree (used by the tests).

**Secrets**: at least 16 characters. The editor generates a 64-character random secret in the browser; copy it
before saving, it is never shown again. To rotate, set a new secret on the webhook and the receiver at about the
same time (deliveries in between fail and are retried).

## 4. Delivery, retries and the log

- An event is matched against the enabled webhooks when it happens. If any wants it, it is written to the durable
  job outbox (MongoDB) and a worker sends it within about a second. Events that no webhook wants cost nothing.
- **At least once**: a delivery is recorded before it is attempted; an attempt holds a lease so no two workers send
  it at the same time; a worker that dies mid-attempt only delays it until the retry sweep (every 15 s) finds it.
- **Success** is any `2xx` answer. Redirects are not followed (a `3xx` is a failure). Anything else (an error
  status, a refused connection, a TLS failure, no answer within the timeout) is retried after 30 s, then 1, 2, 4...
  minutes (±10 %), at most an hour apart, until the webhook's **attempts** (default 8, 1 to 20) are used up. The
  delivery is then **failed** (dead); redeliver it from the log once the receiver is fixed.
- **Timeout**: per webhook, 1 to 30 s (default 10) for the whole attempt.
- **Test events** are attempted once, so the result shows at once.
- A webhook that is **off** gets no new deliveries; ones already waiting for a retry end as failed with the reason.
  Deleting a webhook deletes its log.
- The **delivery log** keeps 30 days: status, each attempt's HTTP status, latency, error and the first KiB of the
  receiver's answer, the exact JSON document and headers (custom header values masked).

## 5. Network safety (SSRF guard)

Deliveries go through the same guard as proxy upstreams (SPECIFICATION §4.1): the address that was checked is the
address that is connected to, every resolved address must be public, and a refused target fails at once without
retries ("refused: the target resolves to a private or reserved address"). A receiver on a private, loopback or
link-local address needs **Allow private network targets** on that webhook, or its network in
`WAYSTATION_SSRF_ALLOW_CIDRS` (which applies to upstreams too). A URL with a literal private address is refused
when saving unless the webhook allows it. Credentials in the URL (`https://user:pass@...`) are refused: use a
custom header.

Outbound proxy: when `WAYSTATION_HTTP_PROXY` is set, deliveries use it too (hosts in `WAYSTATION_NO_PROXY` go
direct); the guard then checks the target's addresses before each request. `WAYSTATION_EXTRA_CA_FILE` is trusted
for HTTPS receivers.

## 6. Audit and metrics

Changes are audited as `webhook.create`, `webhook.update` (field changes; a new secret shows as changed, never its
value), `webhook.delete`, `webhook.test` and `webhook.redeliver`. On `/metrics`:
`waystation_webhook_deliveries_total{outcome="succeeded|failed|dead"}` and `waystation_webhook_deliveries_pending`.
Alert on a growing `dead` count, or on `pending` staying high (a receiver down for a long time).
