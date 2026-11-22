# RubyGems client guide

How to point gem and Bundler at a Waystation **RubyGems** repository and push your own gems. The repository's **Set
me up** tab shows the same snippets with your real URL filled in. Examples below use `https://waystation.example.com`
as the Waystation base URL, `rubygems-group` for a group repository and `rubygems-hosted` for a hosted repository.

## Overview

- The repository URL a client needs is `https://waystation.example.com/repository/<repo>/`. Waystation serves the
  compact index (`versions`, `names`, `info/<gem>`), gemspecs (`quick/Marshal.4.8/*.gemspec.rz`), the legacy
  `specs.4.8.gz` indexes and `gems/<file>.gem`, plus the push and yank API.
- Tested with Ruby 3.4, RubyGems 3.6 and Bundler 2.6 (`gem install`, `bundle install` with a Gemfile source and as
  the rubygems.org mirror, `gem push`, `gem yank`; `tests/clients/rubygems/`).
- Protocol notes: [FORMATS.md](../../Architecture/FORMATS.md), section "RubyGems".

## Repository types

Create a repository in the UI (Repositories → Create → RubyGems) or through the API:

| Type | Settings |
|---|---|
| Proxy | Caches a RubyGems server with the compact index (rubygems.org or any server that serves `versions` and `info/<gem>`): the index, the gemspecs and the `.gem` files. Upstream URL, e.g. `https://rubygems.org` (preset "rubygems.org"). No format options. |
| Hosted | Stores gems you push with `gem push`. Write policy: `allow_redeploy` (default: pushing an existing version replaces it), `disable_redeploy` (a version is pushed once; again answers `409`), `read_only` (pushes and yanks answer `403`). |
| Group | Serves several RubyGems repositories as one. Members in order (RubyGems proxy and hosted repositories). A group has no writable member: push to the hosted repository itself. |

Nexus migration: `rubygems-proxy`, `rubygems-hosted` and `rubygems-group` repositories import.

### How each type behaves

- **Proxy**: `versions`, `names` and each `info/<gem>` are cached as metadata (revalidated after the repository's
  metadata max age). Bundler fetches only the new tail of `versions` (`Range`), checks the result against
  `Repr-Digest` and downloads the whole file again when it does not match. `.gem` files are cached for good once
  their SHA-256 matches the checksum in the gem's info file; a version without a checksum or a different file answers
  an error and nothing is stored. Gemspecs and the legacy `specs.4.8.gz` indexes are passed through as they are
  (checked to be zlib/gzip only: Waystation never reads Ruby Marshal data).
- **Hosted**: the compact index is rendered per request, and each gem's `quick/Marshal.4.8` gemspec is written when
  it is pushed.
- **Group**: `versions` and `names` of the members are merged (kept until a member changes), `info/<gem>` is merged
  when several members have the gem (the first member that has a version wins it), and gems come from the first
  member that has them. The group's legacy `specs.4.8.gz` lists the hosted members' gems only.
- **Prefetch** (proxy repositories): warm items `rake` (the newest release), `rake:13.2.1` or
  `nokogiri:1.18.8-x86_64-linux`; "keep popular metadata fresh" refreshes `versions`, `names` and info files.
- **Browse** (Repositories → a RubyGems repository) lists gems and their versions newest first (one row per
  platform), with checksum, Ruby requirement, dependencies and what is cached.

## Client setup

### gem

```sh
gem sources --remove https://rubygems.org/
gem sources --add https://waystation.example.com/repository/rubygems-group/
gem install rake
```

or for one command: `gem install rake --clear-sources --source https://waystation.example.com/repository/rubygems-group/`.
`gem sources --add` reads the legacy `specs.4.8.gz` index once to check the source; installs use the compact index.

### Bundler

In the Gemfile:

```ruby
source 'https://waystation.example.com/repository/rubygems-group/'

gem 'rake'
```

or keep `source 'https://rubygems.org'` in every Gemfile and send it through Waystation:

```sh
bundle config set --global mirror.https://rubygems.org https://waystation.example.com/repository/rubygems-group/
```

Bundler 2.5 or newer (Ruby 3.3+) is recommended: it checks the compact index files with the `Repr-Digest` Waystation
sends. Older Bundler compares the info file's MD5 with the `ETag`, which Waystation does not use, and falls back to
slower full downloads.

## Authentication

Reads use HTTP basic credentials with an API token as the password; leave them out when anonymous reads are allowed:

```sh
gem sources --add https://<username>:<api-token>@waystation.example.com/repository/rubygems-group/
bundle config set --global waystation.example.com <username>:<api-token>
```

Bundler looks the credentials up by the source URL or its host name (`BUNDLE_REGISTRY__EXAMPLE__COM` as an
environment variable: dots become `__`, dashes `___`).

Pushes and yanks send the API key as the whole `Authorization` header (no scheme), which Waystation accepts on
`/repository/...` as an API token. Either set `GEM_HOST_API_KEY=<api-token>`, or keep it in `~/.gem/credentials`
(mode 600) under a name and pass `--key`:

```sh
mkdir -p ~/.gem
printf ':waystation: %s\n' '<api-token>' >> ~/.gem/credentials
chmod 600 ~/.gem/credentials
```

## Publishing

Hosted repositories accept pushes and yanks (not groups: push to the hosted member directly):

```sh
gem build my_gem.gemspec
gem push my_gem-1.0.0.gem --host https://waystation.example.com/repository/rubygems-hosted --key waystation
gem yank my_gem --version 1.0.0 --host https://waystation.example.com/repository/rubygems-hosted --key waystation
```

- A gemspec may set `spec.metadata['allowed_push_host'] = 'https://waystation.example.com/repository/rubygems-hosted'`
  so the gem cannot be pushed elsewhere by mistake.
- Pushes need the write permission, yanks the delete permission; both are audited.
- A yank removes the version's files and its line from the index (the version can be pushed again when the write
  policy allows).
- At most 512 MiB per gem. The `.gem` is checked before it is stored: a plain tar of `metadata.gz`, `data.tar.gz` and
  optionally `checksums.yaml.gz` (which must match) and signatures, with a valid specification (name, version,
  platform, dependencies).

## Limitations

- The dependency API (`/api/v1/dependencies`) is retired: it answers `404`; Bundler and gem use the compact index.
- Groups have no writable member: push to a hosted repository.
- A group's legacy `specs.4.8.gz` lists only its hosted members' gems.
- Bundler older than 2.5 cannot use incremental index updates and downloads index files in full.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `401` / `Bad username or password` | Anonymous reads are not allowed: add credentials (an API token as the password). A wrong or revoked token also answers `401`. |
| `gem push` answers `401` | Set `GEM_HOST_API_KEY` or `--key` to an API token with the write permission (the bare key, no `Bearer`). |
| `gem push` answers `400` | Not a `.gem`, a checksum in `checksums.yaml.gz` does not match, or the specification is invalid (the message says which). |
| `gem push` answers `409` | The repository does not allow redeploying and the version (with its platform) exists. |
| A `.gem` download answers `502` | The upstream file does not match the checksum in its info file. The repository's recent errors show it. |
| A new upstream version is not found | The cached `versions`/info files are refreshed after the metadata max age; a `.gem` that is not listed yet makes Waystation re-read the gem's info file once. |
| Bundler fetches the whole `versions` file every time | Bundler older than 2.5: upgrade Bundler (`gem install bundler`). |
