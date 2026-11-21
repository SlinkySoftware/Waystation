#!/bin/sh
# pnpm, Yarn 1 (classic), Yarn 4 (berry) and Bun through the Waystation npm proxy (client-nodepm container; runs after
# the npm client, against the same stack). Each installs the lockfile-pinned project of pinned/ from its own
# committed lockfile (frozen: the lockfile must not change) on a cold client cache, checks the pinned versions and
# runs its audit command. Lockfiles name registry.npmjs.org (or no registry at all); the clients do not trust the
# harness CA, so a request that bypassed Waystation would fail TLS.
set -eu
. /harness/lib.sh

registry="$WAYSTATION_URL/repository/npm/"
pinned=/harness/npm/pinned
work=/tmp/npm-pm
rm -rf "$work"
export CI=1 NO_COLOR=1 FORCE_COLOR=0

project() {  # project <dir> <lockfile in pinned/> <lockfile name>: a fresh copy of the pinned project
  mkdir -p "$1"
  cp "$pinned/package.json" "$1/"
  cp "$pinned/$2" "$1/$3"
}
verify() {  # verify <dir> <client>: the pinned versions are installed and work
  (cd "$1" && node -e '
    const isOdd = require("is-odd"); const is = require("@sindresorhus/is");
    if (!isOdd(3) || isOdd(4) || !is.string("x")) process.exit(1);
    const v = require(require.resolve("is-number/package.json", { paths: [require.resolve("is-odd")] })).version;
    if (v !== "6.0.0") { console.error("transitive is-number:", v); process.exit(1); }
  ') || fail "$2: the pinned packages are not installed as locked"
}
unchanged() {  # unchanged <dir> <lockfile name> <expected lockfile> <client>: frozen means byte-identical
  cmp -s "$1/$2" "$3" || { diff "$3" "$1/$2" || true; fail "$4 changed the lockfile"; }
}

# ---------------------------------------------------------------------------- pnpm
step "pnpm $(pnpm --version): install --frozen-lockfile (pnpm-lock.yaml v9, integrity only, no URLs)"
d="$work/pnpm"
project "$d" pnpm-lock.yaml pnpm-lock.yaml
printf 'registry=%s\n' "$registry" > "$d/.npmrc"
(cd "$d" && pnpm install --frozen-lockfile --store-dir "$work/pnpm-store" --reporter=append-only) || fail 'pnpm install'
verify "$d" pnpm
unchanged "$d" pnpm-lock.yaml "$pinned/pnpm-lock.yaml" pnpm
step 'pnpm audit (relayed to the upstream)'
(cd "$d" && pnpm audit --audit-level critical) || fail 'pnpm audit'

# ---------------------------------------------------------------------------- Yarn 1
step "yarn $(yarn --version): install --frozen-lockfile (yarn.lock v1, tarball URLs pointed at Waystation)"
d="$work/yarn1"
project "$d" yarn-classic.lock yarn.lock
# Yarn 1 fetches the lockfile's `resolved` URLs verbatim, whatever the configured registry: a project that uses
# Waystation commits a yarn.lock whose URLs name it (generated against it, or rewritten like here; clients/npm.md).
sed -i "s#https://registry.npmjs.org/#$registry#" "$d/yarn.lock"
cp "$d/yarn.lock" "$work/yarn-classic.lock"
printf 'registry "%s"\n' "$registry" > "$d/.yarnrc"
(cd "$d" && yarn install --frozen-lockfile --non-interactive --no-progress --cache-folder "$work/yarn1-cache") \
  || fail 'yarn install'
verify "$d" yarn1
unchanged "$d" yarn.lock "$work/yarn-classic.lock" yarn1
step 'yarn audit (relayed to the upstream)'
(cd "$d" && yarn audit --level critical --non-interactive) || fail 'yarn audit'

# ---------------------------------------------------------------------------- Yarn 4 (berry)
berry() { node /opt/berry/yarn.cjs "$@"; }
step "yarn $(cd /tmp && berry --version) (berry): install --immutable"
d="$work/berry"
host=${WAYSTATION_URL#http://}; host=${host%%[:/]*}  # unsafeHttpWhitelist takes host names, no port
project "$d" yarn-berry.lock yarn.lock
cat > "$d/.yarnrc.yml" <<YML
nodeLinker: node-modules
npmRegistryServer: "${registry%/}"
unsafeHttpWhitelist: ["$host"]
enableGlobalCache: false
enableTelemetry: false
YML
(cd "$d" && berry install --immutable) || fail 'yarn (berry) install'
verify "$d" berry
unchanged "$d" yarn.lock "$pinned/yarn-berry.lock" berry
step 'yarn npm audit (berry; relayed to the upstream)'
(cd "$d" && berry npm audit --severity critical) || fail 'yarn npm audit'

# ---------------------------------------------------------------------------- Bun
step "bun $(bun --version): install --frozen-lockfile (bun.lock)"
d="$work/bun"
project "$d" bun.lock bun.lock
printf '[install]\nregistry = "%s"\ncache = "%s"\n' "$registry" "$work/bun-cache" > "$d/bunfig.toml"
(cd "$d" && bun install --frozen-lockfile) || fail 'bun install'
verify "$d" bun
unchanged "$d" bun.lock "$pinned/bun.lock" bun
step 'bun audit (relayed to the upstream)'
(cd "$d" && bun audit --audit-level=critical) || fail 'bun audit'

pass
