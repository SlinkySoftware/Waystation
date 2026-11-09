# Backing up and restoring Waystation

How to back up a Waystation installation, restore it on the same or a new host, and check afterwards that every
asset still has its content. Examples use the single-host stack of [DEPLOYMENT.md](DEPLOYMENT.md) installed in
`/srv/waystation`, and `/mnt/backup/waystation` as the backup target (a local disk or a NAS mount).

## 1. What to back up

| What | Where | How | Can you live without it? |
|---|---|---|---|
| **Env file and secret files** | `/srv/waystation/waystation.env`, `/srv/waystation/secrets/` | your usual host backup (they hold `WAYSTATION_SECRET_KEYS`) | **No.** Without `WAYSTATION_SECRET_KEYS` the encrypted fields in the database (upstream credentials, signing keys, authentication provider secrets, webhook secrets) cannot be read |
| **MongoDB** (configuration, users, metadata, stats) | `waystation_mongodb` volume | `waystation-backup.sh backup`: `mongodump --archive --gzip` | **No** |
| **Blob store** `blobs/` (file content) | `waystation_data` volume | `waystation-backup.sh backup`, after the dump | Hosted content: **no**. Cached proxy content: yes, it is re-fetched from upstreams (`--hosted-only`) |
| `keys/` (repository signing keys, encrypted) | `waystation_data` volume | copied with every backup | No (signing keys) |
| `uploads/` (transfers in progress) | `waystation_data` volume | not backed up | Yes |
| Valkey | `waystation_valkey` volume | not backed up | Yes: it holds live state only; job schedules and the job outbox are in MongoDB |

Back up the env file and the secret files separately from the data backups, and keep a copy of
`WAYSTATION_SECRET_KEYS` somewhere safe and offline: a data backup without it is only partly usable.

## 2. How a backup stays consistent

Waystation stores content as immutable, content-addressed blob files and everything else in MongoDB. A backup is
taken **database first, blobs second**:

1. `mongodump --archive --gzip` of the database (online, no downtime);
2. then a copy of the blob files.

Every blob the dump references still exists when it is copied: blobs are never modified, and blob GC deletes a blob
only after it has been unreferenced for its grace period (24 hours by default), so a blob that was referenced at
dump time cannot disappear during a backup that takes less than a day. Files added after the dump are copied too
and are harmless (blob GC collects any the restored database does not reference). Writes during the dump itself
are captured or not; the dump of one database has no single point-in-time snapshot (`mongodump --oplog` only works
for whole instances), which is fine for a cache and acceptable for uploads that happen in the minute of the dump.

## 3. The backup tool

`Build/Containers/backup/waystation-backup.sh` (bash; copy it from the release or the checkout next to your
`compose.yaml`, like the compose file itself). It needs only **bash and Docker** on the host: the work runs in a
throwaway container of the `mongo:8` image (mongodump, mongorestore, mongosh, coreutils) that

- mounts the stack's data volume through `--volumes-from` the worker container (or server, or init) read-only for
  a backup, read-write for a restore;
- joins that container's networks, so it reaches MongoDB the way the application does (bundled or external);
- takes the MongoDB URI (`WAYSTATION_MONGO_URI`, or `WAYSTATION_MONGO_URI_FILE` through the same mount) and the
  database name from that container's environment. The URI is passed through a mode-0600 env file and a tmpfs
  config file, never on a command line.

From a repository checkout the same commands are `make backup`, `make restore` and `make verify-blobs` (below).

