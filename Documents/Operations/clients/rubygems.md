# RubyGems: client guide

A RubyGems proxy repository caches a RubyGems server with the compact index (rubygems.org or any server that
serves `versions` and `info/<gem>`): the index, the gemspecs and the `.gem` files. A RubyGems hosted repository stores
gems you push with `gem push`. A RubyGems group serves several RubyGems repositories as one. Tested with Ruby 3.4,
RubyGems 3.6 and Bundler 2.6 (`gem install`, `bundle install` with a Gemfile source and as the rubygems.org mirror,
`gem push`, `gem yank`; `tests/clients/rubygems/`). Protocol notes: FORMATS.md "RubyGems".

Examples use `https://registry.example.com` as the Waystation base URL. The repository URL a client needs is
`https://registry.example.com/repository/<repo>/`.

## Create the repository

In the UI (Repositories → Create → RubyGems) or the API:

| Type | Settings |
|---|---|
| Proxy | Upstream URL: a RubyGems server with the compact index, e.g. `https://rubygems.org` (preset "rubygems.org"). No format options. |
| Hosted | Write policy: `allow_redeploy` (default: pushing an existing version replaces it), `disable_redeploy` (a version is pushed once; again answers `409`), `read_only` (pushes and yanks answer `403`). |
| Group | Members in order (RubyGems proxy and hosted repositories). Push to the hosted repository itself: a group has no writable member. |

Nexus migration: `rubygems-proxy`, `rubygems-hosted` and `rubygems-group` repositories import (gems are cached
forever: they are verified against the index checksum).

## gem

```sh
gem sources --remove https://rubygems.org/
gem sources --add https://registry.example.com/repository/rubygems-group/
gem install rake
```

or for one command: `gem install rake --clear-sources --source https://registry.example.com/repository/rubygems-group/`.
`gem sources --add` reads the legacy `specs.4.8.gz` index once to check the source; installs use the compact index.

## Bundler

In the Gemfile:

```ruby
source 'https://registry.example.com/repository/rubygems-group/'

gem 'rake'
```

or keep `source 'https://rubygems.org'` in every Gemfile and send it through Waystation:

```sh
bundle config set --global mirror.https://rubygems.org https://registry.example.com/repository/rubygems-group/
```

Bundler 2.5 or newer (Ruby 3.3+) is recommended: it checks the compact index files with the `Repr-Digest` Waystation
sends. Older Bundler compares the info file's MD5 with the `ETag`, which Waystation does not use, and falls back to
slower full downloads.

## Authentication

Reads use HTTP basic credentials with an API token as the password; leave them out when anonymous reads are allowed:

```sh
gem sources --add https://<username>:<api-token>@registry.example.com/repository/rubygems-group/
bundle config set --global registry.example.com <username>:<api-token>
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

## Push and yank (hosted)

```sh
gem build my_gem.gemspec
gem push my_gem-1.0.0.gem --host https://registry.example.com/repository/rubygems-hosted --key waystation
gem yank my_gem --version 1.0.0 --host https://registry.example.com/repository/rubygems-hosted --key waystation
```

A gemspec may set `spec.metadata['allowed_push_host'] = 'https://registry.example.com/repository/rubygems-hosted'`
so the gem cannot be pushed elsewhere by mistake. Pushes need the write permission, yanks the delete permission;
both are audited. A yank removes the version's files and its line from the index (the version can be pushed again
when the write policy allows). At most 512 MiB per gem. The `.gem` is checked before it is stored: a plain tar of
`metadata.gz`, `data.tar.gz` and optionally `checksums.yaml.gz` (which must match) and signatures, with a valid
specification (name, version, platform, dependencies).

## How it behaves

- The dependency API (`/api/v1/dependencies`) is retired: it answers `404`, and Bundler and gem use the compact index.
- A proxy caches `versions`, `names` and each `info/<gem>` as metadata (revalidated after the repository's metadata
  max age). Bundler fetches only the new tail of `versions` (`Range`), checks the result against `Repr-Digest` and
  downloads the whole file again when it does not match.
- `.gem` files are cached forever once their SHA-256 matches the checksum in the gem's info file; a version without
  a checksum or a different file answers an error and nothing is stored. Gemspecs (`quick/Marshal.4.8/*.gemspec.rz`)
  and the legacy `specs.4.8.gz` indexes are passed through as they are (checked to be zlib/gzip only: Waystation
  never reads Ruby Marshal data).
- A hosted repository renders its compact index per request and writes each gem's `quick/Marshal.4.8` gemspec when
  it is pushed.
- A group merges `versions` and `names` of its members (kept until a member changes), merges `info/<gem>` when
  several members have the gem (the first member that has a version wins it) and serves gems from the first member
  that has them. Its legacy `specs.4.8.gz` lists the hosted members' gems only.
- Browse (Repositories → a RubyGems repository) lists gems and their versions newest first (one row per platform),
  with checksum, Ruby requirement, dependencies and what is cached.
- Prefetch (proxy repositories): warm items `rake` (the newest release), `rake:13.2.1` or
  `nokogiri:1.18.8-x86_64-linux`; "keep popular metadata fresh" refreshes `versions`, `names` and info files.

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
