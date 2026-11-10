#!/bin/sh
# npm through a Waystation npm proxy (client-node container, npm 11). The client never talks to the upstream: it
# does not trust the harness CA, so any request that bypassed Waystation would fail TLS.
set -eu
. /harness/lib.sh

registry="$WAYSTATION_URL/repository/npm/"
export npm_config_registry="$registry"
export npm_config_audit=false npm_config_fund=false npm_config_update_notifier=false npm_config_loglevel=warn
work=/tmp/npm-harness
rm -rf "$work"
mkdir -p "$work"

# A lockfile names a registry.npmjs.org URL for every package it pins.
check_resolved() {  # check_resolved <package-lock.json>: every package resolved through Waystation
  node -e '
    const lock = require(process.argv[1]);
    const bad = Object.entries(lock.packages).filter(([k, p]) => k && !String(p.resolved).startsWith(process.argv[2]));
    if (bad.length) { console.error("not resolved through Waystation:", bad.map(([k, p]) => `${k} ${p.resolved}`)); process.exit(1); }
  ' "$1" "$registry"
}

step 'npm ping'
npm ping >/dev/null || fail 'npm ping'

step 'lockfile-pinned install (npm ci, cold cache: tarball integrity from the packument)'
mkdir "$work/pinned"
cp /harness/npm/pinned/package.json /harness/npm/pinned/package-lock.json "$work/pinned/"
(cd "$work/pinned" && npm ci --cache "$work/cache-pinned") || fail 'npm ci'
(cd "$work/pinned" && node -e '
  const isOdd = require("is-odd"); const is = require("@sindresorhus/is");
  if (!isOdd(3) || isOdd(4) || !is.string("x")) process.exit(1);
') || fail 'pinned packages do not work'
[ "$(node -p 'require(process.argv[1]).version' "$work/pinned/node_modules/is-number/package.json")" = 6.0.0 ] \
  || fail 'transitive is-number is not the pinned 6.0.0'

step 'lockfile-free install (packuments: plain and scoped names, tarball URLs rewritten)'
mkdir "$work/fresh"
(cd "$work/fresh" && npm init -y >/dev/null && \
  npm install --cache "$work/cache-fresh" is-number@^7.0.0 is-odd@3.0.1 @sindresorhus/is@4.6.0) || fail 'npm install'
check_resolved "$work/fresh/package-lock.json" || fail 'lockfile resolved URLs'
[ "$(node -p 'require(process.argv[1]).version' "$work/fresh/node_modules/is-number/package.json")" = 7.0.0 ] \
  || fail 'is-number@^7 did not resolve to 7.0.0'
(cd "$work/fresh" && node -e 'if (!require("is-number")(5)) process.exit(1)') || fail 'is-number does not work'

step 'second install from a cold npm cache (served from the Waystation cache)'
mkdir "$work/again"
cp "$work/fresh/package.json" "$work/fresh/package-lock.json" "$work/again/"
(cd "$work/again" && npm ci --cache "$work/cache-again") || fail 'npm ci (warm Waystation cache)'

step 'version documents: npm view <pkg>@<version>, corepack-style GET <registry>/<pkg>/<version or tag>'
[ "$(npm view --cache "$work/cache-view-proxy" is-odd@3.0.1 version)" = 3.0.1 ] || fail 'npm view is-odd@3.0.1'
[ "$(npm view --cache "$work/cache-view-proxy" @sindresorhus/is@4.6.0 name)" = @sindresorhus/is ] \
  || fail 'npm view @sindresorhus/is@4.6.0'
# corepack resolves pnpm/yarn with a plain fetch of <registry>/<name>/<version> and reads dist.tarball/integrity
node -e '
  const reg = process.argv[1];
  const get = async (p) => { const r = await fetch(reg + p); return [r.status, await r.json()]; };
  (async () => {
    for (const [p, name, version] of [
      ["is-odd/3.0.1", "is-odd", "3.0.1"],
      ["is-odd/latest", "is-odd", null],
      ["@sindresorhus/is/4.6.0", "@sindresorhus/is", "4.6.0"],
      ["@sindresorhus%2fis/4.6.0", "@sindresorhus/is", "4.6.0"],
    ]) {
      const [status, doc] = await get(p);
      const tarball = String(doc.dist && doc.dist.tarball);
      if (status !== 200 || doc.name !== name || (version && doc.version !== version) || !tarball.startsWith(reg)
          || !doc.dist.integrity) throw new Error(`${p}: ${status} ${JSON.stringify(doc).slice(0, 300)}`);
    }
    const [status, doc] = await get("is-odd/no-such-tag");  // a tag: answered from the cached packument
    if (status !== 404 || doc.error !== "version not found: no-such-tag") throw new Error(`unknown tag: ${status} ${JSON.stringify(doc)}`);
  })().catch((e) => { console.error(e.message); process.exit(1); });
' "$registry" || fail 'version documents'

step 'npm audit (POST relayed to the upstream, anonymous)'
(cd "$work/pinned" && npm audit --audit-level=critical --cache "$work/cache-pinned") || fail 'npm audit'

step 'browse API (anonymous): cached packages and versions'
node -e '
  const api = process.env.WAYSTATION_URL + "/api/v1/repositories/npm/npm";
  const get = async (p) => { const r = await fetch(api + p); if (!r.ok) throw new Error(p + " " + r.status); return r.json(); };
  (async () => {
    for (let i = 0; ; i++) {  // packument summaries are recorded in the background
      const names = (await get("/packages?limit=500")).items.map((p) => p.name);
      if (["is-odd", "is-number", "@sindresorhus/is"].every((n) => names.includes(n))) break;
      if (i > 20) throw new Error("packages not listed: " + names);
      await new Promise((r) => setTimeout(r, 500));
    }
    const odd = (await get("/packages?q=is-odd")).items.find((p) => p.name === "is-odd");
    if (!odd || odd.cached_versions < 1 || odd.size_bytes <= 0) throw new Error("is-odd: " + JSON.stringify(odd));
    const versions = await get("/versions?package=" + encodeURIComponent("@sindresorhus/is"));
    const v = versions.items.find((x) => x.version === "4.6.0");
    if (!v || !v.cached || !v.integrity) throw new Error("@sindresorhus/is 4.6.0: " + JSON.stringify(v));
  })().catch((e) => { console.error(e.message); process.exit(1); });
' || fail 'browse API'

step 'API token in .npmrc (_authToken): install with it, narrowed, refused once revoked'
ws_login
auth_line() { printf '%s:_authToken=%s\n' "${registry#http:}" "$1"; }  # //host:port/repository/npm/:_authToken=...
ws_token '["read"]' '["npm"]'
token=$WS_TOKEN token_id=$WS_TOKEN_ID
mkdir "$work/token"
cp "$work/fresh/package.json" "$work/fresh/package-lock.json" "$work/token/"
auth_line "$token" > "$work/token/.npmrc"
(cd "$work/token" && npm ci --cache "$work/cache-token") || fail 'npm ci with an API token'
ws_token '["read"]' '["docker-*"]'
auth_line "$WS_TOKEN" > "$work/token/.npmrc"
if (cd "$work/token" && npm view --cache "$work/cache-narrow" is-odd@3.0.1 version > "$work/narrow.log" 2>&1); then
  fail 'a token limited to docker-* could read the npm repository'
fi
grep -q 'E404' "$work/narrow.log" || fail "narrowed token: $(cat "$work/narrow.log")"
ws_revoke "$token_id"
auth_line "$token" > "$work/token/.npmrc"
if (cd "$work/token" && npm view --cache "$work/cache-revoked" is-odd@3.0.1 version > "$work/revoked.log" 2>&1); then
  fail 'a revoked token still works'
fi
grep -q 'E401' "$work/revoked.log" || fail "revoked token: $(cat "$work/revoked.log")"

# ---------------------------------------------------------------------------- hosted + group (T-P5-NPMH)
hosted="$WAYSTATION_URL/repository/npm-hosted/"
group="$WAYSTATION_URL/repository/npm-all/"
pkg='@ws-harness-test/hello-waystation'  # not on the public registry: the proxy member answers 404 (recorded)
make_pkg() {  # make_pkg <dir> <version>: a tiny package whose publishConfig names the group
  mkdir -p "$1"
  printf '{"name":"%s","version":"%s","main":"index.js","license":"MIT","publishConfig":{"registry":"%s"}}\n' \
    "$pkg" "$2" "$group" > "$1/package.json"
  printf 'module.exports = () => "hello %s";\n' "$2" > "$1/index.js"
}
npmrc="$work/npmrc-user"  # the user's ~/.npmrc for the steps below
: > "$npmrc"

step 'npm login --auth-type=legacy against the group (username + password -> an API token in .npmrc)'
# npm reads each answer with a fresh readline on stdin: feed the password only once the username prompt is done.
{ printf 'admin\n'; sleep 3; printf '%s\n' "$HARNESS_ADMIN_PASSWORD"; } | \
  npm login --auth-type=legacy --registry "$group" --userconfig "$npmrc" --loglevel notice > "$work/login.log" 2>&1 \
  || fail "npm login: $(cat "$work/login.log")"
login_token=$(sed -n 's/.*_authToken=\(wst_.*\)$/\1/p' "$npmrc")
[ -n "$login_token" ] || fail 'npm login stored no API token'
[ "$(npm whoami --registry "$group" --userconfig "$npmrc")" = admin ] || fail 'npm whoami after npm login'

step 'npm publish through the group (lands in the hosted member) and straight to the hosted repository'
make_pkg "$work/pkg-1.0.0" 1.0.0
(cd "$work/pkg-1.0.0" && npm publish --userconfig "$npmrc" --cache "$work/cache-pub") || fail 'npm publish via the group'
ws_token '["read","write","delete"]'
publisher="$work/npmrc-publisher"
printf '%s:_authToken=%s\n' "${hosted#http:}" "$WS_TOKEN" > "$publisher"
make_pkg "$work/pkg-1.1.0" 1.1.0
(cd "$work/pkg-1.1.0" && npm publish --registry "$hosted" --userconfig "$publisher" --cache "$work/cache-pub") \
  || fail 'npm publish to the hosted repository'
if (cd "$work/pkg-1.1.0" && npm publish --registry "$hosted" --userconfig "$publisher" --cache "$work/cache-pub" \
      > "$work/republish.log" 2>&1); then
  fail 'republishing 1.1.0 was accepted (disable_redeploy)'
fi
grep -qi 'previously published' "$work/republish.log" || fail "republish: $(cat "$work/republish.log")"

step 'install the hosted package and a proxied one through the group'
mkdir "$work/consumer"
(cd "$work/consumer" && npm init -y >/dev/null && \
  npm install --registry "$group" --userconfig "$npmrc" --cache "$work/cache-consumer" "$pkg@^1.0.0" is-number@7.0.0) \
  || fail 'npm install through the group'
node -e '
  const lock = require(process.argv[1]);
  const bad = Object.entries(lock.packages).filter(([k, p]) => k && !String(p.resolved).startsWith(process.argv[2]));
  if (bad.length) { console.error("not resolved through the group:", bad); process.exit(1); }
' "$work/consumer/package-lock.json" "$group" || fail 'lockfile resolved URLs (group)'
(cd "$work/consumer" && node -e '
  if (require(process.argv[1])() !== "hello 1.1.0" || !require("is-number")(5)) process.exit(1);
' "$pkg") || fail 'installed packages do not work'

step 'npm dist-tag add / ls / rm'
npm dist-tag add "$pkg@1.0.0" stable --registry "$group" --userconfig "$npmrc" >/dev/null || fail 'dist-tag add'
npm dist-tag ls "$pkg" --registry "$group" --userconfig "$npmrc" > "$work/tags.txt" || fail 'dist-tag ls'
grep -q '^stable: 1.0.0$' "$work/tags.txt" && grep -q '^latest: 1.1.0$' "$work/tags.txt" \
  || fail "dist-tags: $(cat "$work/tags.txt")"
npm dist-tag rm "$pkg" stable --registry "$hosted" --userconfig "$publisher" >/dev/null || fail 'dist-tag rm'

step 'npm deprecate (read with ?write=true through the group, written to the hosted member)'
npm deprecate "$pkg@1.0.0" 'use 1.1.0' --registry "$group" --userconfig "$npmrc" || fail 'npm deprecate'
deprecated=$(npm view "$pkg@1.0.0" deprecated --registry "$group" --userconfig "$npmrc" --cache "$work/cache-view")
[ "$deprecated" = 'use 1.1.0' ] || fail "deprecation message not served: $deprecated"

step 'version documents of the hosted package (group: from the merged packument; hosted: generated)'
node -e '
  const [group, hosted, pkg, token] = process.argv.slice(1);
  const get = async (url) => {
    const r = await fetch(url, { headers: { authorization: `Bearer ${token}` } });
    return [r.status, await r.json()];
  };
  const encoded = pkg.replace("/", "%2f");
  (async () => {
    for (const [url, version, base] of [
      [`${group}${pkg}/1.0.0`, "1.0.0", group],
      [`${group}${encoded}/latest`, "1.1.0", group],
      [`${hosted}${encoded}/1.1.0`, "1.1.0", hosted],
    ]) {
      const [status, doc] = await get(url);
      if (status !== 200 || doc.version !== version || !String(doc.dist && doc.dist.tarball).startsWith(base))
        throw new Error(`${url}: ${status} ${JSON.stringify(doc).slice(0, 300)}`);
    }
    const [status, doc] = await get(`${group}${pkg}/1.0.0`);
    if (doc.deprecated !== "use 1.1.0") throw new Error(`deprecated: ${JSON.stringify(doc)}`);
    const [missing] = await get(`${hosted}${pkg}/9.9.9`);
    if (missing !== 404) throw new Error(`unknown version: ${missing}`);
  })().catch((e) => { console.error(e.message); process.exit(1); });
' "$group" "$hosted" "$pkg" "$login_token" || fail 'hosted/group version documents'

step 'npm unpublish one version, then the whole package'
npm unpublish "$pkg@1.0.0" --registry "$hosted" --userconfig "$publisher" || fail 'npm unpublish <pkg>@1.0.0'
versions=$(npm view "$pkg" versions --json --registry "$hosted" --userconfig "$publisher" --cache "$work/cache-view2" \
  | tr -d ' \n')
[ "$versions" = '"1.1.0"' ] || [ "$versions" = '["1.1.0"]' ] || fail "versions after unpublish: $versions"
npm unpublish "$pkg" --force --registry "$hosted" --userconfig "$publisher" || fail 'npm unpublish --force'
if npm view "$pkg" --registry "$hosted" --userconfig "$publisher" --cache "$work/cache-view3" > /dev/null 2>&1; then
  fail 'the package is still there after npm unpublish --force'
fi

step 'npm logout revokes the token npm login created'
npm logout --registry "$group" --userconfig "$npmrc" || fail 'npm logout'
[ ! -f "$npmrc" ] || ! grep -q '_authToken' "$npmrc" || fail 'npm logout left the token in .npmrc'
printf '%s:_authToken=%s\n' "${group#http:}" "$login_token" > "$work/npmrc-revoked"
if npm whoami --registry "$group" --userconfig "$work/npmrc-revoked" > "$work/logout.log" 2>&1; then
  fail 'the token npm login created still works after npm logout'
fi
grep -q 'E401' "$work/logout.log" || fail "after logout: $(cat "$work/logout.log")"

pass
