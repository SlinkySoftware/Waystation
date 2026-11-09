# Migrating from Nexus Repository 3

This guide moves a running Nexus Repository 3 to Waystation with no client downtime: Waystation runs next to Nexus,
takes over one format (or one repository) at a time, and Nexus stays untouched until you decide it can go. Every
step can be undone up to the last one.

Examples use `https://nexus.example.com` for Nexus and `https://registry.example.com` for Waystation. Replace them
with your own; nothing here needs to change in Nexus.

| Moves automatically | Moves with a command per repository | Does not move (redo by hand) |
|---|---|---|
| Repositories: names, formats, types, upstream URLs, cache ages, negative cache, auto-block, online state, docker Hub flag and aliases, the default docker repository, apt distribution, raw content disposition, hosted write policy, group members and writable member (step 2) | Hosted content you still need: docker images (`skopeo`/`crane`), raw files (`curl`) (step 4) | Upstream credentials (Nexus never returns passwords), users, roles, LDAP/OIDC settings, cleanup policies, routing rules, docker connector ports, signing keys (steps 3 and 7) |

Cached proxy content is never copied: Waystation fetches it again on first use, verifies it and keeps it.

## 1. Before you start

1. **Deploy Waystation** on its own host name ([DEPLOYMENT.md](DEPLOYMENT.md)), behind your reverse proxy if you
   use one ([REVERSE_PROXIES.md](REVERSE_PROXIES.md)). Size the data volume for the content clients actually pull
   (usually far less than the Nexus blob store, which also keeps what nobody uses any more).
2. **Inventory what clients use.** The Nexus request log (`request.log`) shows which repositories, paths and user
   agents are active. Note for each format how clients are configured today (docker daemon mirrors, connector
   ports, `.npmrc`, `sources.list`, `.repo` files, scripts with URLs).
3. **Decide the switch strategy** (step 6 and 7): change client configuration to the new host name (cleanest),
   or keep the Nexus host name and move it to Waystation behind the reverse proxy (no client changes except
   credentials), or both, format by format.
4. **Plan credentials.** Waystation format clients authenticate with API tokens (as the password of basic auth, any
   user name, or as a bearer token), never with account passwords. Every client that signs in to Nexus today needs
   a token (step 3) before it is switched.

## 2. Import the repository configuration

Repositories → **Import from Nexus** (needs the `admin` permission). Give the Nexus base URL and a Nexus
administrator's user name and password: they are used for this request only, never stored, logged or audited.
A Nexus on a private or loopback address needs **Allow private address**; a private CA goes in **Extra CA
certificates**.

1. **Preview** (writes nothing) lists every Nexus repository as *create*, *exists* (a Waystation repository already
   has the name: left alone), *unsupported* (a format or type Waystation does not serve yet, with the reason) or
   *invalid*. Expand an item to see every setting that differs and why (cleanup policies, routing rules, blob
   stores, connector ports, credentials that cannot be read).
2. Check the **default docker repository**: the Docker Hub proxy behind a Nexus connector, else the only Docker Hub
   proxy; it receives unprefixed pulls (`registry.example.com/library/alpine`) and daemon `registry-mirrors`
   traffic. Pick another one in the form if needed.
3. **Apply** the selection. Members are created before their groups; the import can be repeated safely (existing
   names are reported, never changed). Each repository is audited as `repository.create` with
   `details.source = nexus_import`.

The same is available to scripts: `POST /api/v1/import/nexus/preview` and `POST /api/v1/import/nexus/apply` (JSON
body `url`, `username`, `password`, `allow_private`, `ca_pem`, `docker_aliases`, `default_docker_repo`; apply also
takes `repositories`, the names to import).

## 3. Re-enter credentials and access

1. **Upstream credentials.** Proxies whose Nexus repository used authentication (a Docker Hub account for higher pull
   limits, a GHCR token, a private upstream) are created without it; the import report says which. Open each one
   (Settings → Upstream) and enter the user name and password or token. Secrets are write-only and encrypted at rest.
   Use **Test upstream** to confirm.
2. **Private CAs and proxies**: per repository (CA bundle, outbound HTTP proxy) or globally
   (`WAYSTATION_EXTRA_CA_FILE`, `WAYSTATION_HTTP_PROXY`).
3. **Sign-in**: configure LDAP / Active Directory or OIDC (Administration → Auth providers), map directory groups to
   roles, and create local users only where needed.
4. **Roles**: recreate the Nexus roles you rely on as Waystation roles (repository patterns × actions: browse, read,
   write, delete, manage). Decide anonymous access (Administration → Roles → Anonymous access); by default anonymous
   users may browse and read proxy repositories only.
5. **API tokens** for every client and CI job that authenticates: on a user's profile, or on a service account for
   automation. Give each the smallest scope (`read` for pulls and installs, `write` for pushes and uploads) and
   restrict it to the repositories it needs.

## 4. Copy hosted content you still need

Cached content needs nothing. Hosted content (what your own builds pushed to Nexus) is copied with the formats' own
tools, which verify every digest on the way. Copy into the hosted repository the import created (same name).

