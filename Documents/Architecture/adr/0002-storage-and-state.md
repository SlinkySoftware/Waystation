# ADR 0002: One content-addressed blob path; MongoDB as record; Valkey as ephemeral coordination

Status: accepted (2026-11-08, approved by the owner).

## Context
The owner wants a single server path for cached files that can be externalised (e.g. to a NAS), and MongoDB in
the stack. Live activity, request coalescing across replicas, rate counters and a job queue need fast shared state.

## Decision
- **Blob store**: one directory (`WAYSTATION_DATA_DIR`), sha256 content-addressed, global de-duplication across
  repos and formats, atomic rename commits, uploads staged on the same filesystem. No other state on that path
  except encrypted signing keys. Works on local disk and NFS/SMB.
- **MongoDB** (bundled single-node replica set by default, external supported): configuration, metadata,
  identity, audit, job history, and stats in native time-series collections. Replica set mode enables change
  streams (cache invalidation across replicas) and transactions (hosted index swaps).
- **Valkey**: locks/leases (single-flight fills, scheduler leader), negative and hot-metadata caches, per-minute
  counters, live transfer table, live pub/sub and the activity stream for SSE, and the job queue (Streams +
  consumer groups). Runs with `maxmemory-policy noeviction` and AOF (`everysec`); correctness never depends on it:
  event-driven jobs are also recorded in a MongoDB outbox and re-dispatched after a Valkey loss.

## Consequences
- Backups = `mongodump` + the blob directory (hosted-only option). Losing Valkey loses only in-flight live stats;
  jobs are re-created from schedules and the outbox.
- An S3 blob driver can be added behind `BlobStore` later (serving via presigned redirects instead of X-Accel).