| Option | Meaning |
|---|---|
| `--prefix NAME` | Container name prefix: `waystation` (default, `compose.yaml`), `waystation-dev` for `make dev-up` |
| `--target DIR` | Backup target directory (backup, list) |
| `--hosted-only` | Copy only the blobs hosted repositories' assets reference |
| `--keep N` | After a successful backup, delete the oldest complete backups beyond N |
| `--no-hardlinks` | Copy every blob even when the previous backup has it |
| `--user UID:GID` | Run the backup as this user instead of root (NAS mounts that squash root; e.g. `10001:10001`) |
| `--from DIR` | The backup to restore |
| `--force` | Restore even though server, worker or web are running (not recommended) |
| `--keep-valkey` | Do not empty Valkey after a restore |
| `--db NAME` | MongoDB database (default: the stack's `WAYSTATION_MONGO_DB`, else `waystation`) |
| `--image IMAGE` | Tools image (default `mongo:8`) |
| `--timeout SECONDS` | How long `verify` waits for the result (default 3600) |

## 4. Taking a backup

```bash
cd /srv/waystation
sudo ./waystation-backup.sh backup --target /mnt/backup/waystation --keep 14
# from a checkout: make backup TARGET=/mnt/backup/waystation KEEP=14 [HOSTED_ONLY=1] [PREFIX=waystation]
```

Each run writes one directory, named by its UTC start time, and moves a `latest` link to it:

```text
/mnt/backup/waystation/
  waystation-20261110T023000Z/
    manifest.json
    mongodb.archive.gz
    blobs/sha256/aa/bb/<sha256>      same layout as the data directory
    keys/
  latest -> waystation-20261110T023000Z
```

The directory is written as `.waystation-<time>.partial` and renamed when complete, so an interrupted backup never
looks like a good one (the next run removes the leftover). Two runs into the same target cannot overlap (a lock file).
The backup's files are given the owner of the target directory.

**Incremental snapshots.** Blob files that the previous complete backup already holds are **hard-linked** from it
instead of copied: every backup directory is a full, independently restorable snapshot, but unchanged blobs take
no extra space and are not read again. Deleting an old backup (by hand or with `--keep`) never affects the others.
Where the target filesystem cannot hard-link (some SMB shares), the tool copies instead (`--no-hardlinks` forces it).

**Hosted-only backups.** `--hosted-only` copies only the blobs that assets of **hosted** repositories reference,
computed from MongoDB at backup time; the database dump is always complete. Cached proxy content is skipped: after a
restore the verify task marks those assets and the proxies re-fetch them from their upstreams on the next request.
This keeps backups small when most of the store is a cache. A hosted blob that is already missing from the store at
backup time is counted in the manifest (`blobs.hosted_missing`) and printed as a warning.

**The manifest** records what the backup holds (counts are taken right after the dump):

```json
{
  "format": 1,
  "name": "waystation-20261110T023000Z",
  "mode": "full",
  "started_at": "2026-11-10T02:30:00Z",
  "finished_at": "2026-11-10T02:31:12.840Z",
  "waystation": {"image": "registry.example.com/waystation/waystation-worker:0.1.0"},
  "mongodb": {
    "server_version": "8.0.15",
    "database": "waystation",
    "archive": "mongodb.archive.gz",
    "archive_bytes": 1843311,
    "archive_sha256": "<sha256 of mongodb.archive.gz>",
    "migrations": ["0001_baseline", "0002_search_keys"],
    "collections": {"assets": 48211, "blobs": 40102, "repositories": 31}
  },
  "blobs": {"files": 40102, "bytes": 96367411211, "linked_from_previous": 40011, "copied": 91,
            "previous": "waystation-20261109T023000Z", "hosted_missing": 0},
  "keys": {"files": 2}
}
```

`./waystation-backup.sh list --target /mnt/backup/waystation` prints one line per complete backup.

### Backing up to a NAS

- **NFS** (Synology, TrueNAS, ...): mount the export on the Docker host (`/mnt/backup`) and back up into a
  directory on it. NFS keeps hard links, so snapshots are incremental. With the default root squashing, root in the
  tools container becomes `nobody` on the NAS: either export with `no_root_squash` for the backup host only, or
  create the target directory owned by the application user and run the backup as that user, which can read the
  data directory: `--user 10001:10001` (`WAYSTATION_UID:GID`).
- **SMB/CIFS**: mount with Unix extensions if the server supports them; otherwise ownership and modes are not kept
  (a restore sets them again) and hard links may not work (the tool falls back to copies).
- Keep at least one copy off the host and, ideally, off-site. A NAS snapshot schedule on the target share adds
  protection against a backup run that deletes too much.

### Scheduling

cron (`/etc/cron.d/waystation-backup`), nightly at 02:30 with two weeks kept:

```cron
30 2 * * * root /srv/waystation/waystation-backup.sh backup --target /mnt/backup/waystation --keep 14 >>/var/log/waystation-backup.log 2>&1
```

systemd timer:

```ini
# /etc/systemd/system/waystation-backup.service
[Unit]
Description=Waystation backup
Requires=docker.service
After=docker.service mnt-backup.mount
RequiresMountsFor=/mnt/backup

[Service]
Type=oneshot
ExecStart=/srv/waystation/waystation-backup.sh backup --target /mnt/backup/waystation --keep 14

# /etc/systemd/system/waystation-backup.timer
[Unit]
Description=Nightly Waystation backup

[Timer]
OnCalendar=*-*-* 02:30:00
RandomizedDelaySec=15m
Persistent=true

[Install]
WantedBy=timers.target
```

`sudo systemctl enable --now waystation-backup.timer`; `journalctl -u waystation-backup` shows the runs. A failed
run exits non-zero (systemd marks the unit failed; alert on it with your usual tooling).

## 5. Restoring

A restore replaces the database with the dump, copies the backup's blob files and keys into the data directory
(files already there are kept: they are identical, content-addressed), sets their owner and modes, and empties
Valkey (its live state belongs to the old database). The tool checks the archive against the manifest's sha256 first.
The backup must come from the **same or an older** Waystation release: `init` applies newer migrations on start; a
server refuses to start on a database migrated by a newer release.

### On the same host

```bash
cd /srv/waystation
docker compose stop web server worker            # MongoDB and Valkey keep running
sudo ./waystation-backup.sh restore --from /mnt/backup/waystation/waystation-20261110T023000Z
docker compose up -d                             # init applies migrations and fixes ownership
./waystation-backup.sh verify                    # see section 6
# from a checkout: make restore FROM=<backup dir>; make verify-blobs
```

### On a new host

1. Install the stack as in [DEPLOYMENT.md](DEPLOYMENT.md) with the **same** env file and secret files (above all
   `WAYSTATION_SECRET_KEYS`), but do not start it yet.
2. Create the empty stack and let `init` prepare MongoDB (replica set, schema) and the data directory:
   `docker compose up -d init` and wait until `docker wait waystation-init` prints `0`; then
   `docker compose create worker` (the tool borrows the worker container's data mount).
3. `sudo ./waystation-backup.sh restore --from <backup directory>`.
4. `docker compose up -d`, then `./waystation-backup.sh verify`.

The administrator and every other account come back with the database; the bootstrap password in the env file is
only used when the database has no administrator.

## 6. After a restore: blob verify

The **blob verify** task (`blob.verify`, worker job, SPECIFICATION §8.5) checks every asset's blob file:

- **missing** (no file) or **damaged** (its size differs from the recorded size; a deep run also re-hashes every file).
  A damaged file is moved out of `blobs/` into `uploads/` (blob GC removes it a day later), so it is never served.
- Assets of **proxy** repositories whose blob is missing or damaged are **marked for re-fetch**
  (`assets.blob_missing`): the next request fetches them from the upstream again, which clears the mark.
- Assets of **hosted** repositories whose blob is missing or damaged are **lost**: the run lists them in its log
  (`lost: <repository>/<path>`), its result counts them per repository, an activity event
  (`storage.blobs_missing`, shown in the activity feed as a storage failure) announces them, and
  `waystation_assets_blob_missing{repository_type="hosted"}` on `/metrics` stays above zero until they are uploaded
  again or restored from an older backup.
- Marks whose blob is back are cleared.

It runs weekly (Sunday 03:30 UTC; change it on the Tasks page) and on demand: **Run now** on the Tasks page,
`POST /api/v1/tasks/scheduled/blob.verify/run`, or from the host:

```bash
./waystation-backup.sh verify            # = docker exec waystation-worker python -m waystation_worker.run_task blob.verify --wait
```

which waits for the run and prints its result:

```json
{
  "task": "blob.verify",
  "status": "succeeded",
  "result": {"assets": 48211, "blobs": 40102, "missing_blobs": 3120, "damaged_blobs": 0, "moved_blobs": 0,
             "inconsistent": 0, "proxy_marked": 3304, "hosted_lost": 0, "cleared": 0, "lost_repositories": {}}
}
```

After a `--hosted-only` restore, `proxy_marked` counts the cached files that will be re-fetched; `hosted_lost`
should be 0. `python -m waystation_worker.run_task <task id>` runs any scheduled task the same way (`blob.gc`, ...).

## 7. Monitoring backups and losses

- Backups: the tool exits non-zero on any failure; alert on the cron mail or the failed systemd unit. The newest
  `manifest.json` (`finished_at`) tells when the last good backup finished.
- `/metrics`: `waystation_assets_blob_missing{repository_type}` (assets currently marked: hosted = lost, proxy =
  waiting for a re-fetch), `waystation_verify_runs_total`, `waystation_verify_missing_blobs_total`,
  `waystation_verify_damaged_blobs_total`, `waystation_verify_refetch_marked_total`,
  `waystation_verify_hosted_lost_total` ([MONITORING.md](MONITORING.md)).

## 8. Without the tool

The same steps by hand, for a bundled MongoDB and the named data volume:

```bash
docker exec waystation-mongodb mongodump --archive --gzip --db waystation > waystation-$(date +%F).archive.gz
docker run --rm -v waystation_data:/data:ro -v /mnt/backup/waystation:/backup alpine \
  tar -C /data -cf /backup/waystation-blobs-$(date +%F).tar blobs keys        # after the dump
# restore (web, server and worker stopped):
docker exec -i waystation-mongodb mongorestore --archive --gzip --drop < waystation-2026-11-10.archive.gz
docker run --rm -v waystation_data:/data -v /mnt/backup/waystation:/backup alpine \
  tar -C /data -xf /backup/waystation-blobs-2026-11-10.tar
docker exec waystation-valkey valkey-cli FLUSHALL
docker compose up -d && docker exec waystation-worker python -m waystation_worker.run_task blob.verify --wait
```

## 9. Testing the procedure

`make backup-test` (from a checkout; Docker only) runs the whole cycle against a throwaway stack with its own
compose project: real clients upload to a hosted repository and fill a proxy, two full backups (the second
hard-linked from the first), a hosted-only backup with retention, a wipe (`down -v`), a restore into an empty stack,
blob verify (the proxied asset is marked for re-fetch, nothing hosted is lost), the clients again (hosted files
intact, the proxied file re-fetched), and a deliberately deleted hosted blob reported as lost. Rehearse a restore of
your own backups on a spare host from time to time.