**Docker** (any OCI tool; keeps multi-platform indexes, signatures and attestations):

```sh
# one image, every platform
skopeo copy --all --src-creds "$NEXUS_USERNAME:$NEXUS_PASSWORD" --dest-creds "migrate:$WAYSTATION_TOKEN" \
  docker://nexus.example.com:8083/team/app:1.4.2 docker://registry.example.com/docker-hosted/team/app:1.4.2

# every tag of a repository
crane auth login nexus.example.com:8083 -u "$NEXUS_USERNAME" -p "$NEXUS_PASSWORD"
crane auth login registry.example.com -u migrate -p "$WAYSTATION_TOKEN"
for tag in $(crane ls nexus.example.com:8083/team/app); do
  crane copy "nexus.example.com:8083/team/app:$tag" "registry.example.com/docker-hosted/team/app:$tag"
done
```

**Raw** (list the assets through the Nexus REST API, upload each one with its sha256 checked by Waystation):

```sh
repo=files token=''
while :; do
  page=$(curl -fsS -u "$NEXUS_USERNAME:$NEXUS_PASSWORD" \
    "https://nexus.example.com/service/rest/v1/assets?repository=$repo${token:+&continuationToken=$token}")
  printf '%s' "$page" | jq -r '.items[] | [.path, .checksum.sha256] | @tsv' |
  while IFS="$(printf '\t')" read -r path sha; do
    curl -fsS -u "$NEXUS_USERNAME:$NEXUS_PASSWORD" -o /tmp/asset "https://nexus.example.com/repository/$repo/$path"
    curl -fsS -u "migrate:$WAYSTATION_TOKEN" -H "X-Checksum-Sha256: $sha" --upload-file /tmp/asset \
      "https://registry.example.com/repository/$repo/$path"
  done
  token=$(printf '%s' "$page" | jq -r '.continuationToken // empty')
  [ -n "$token" ] || break
done
```

Use a token with the `write` scope restricted to the target repositories, and set the hosted repository's write
policy to `allow_redeploy` while copying if you may run the loop twice (put it back afterwards).

Hosted npm, apt and yum repositories are not served by Waystation yet (the import report marks them
*unsupported*); keep them in Nexus until they are (TASKS T-M04 adds a migration job for hosted content).

## 5. Warm the cache (optional)

Clients fill the cache by using it. To avoid a slow first day, run the side-by-side checks of step 6 (every `GET`
is a cache fill) or the same commands your CI runs, pointed at Waystation.

## 6. Validate side by side

`Build/migration/nexus_compare.py` sends the same requests to Nexus and Waystation and compares what clients care
about: the status, the size, the sha256 of the body, `Docker-Content-Digest`, the media type, and chosen JSON values.
It needs only Python 3.11+ (standard library) and never writes.

1. Copy `Build/migration/checks.example.toml` and list the requests that matter: one or two per repository, taken
   from the Nexus request log (docker manifest `HEAD`/`GET`, npm packument and a tarball, apt `InRelease`,
   `Packages` and a pool file, yum `repomd.xml` and an rpm, raw files, PyPI simple pages).
2. Run it (credentials only from the environment):

   ```sh
   export NEXUS_USERNAME=reader NEXUS_PASSWORD='...' WAYSTATION_TOKEN='wst_...'
   make nexus-compare NEXUS=https://nexus.example.com WAYSTATION=https://registry.example.com \
     CHECKS=checks.toml REPORT=markdown OUTPUT=cutover-report.md
   # or without the repository checkout's dev image:
   docker run --rm -v "$PWD:/w" -w /w -e NEXUS_USERNAME -e NEXUS_PASSWORD -e WAYSTATION_TOKEN python:3.14-slim \
     python nexus_compare.py --nexus https://nexus.example.com --waystation https://registry.example.com \
     --checks checks.toml
   ```

   Options: `--only docker,npm`, `--report table|markdown|json`, `--output FILE`, `--ca-file PEM` (private CAs),
   `--timeout`, `--parallel`, `--max-bytes`; `NETWORK=host` on the make target reaches servers on the local host.
3. Read the report. Exit code `0` = every check matches, `1` = a difference or a failed request, `2` = a bad checks
   file or option. Each row shows both statuses and what differs:

   ```
   RESULT  FORMAT  CHECK                        NEXUS  WAYSTATION  DETAIL
   OK      docker  alpine:3.20 index (HEAD)     200    200         sha256:4bcff63911fc, 9218 B
   DIFF    apt     trixie InRelease             200    200         digest: nexus 1c41a3e0b2d4 != waystation 8f02bb7a91c3
   ERROR   raw     an uploaded file             200    error       waystation: connection failed: ...
   ```

Expected differences and what to do:

