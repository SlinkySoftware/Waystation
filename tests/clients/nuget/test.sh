#!/bin/sh
# NuGet through Waystation (client-dotnet container: the .NET SDK's dotnet CLI). dotnet restores and builds against a
# public package through the api.nuget.org proxy (the package verified against its catalog hash by Waystation), packs a
# generated library and pushes it with an API token as the API key to the hosted repository and through the group,
# restores it through the hosted repository and the group (with the proxied package), unlists a version, searches,
# and checks that a wrong API key and anonymous reads of the hosted repository are refused. The client never talks to
# the upstream: the service index Waystation serves names only Waystation URLs.
set -eu
. /harness/lib.sh

W=$WAYSTATION_URL
work=/tmp/nuget-harness
rm -rf "$work"
mkdir -p "$work/home"
export HOME="$work/home" DOTNET_CLI_HOME="$work/home"
export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1
# "true", not "1": the SDK would otherwise update workload manifests from the configured NuGet sources
export DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE=true DOTNET_GENERATE_ASPNET_CERTIFICATE=false
export NUGET_PACKAGES="$work/packages" NUGET_HTTP_CACHE_PATH="$work/http-cache"
PUBLIC=Ardalis.GuardClauses
PUBLIC_VERSION=5.0.0
DEMO=Ws.Harness.Demo

config() {  # config <dir> <repository> [<token>]: a nuget.config with only that repository (plain HTTP allowed)
  mkdir -p "$1"
  {
    printf '<?xml version="1.0" encoding="utf-8"?>\n<configuration>\n  <packageSources>\n    <clear />\n'
    printf '    <add key="waystation" value="%s/repository/%s/index.json" protocolVersion="3" ' "$W" "$2"
    printf 'allowInsecureConnections="true" />\n  </packageSources>\n'
    if [ -n "${3:-}" ]; then
      printf '  <packageSourceCredentials>\n    <waystation>\n'
      printf '      <add key="Username" value="admin" />\n      <add key="ClearTextPassword" value="%s" />\n' "$3"
      printf '    </waystation>\n  </packageSourceCredentials>\n'
    fi
    printf '</configuration>\n'
  } > "$1/nuget.config"
}

consumer() {  # consumer <dir> <PackageReference lines>: a library project with these references
  mkdir -p "$1"
  cat > "$1/consumer.csproj" <<EOF
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net10.0</TargetFramework>
    <NuGetAudit>false</NuGetAudit>
  </PropertyGroup>
  <ItemGroup>
$2
  </ItemGroup>
</Project>
EOF
}

build() {  # build <dir>: restore (fresh package folder and HTTP cache) and build
  rm -rf "$NUGET_PACKAGES" "$NUGET_HTTP_CACHE_PATH"
  (cd "$1" && dotnet build -nologo -v q -c Release) > "$1/build.log" 2>&1 || { tail -n 40 "$1/build.log"; return 1; }
}

ref() { printf '    <PackageReference Include="%s" Version="[%s]" />' "$1" "$2"; }

step "dotnet restore + build through the proxy ($PUBLIC $PUBLIC_VERSION, verified against its catalog hash)"
config "$work/proxy" nuget-proxy
consumer "$work/proxy" "$(ref "$PUBLIC" "$PUBLIC_VERSION")"
cat > "$work/proxy/Use.cs" <<'EOF'
using Ardalis.GuardClauses;
public static class Use { public static string Check(string s) => Guard.Against.NullOrEmpty(s); }
EOF
build "$work/proxy" || fail 'restore through the proxy'
[ -f "$NUGET_PACKAGES/ardalis.guardclauses/$PUBLIC_VERSION/ardalis.guardclauses.$PUBLIC_VERSION.nupkg" ] \
  || fail 'the package is not in the package folder'

step 'dotnet list package --outdated through the proxy (registrations rewritten to the repository)'
(cd "$work/proxy" && dotnet list package --outdated > "$work/outdated.log" 2>&1) || { cat "$work/outdated.log"; fail 'dotnet list package'; }
grep -q -E "$PUBLIC|has no updates" "$work/outdated.log" || { cat "$work/outdated.log"; fail 'dotnet list package --outdated'; }

