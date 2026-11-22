# ADR 0010: Signed download URLs for the files a Terraform registry answer points at

Status: accepted (2026-11-22, owner). Proposed 2026-11-22 with T-P5-TFM.

## Context
T-P5-TFM serves the Terraform Module Registry Protocol (proxy and hosted repositories) and the Provider Registry
Protocol (the hosted repository). In both, a registry answer points at a file the client fetches next: a module's
archive (`X-Terraform-Get` of `.../download`), a provider's zip, `SHA256SUMS` and signature (`download_url`,
`shasums_url`, `shasums_signature_url` of the download document). Checked with terraform 1.17 and tofu 1.13 against a
scripted registry (2026-11-22):

- The registry API requests (`/.well-known/terraform.json`, `versions`, `download`) carry the host's credentials
  (`credentials` block or `TF_TOKEN_*`: `Authorization: Bearer <token>`).
- The file downloads do **not**. terraform sends `~/.netrc` credentials (Basic) for module archives and the provider
  zip, but nothing for `SHA256SUMS` and its signature; tofu sends nothing for provider files at all.

So a repository that anonymous users cannot read can serve the registry API but not the files: a private provider
cannot be installed by either client, and private modules need a `.netrc` besides the token. Public registries and
Terraform Cloud solve this the same way: the file URLs are pre-signed and short-lived.

Options:

1. **Require anonymous read** on repositories used with Terraform, or document `.netrc`. Leaks private modules to
   anonymous users in the first case and does not work for provider files (tofu, and terraform's checksum list) in
   the second.
2. **Signed download URLs.** When a signed-in principal asks for a registry answer, each file URL in it carries a
   grant: `?ws_grant=<expiry>.<mac>`, HMAC-SHA256 under a key derived from `WAYSTATION_SECRET_KEYS` over the
   repository id, the exact normalised path and the expiry (5 minutes). The router accepts a valid grant as the
   authorisation of that one `GET`/`HEAD` and nothing else.
3. **Serve the files through the registry API path with the token in the URL** (a token query parameter). Puts a
   long-lived credential in URLs, client logs and proxy logs.

## Decision
Option 2, implemented as:

- `waystation_server.security.download_urls`: `DownloadUrls.sign(repo_id, path)` / `verify(grant, repo_id, path)`.
  Keys: HMAC-SHA256 of a fixed label under each `WAYSTATION_SECRET_KEYS` entry (the first signs, all verify, as for
  API token hashes); without secret keys a random per-process key (grants then work only on the issuing replica).
  Grants are at most 5 minutes valid (plus 60 s clock tolerance), bound to repository and path, carry no identity
  and give nothing but a read of that file.
- The decision stays in `security.access` (`download_grant`): the router (`routing._dispatch`) asks it before the
  normal authorisation for `GET`/`HEAD` on `/repository/<repo>/...`; a valid grant yields a principal that may read
  exactly that path (`AuthMethod.SIGNED_URL`, no roles); an invalid, expired or foreign grant is ignored and the
  request is authenticated as usual (so it gets the normal 401/404).
- Formats get grants only through the `FormatRequest` (`req.download_query(path)`), never from the security
  internals (ARCHITECTURE §3, §11). Only the Terraform format issues them, and only for a signed-in principal
  (anonymous answers carry plain URLs, which anonymous users can fetch anyway).
- The server's access log records paths without query strings; nginx's access log keeps the query (a grant expires
  within minutes and opens one file).

## Consequences
- terraform and tofu install private modules and providers with only a `credentials` block (verified by the client
  harness: `tests/clients/terraform/test.sh`, hosted section).
- A grant handed to someone else lets them download that one file for up to 5 minutes; revoking the token that
  obtained it does not shorten that window (as for docker registry tokens issued for an API token, which are
  re-checked; grants are not, they are bounded by their lifetime instead).
- ARCHITECTURE §7 gains one more way a request is authorised ("signed download grant"), still decided by
  `security.access`.
- An S3 blob driver (D5) would issue its own presigned URLs for the same answers; the grant format is internal and
  can change without a migration (grants never outlive 5 minutes).