| Difference | Meaning |
|---|---|
| npm packuments, PyPI simple pages: `digest` | Both servers rewrite download URLs to themselves. Compare `json` pointers (`/dist-tags/latest`, `/versions/<v>/dist/integrity`) instead, or add `normalise = ["base_url", "json"]`. |
| raw `content_type` | Waystation types files by extension (never by the upstream's claim); Nexus may answer otherwise. Leave `content_type` out of raw checks. |
| apt `InRelease`/`Packages` `digest` | The upstream published a new index between the two requests (mirrors update several times a day). Run the check again; the pool files must always match. |
| docker tag `docker_digest` | The tag moved upstream between the two caches' refreshes (`latest`, rolling tags). Compare digests by digest reference, or after both caches refreshed. |
| `status: nexus 200 != waystation 404` | Missing content in Waystation: a hosted file not copied (step 4), a renamed repository (`waystation_path`), or a repository the import marked unsupported. |
| `status ... 401/403/404` on Waystation | The token lacks read access to the repository (hidden repositories answer 404). |

Keep the final report with the cutover change record.

## 7. Switch clients, format by format

Each client guide has the exact configuration, authentication and troubleshooting:
[docker](clients/docker.md), [npm](clients/npm.md), [apt](clients/apt.md), [yum/dnf](clients/yum.md),
[raw](clients/raw.md), [Go](clients/go.md). In short:

| Format | Nexus today | Waystation |
|---|---|---|
| Docker | `nexus.example.com:8082/library/alpine` (connector port), `registry-mirrors` | `registry.example.com/library/alpine` (default repository), `registry.example.com/<repo>/<image>`, `registry.example.com/docker.io/<image>` (alias); `registry-mirrors: ["https://registry.example.com"]` |
| npm | `registry=https://nexus.example.com/repository/npm/` | the same path on `registry.example.com`; `//registry.example.com/repository/npm/:_authToken=<token>` |
| apt | `deb https://nexus.example.com/repository/debian trixie main` | the same path; credentials in `/etc/apt/auth.conf.d/` |
| Go | `GOPROXY=https://nexus.example.com/repository/go-proxy/` | the same path; credentials in `~/.netrc`; `GOSUMDB` can stay at its default (the checksum database is proxied) |
| yum/dnf | `baseurl=https://nexus.example.com/repository/rocky/...` | the same path; `username`/`password` (token) in the `.repo` file |
| Raw | `https://nexus.example.com/repository/files/<path>` | the same path; `curl -u <user>:<token>` |

Switch a few clients first (a CI runner, one server), watch them on the Activity page and the repository's upstream
health card, then the rest. Nexus stays as it is, so a client that misbehaves goes back by restoring its old
configuration.

## 8. Cutover through the reverse proxy (keep the Nexus host name)

When clients cannot all be changed, move the host name instead. Waystation serves the Nexus path layout
(`/repository/<name>/...`, docker also at `/repository/<name>/v2/...`), so the reverse proxy in front of both can
send repositories to Waystation one by one while everything else still reaches Nexus:

```nginx
# nexus.example.com: Waystation takes over the repositories listed here; the rest is still Nexus.
location ~ ^/repository/(npm|debian|debian-security|rocky|files)/ {
    proxy_pass http://waystation-web:8080;   # the settings of REVERSE_PROXIES.md §3 (buffering off, sizes, timeouts)
}
location /v2/ {                                # docker API (connector ports: see below)
    proxy_pass http://waystation-web:8080;
}
location / {
    proxy_pass http://nexus:8081;
}
```

- Apply the requirements in [REVERSE_PROXIES.md](REVERSE_PROXIES.md) §1 to the Waystation locations (no request or
  response buffering, no body size limit, long timeouts, `X-Forwarded-*`), and set `WAYSTATION_BASE_URL` to the
  host name clients use, so rewritten metadata (npm tarball URLs, docker token realm) points back through the proxy.
- **Docker connector ports**: a Nexus connector (`nexus.example.com:8082`) is a whole host:port for one repository.
  Point that port at Waystation's web tier: unprefixed names land in the default docker repository (step 2). A second
  connector for another repository needs a client change (`registry.example.com/<repo>/<image>`) because only one
  repository can be the default.
- **Credentials**: switch a repository only once its clients use Waystation API tokens (step 3). A client still
  sending its Nexus password is refused (401) by Waystation.
- Move the Nexus UI and REST paths (`/`, `/service/`) last, when nothing uses Nexus any more.

## 9. Rollback

Until Nexus is decommissioned, rolling back is a configuration change:

1. Restore the previous client configuration, reverse-proxy routes or DNS record. Nexus was never changed, so it
   serves exactly what it served before.
2. Content uploaded to Waystation hosted repositories since the switch is not in Nexus. List it from the audit log
   (Administration → Audit log, action `content.upload`, from the switch time; or `GET /api/v1/audit?action=
   content.upload&from=<time>`, also exportable as CSV) and push or upload it to Nexus again with the commands of
   step 4 in the other direction.
3. Proxy caches need nothing: they are only caches.

## 10. Decommission Nexus

After a quiet period (a release cycle, or as long as your change policy requires) with no Nexus traffic in its
request log: take a final Nexus backup, stop it, keep the backup for your retention period, and remove the Nexus
routes from the reverse proxy.