step 'pack a generated library (two versions)'
ws_login
ws_token '["read", "write", "delete"]' '["nuget-*"]'
token=$WS_TOKEN
mkdir -p "$work/lib"
cat > "$work/lib/$DEMO.csproj" <<EOF
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net10.0</TargetFramework>
    <PackageId>$DEMO</PackageId>
    <Authors>Waystation harness</Authors>
    <Description>A generated library for the Waystation client harness</Description>
    <NuGetAudit>false</NuGetAudit>
  </PropertyGroup>
</Project>
EOF
printf 'namespace Ws.Harness.Demo;\npublic static class Demo { public const string Version = "%s"; }\n' 0.1.0 > "$work/lib/Demo.cs"
config "$work/lib" nuget-hosted "$token"
(cd "$work/lib" && dotnet pack -nologo -v q -c Release -p:Version=0.1.0 -o "$work/out" > "$work/pack.log" 2>&1) \
  || { tail -n 30 "$work/pack.log"; fail 'dotnet pack 0.1.0'; }
printf 'namespace Ws.Harness.Demo;\npublic static class Demo { public const string Version = "%s"; }\n' 0.2.0 > "$work/lib/Demo.cs"
(cd "$work/lib" && dotnet pack -nologo -v q -c Release -p:Version=0.2.0 -o "$work/out" > "$work/pack.log" 2>&1) \
  || { tail -n 30 "$work/pack.log"; fail 'dotnet pack 0.2.0'; }

step 'the push API with X-NuGet-ApiKey alone: a wrong key is refused, an API token accepted'
# dotnet sends basic credentials from nuget.config when it has them (they win over the API key), so the header alone
# is exercised with curl, as dotnet sends it (multipart, part "package").
push_status() {  # push_status <api key>
  curl -s -o "$work/push.out" -w '%{http_code}' -X PUT -H "X-NuGet-ApiKey: $1" \
    -F "package=@$work/out/$DEMO.0.1.0.nupkg;filename=package.nupkg" "$W/repository/nuget-hosted/api/v2/package"
}
[ "$(push_status wrong-key)" = 401 ] || fail 'a push with a wrong API key was not refused with 401'
[ "$(push_status "$token")" = 201 ] || { cat "$work/push.out"; fail 'a push with X-NuGet-ApiKey alone'; }

step 'dotnet nuget push to the hosted repository (an API token as the API key and the nuget.config password)'
(cd "$work/lib" && dotnet nuget push "$work/out/$DEMO.0.1.0.nupkg" --source waystation --api-key "$token") \
  || fail 'push to the hosted repository'

step 'dotnet nuget push through the group (lands in its writable member)'
config "$work/push-group" nuget-group "$token"
(cd "$work/push-group" && dotnet nuget push "$work/out/$DEMO.0.2.0.nupkg" --source waystation --api-key "$token") \
  || fail 'push through the group'

step 'restore + build against the hosted repository (credentials from nuget.config)'
config "$work/hosted" nuget-hosted "$token"
consumer "$work/hosted" "$(ref "$DEMO" 0.1.0)"
build "$work/hosted" || fail 'restore from the hosted repository'

step 'restore + build through the group: the pushed library and the proxied package'
config "$work/group" nuget-group "$token"
consumer "$work/group" "$(ref "$DEMO" 0.2.0)
$(ref "$PUBLIC" "$PUBLIC_VERSION")"
build "$work/group" || fail 'restore through the group'
[ -f "$NUGET_PACKAGES/ws.harness.demo/0.2.0/ws.harness.demo.0.2.0.nupkg" ] || fail '0.2.0 not restored'

step 'anonymous restore from the hosted repository is refused'
config "$work/anonymous" nuget-hosted
consumer "$work/anonymous" "$(ref "$DEMO" 0.1.0)"
if build "$work/anonymous" > /dev/null 2>&1; then
  fail 'an anonymous restore from the hosted repository succeeded'
fi

step 'dotnet nuget delete unlists 0.2.0; dotnet package search shows 0.1.0'
(cd "$work/hosted" && dotnet nuget delete "$DEMO" 0.2.0 --source waystation --api-key "$token" --non-interactive) \
  || fail 'dotnet nuget delete'
(cd "$work/hosted" && dotnet package search Ws.Harness --source waystation --configfile nuget.config --format json \
  > "$work/search.json") || fail 'dotnet package search'
grep -q '"latestVersion": "0.1.0"' "$work/search.json" || { cat "$work/search.json"; fail 'search after unlisting'; }
step 'unlisted versions stay restorable'
build "$work/group" || fail 'restore of an unlisted version'

pass
