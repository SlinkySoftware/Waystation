# Rotating WAYSTATION_SECRET_KEYS

`WAYSTATION_SECRET_KEYS` is a comma-separated list of base64 AES-256 keys. The **first** key encrypts and hashes
everything new; **every** key in the list can decrypt and is tried when a keyed hash is checked. To retire a key,
put a new key first, let Waystation re-encrypt what is stored, then remove the old key.

## What the keys protect

| Stored where | What | After the job ran |
|---|---|---|
| MongoDB `repositories` | upstream credentials (`proxy.auth.secret_enc`) | re-encrypted with the new key |
| MongoDB `auth_providers` | LDAP service bind password, OIDC client secret | re-encrypted |
| MongoDB `webhooks` | HMAC signing secret, custom header values | re-encrypted |
| MongoDB `users` | TOTP secrets (enrolled and pending) | re-encrypted |
| MongoDB `sessions` | OIDC ID tokens kept for RP-initiated logout | re-encrypted |
| Data directory `keys/` | hosted apt/yum signing keys (`<repo id>.json`), the registry token signing key (`registry-token.key`) | files rewritten with the new key; the keys themselves do not change, so signatures, `Signed-By` files and issued registry tokens stay valid |
| MongoDB `api_tokens` | API token hashes (HMAC under a key derived from the secret key) | **cannot be re-hashed without the token.** A token is re-hashed with the new key the first time it is used after the rotation (within about a minute). Tokens nobody uses keep the old hash |
| MongoDB `users` | TOTP recovery-code hashes | **cannot be re-hashed.** They keep working while the old key is configured; users make new codes (Profile, Two-factor sign-in) |
| Valkey `uptok:*` | upstream registry tokens shared between server processes | not rotated: they expire within an hour and are fetched again |
| URLs | Terraform download grants (`?ws_grant=`) | not stored; valid for 5 minutes |

Session cookies and registry refresh tokens do not depend on `WAYSTATION_SECRET_KEYS` directly: refresh tokens are
signed with the registry token signing key, which the job re-encrypts without changing.

## Procedure

1. **Generate a key** on a trusted machine: `head -c 32 /dev/urandom | base64`. Keep it with your other secrets and
   in the backup of the env file or secret files (BACKUP.md): without the key, a database backup's credentials cannot
   be read.
2. **Put it first, keep the old keys**: `WAYSTATION_SECRET_KEYS=<new>,<old>` (or the same in the file named by
   `WAYSTATION_SECRET_KEYS_FILE`).
3. **Restart every server and worker container** (all hosts in a split deployment, DEPLOYMENT.md): `docker compose
   --env-file waystation.env up -d` recreates the containers whose environment changed. Until every replica has the
   new list, a replica with only the old key cannot read what the others encrypt with the new one, so restart them
   all before going on.
4. **Run the job** `secrets.rotate` now instead of waiting for its daily run (04:23 UTC):
   - Administration → Tasks → Scheduled → "Secret-key rotation" → Run now, or
   - `docker exec waystation-worker python -m waystation_worker.run_task secrets.rotate --wait`
     (prints the result as JSON; exit code 0 when it succeeded).
5. **Check the result** (the run's detail on the Tasks page, or the JSON):
   - `undecryptable` must be `0`. A value no configured key decrypts makes the run fail (shown red, a dead letter)
     after everything else was re-encrypted; the run log and `stores.<store>.undecryptable_ids` name the documents
     (never values). It usually means a key was removed too early or a backup from another installation was
     restored: put the missing key back, or enter the credential again (repository, provider, webhook settings;
     a user turns two-factor sign-in off and on again), then run the job again.
   - `conflicts` counts values an administrator changed while the job ran; they were saved with the new key
     already. A second run reports them as `already_current`.
   - Running the job again must report `rotated: 0`.
   - `hashes.api_tokens` and `hashes.recovery_codes`: `current`, `older_key` (made with a key that is not the
     first one) and `unrecorded` (made by a release that did not record which key hashed them, 0.2.8 or older).
     Every `older_key` and `unrecorded` API token stops working when the key that made it is removed, unless it is
     used before that (it is then re-hashed with the new key; the rollup stores it within a minute). The same for
     recovery codes, which are only replaced when the user makes new ones.
6. **Remove the old key** when `undecryptable` is 0, a second run rotated nothing, and you accept what the hash
   report says (wait until the API tokens your automation uses were used once, or replace them; ask two-factor users
   with `older_key`/`unrecorded` recovery codes to make new ones). Set `WAYSTATION_SECRET_KEYS=<new>` and restart all
   server and worker containers again. Keep the old key with your backups for as long as you keep backups made
   before the rotation.

The job runs daily anyway: when nothing uses an older key it reads nothing but a few empty queries. A run that
re-encrypted something (or found undecryptable values) is recorded in the audit log as `secrets.rotate` (actor
`system`, counts only).

## If a key is lost

Values encrypted only with a lost key cannot be recovered. The job reports them as `undecryptable`; re-enter each
credential (upstream passwords, LDAP bind password, OIDC client secret, webhook secrets), let affected users set up
two-factor sign-in again (an administrator can reset it), and delete the affected `keys/<repo id>.json` files only
if you accept new signing keys for those hosted repositories (clients must then trust the new public key).
An undecryptable `registry-token.key` can be deleted: the server creates a new one (restart the servers; Docker
clients sign in again).
