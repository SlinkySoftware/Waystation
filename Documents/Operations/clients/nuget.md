# NuGet (v3): client guide

A NuGet proxy repository caches a NuGet v3 feed (nuget.org or any v3 server): its version lists, registrations and
packages. A NuGet hosted repository stores packages you push with `dotnet nuget push` or `nuget push`. A NuGet group
serves several NuGet repositories as one source. Tested with the .NET 10 SDK (`dotnet restore`, `build`, `pack`,
`nuget push`, `nuget delete`, `list package --outdated`, `package search`; `tests/clients/nuget/`). NuGet.exe speaks
the same v3 protocol (not in the harness: it needs Windows or Mono). Protocol notes: FORMATS.md "NuGet v3".

Examples use `https://registry.example.com` as the Waystation base URL. The source URL a client needs is the
repository's **service index**: `https://registry.example.com/repository/<repo>/index.json`.

## Create the repository

In the UI (Repositories → Create → NuGet) or the API:

| Type | Settings |
|---|---|
| Proxy | Upstream URL: the upstream's v3 service index, e.g. `https://api.nuget.org/v3/index.json` (preset "nuget.org"; a URL that does not end in `.json` gets `/index.json`). Option `verify_catalog` (default on): packages are checked against the SHA-512 hash and size the upstream's catalog publishes (nuget.org has a catalog; other servers may not, then only the package check below applies). |
| Hosted | Write policy: `allow_redeploy` (default: pushing an existing version replaces it), `disable_redeploy` (a version is pushed once; again answers `409`, which `--skip-duplicate` accepts), `read_only` (pushes and deletes answer `403`). Option `delete_mode`: `unlist` (default, as nuget.org: `dotnet nuget delete` hides the version from search and marks it unlisted, it stays restorable by exact version) or `delete` (removes it). |
| Group | Members in order (NuGet proxy and hosted repositories), optionally a writable member (a hosted member that receives pushes and deletes sent to the group). |

Nexus migration: `nuget-proxy` (NuGet **V3** proxies), `nuget-hosted` and `nuget-group` repositories import; a Nexus
proxy set to the V2 protocol is reported invalid (point it at a v3 service index instead).

## dotnet CLI

User-wide source:

```sh
dotnet nuget add source https://registry.example.com/repository/nuget-group/index.json --name waystation \
  --username alice --password <api-token> --store-password-in-clear-text
dotnet add package Newtonsoft.Json
```

Per solution (`nuget.config` next to the `.sln`); leave out `packageSourceCredentials` when anonymous reads are
allowed:

```xml
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <packageSources>
    <clear />
    <add key="waystation" value="https://registry.example.com/repository/nuget-group/index.json" protocolVersion="3" />
  </packageSources>
  <packageSourceCredentials>
    <waystation>
      <add key="Username" value="alice" />
      <add key="ClearTextPassword" value="<api-token>" />
    </waystation>
  </packageSourceCredentials>
</configuration>
```

Plain-HTTP sources (a test instance without TLS) need `allowInsecureConnections="true"` on the `<add>` element (or
`--allow-insecure-connections`): .NET 9 and later refuse HTTP sources otherwise (NU1302).

Push and delete (hosted repositories, or a group with a writable member):

```sh
dotnet pack -c Release
dotnet nuget push bin/Release/MyLib.1.0.0.nupkg --source waystation --api-key <api-token>
dotnet nuget delete MyLib 1.0.0 --source waystation --api-key <api-token> --non-interactive
```

## NuGet.exe

```powershell
nuget sources add -Name waystation -Source https://registry.example.com/repository/nuget-group/index.json `
  -Username alice -Password <api-token>
nuget install Newtonsoft.Json -Source waystation
nuget push MyLib.1.0.0.nupkg -Source waystation -ApiKey <api-token>
```

## Authentication

- Reads: basic credentials, any username and an **API token** as the password (`packageSourceCredentials`). Clients
  send them after the `401` challenge Waystation answers when anonymous access does not cover the repository.
- Push and delete: the API key (`X-NuGet-ApiKey`, `--api-key`) is an API token with the `write` scope (`delete` for
  `dotnet nuget delete`). When the source also has credentials in `nuget.config`, NuGet sends them as well and they
  decide (Waystation reads the API key only from requests without an `Authorization` header); an API key that is not
  a valid token is refused with `401`. Feeds that tell you to pass a dummy key (`--api-key az`) work the same way.
- Unlisting and relisting from the UI (Browse → the package → Unlist/Relist) needs `write` on the repository.

## How it behaves

- The service index Waystation serves names only Waystation URLs: the flat container (`v3-flatcontainer/`), the
  registrations (`v3/registration/`, SemVer 2.0.0 packages included) and search (`query`); hosted repositories and
  groups with a writable member add the publish endpoint (`api/v2/package`).
- Proxy: version lists and registrations are metadata (revalidated after the repository's metadata max age, rewritten
  so their URLs point at Waystation); packages are content, kept until cleanup removes them. Before a package is
  stored it must be a readable NuGet package (zip entries checked for path traversal, bounded sizes) whose `.nuspec`
  names the requested id and version, and with `verify_catalog` it must match the catalog hash and size. Search is
  passed through to the upstream (not cached).
- Hosted: the push is checked the same way (one `.nuspec` at the root, valid id and version); the version is
  normalised (a zero fourth number is dropped, leading zeros too; package URLs are lower case). Version lists include unlisted versions (as
  nuget.org's do, so exact-version restores keep working); search and registrations mark them unlisted.
- Groups merge version lists, registrations and search answers; the first member that has a version wins it.
- Prefetch (proxy repositories): warm items `Package@version` (`Newtonsoft.Json@13.0.3`) or `Package` (the newest
  stable version) fetch the version list, the package and its `.nuspec`; popular version lists and registration
  indexes can be refreshed before they go stale.
- Browse shows packages (id, latest version, description) and their versions newest first (NuGet order), listed or
  unlisted, cached or not; global search finds packages by id.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `NU1302` / "HTTP source" error | The source URL is plain HTTP: add `allowInsecureConnections="true"` (or use HTTPS). |
| `401` on restore | The repository is not readable anonymously: add `packageSourceCredentials` with an API token as the password. |
| `401` on push with a valid-looking key | The API key is not an API token (or it was revoked); create one with the `write` scope. |
| `403` on push | The token lacks `write` on the repository (or `delete` for `dotnet nuget delete`), or the repository is `read_only`. |
| `409` on push | The version exists and the repository's write policy is `disable_redeploy` (`--skip-duplicate` to ignore). |
| `502` for one package | The upstream's bytes did not match its catalog hash or size, or the package's `.nuspec` names another package: nothing was cached. Retry later or turn `verify_catalog` off for upstreams whose catalog is wrong. |
| A version is missing from search | It is unlisted (Browse → Relist), a prerelease (`--prerelease`), or SemVer 2.0.0 and the client did not ask for `semVerLevel=2.0.0`. |
