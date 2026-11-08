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

pass
