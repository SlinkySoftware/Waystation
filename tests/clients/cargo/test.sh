#!/bin/sh
# Cargo through Waystation (client-cargo container). cargo builds a tiny program with two small crates.io
# dependencies (cfg-if, itoa) through the sparse proxy, crates.io replaced by it (source replacement); publishes a
# crate to the hosted registry with an API token (cargo login's token, sent as the bare Authorization header), builds
# a program against it, yanks and un-yanks it, and is refused without a token; then resolves everything through the
# group, which needs credentials (config.json answers 401, cargo retries with its token: auth-required).
# cargo never talks to the upstreams: the fake upstream fails on any request it does not expect.
set -eu
. /harness/lib.sh

W=$WAYSTATION_URL
work=/tmp/cargo-harness
rm -rf "$work"
mkdir -p "$work"
export CARGO_TERM_COLOR=never CARGO_TERM_PROGRESS_WHEN=never CARGO_NET_RETRY=1

home() {  # home <name>: a fresh CARGO_HOME (registry cache, credentials) for one scenario, config on stdin
  export CARGO_HOME="$work/$1/home"
  rm -rf "$work/$1"
  mkdir -p "$CARGO_HOME"
  cat > "$CARGO_HOME/config.toml"
}

project() {  # project <dir> <name> <dependencies> <main.rs>: a binary crate
  mkdir -p "$1/src"
  printf '[package]\nname = "%s"\nversion = "0.1.0"\nedition = "2021"\npublish = false\n\n[dependencies]\n%s\n' \
    "$2" "$3" > "$1/Cargo.toml"
  printf '%s\n' "$4" > "$1/src/main.rs"
}

run() {  # run <dir> <label> <expected output>: cargo run (writes Cargo.lock), from the scenario's CARGO_HOME
  (cd "$1" && cargo run --quiet > "$work/out.txt" 2> "$work/cargo.log") \
    || { grep -v -i token "$work/cargo.log" | tail -n 30; fail "cargo run: $2"; }
  [ "$(cat "$work/out.txt")" = "$3" ] || fail "unexpected program output ($2): $(cat "$work/out.txt")"
}

APP_DEPS='cfg-if = "=1.0.0"
itoa = "=1.0.11"'
APP_MAIN='fn main() {
    let mut buffer = itoa::Buffer::new();
    cfg_if::cfg_if! {
        if #[cfg(unix)] { println!("{} unix", buffer.format(42)); } else { println!("{} other", buffer.format(42)); }
    }
}'

# ---------------------------------------------------------------------------- proxy
step 'config.json of the proxy points downloads and the API at Waystation'
config=$(curl -fsS "$W/repository/cargo-proxy/index/config.json") || fail 'config.json'
printf '%s' "$config" | grep -q "\"dl\":\"[^\"]*/repository/cargo-proxy/crates/{crate}/{crate}-{version}.crate\"" \
  || fail "unexpected config.json: $config"
printf '%s' "$config" | grep -q '"auth-required":false' || fail "the proxy asks for credentials: $config"

step 'cargo run through the proxy (crates.io replaced by it)'
home proxy <<EOF
[registries.waystation]
index = "sparse+$W/repository/cargo-proxy/index/"

[source.crates-io]
replace-with = "waystation"
EOF
project "$work/proxy/app" harness-app "$APP_DEPS" "$APP_MAIN"
run "$work/proxy/app" proxy '42 unix'
grep -q '^checksum = "[0-9a-f]\{64\}"' "$work/proxy/app/Cargo.lock" || fail 'Cargo.lock has no checksums'
ls "$CARGO_HOME"/registry/cache/*/itoa-1.0.11.crate >/dev/null 2>&1 || fail 'itoa-1.0.11.crate was not downloaded'

step 'the cached crates are browsable'
curl -fsS "$W/api/v1/repositories/cargo-proxy/cargo/versions?crate=itoa" | grep -q '"version":"1.0.11"' \
  || fail 'itoa 1.0.11 is not listed by the browse API'

# ---------------------------------------------------------------------------- hosted
ws_login
ws_token '["read", "write"]'
writer=$WS_TOKEN
ws_token '["read"]'
reader=$WS_TOKEN

step 'hosted: anonymous requests get the 401 challenge (cargo then sends its token)'
code=$(curl -s -o /dev/null -w '%{http_code}' "$W/repository/cargo-hosted/index/config.json")
[ "$code" = 401 ] || fail "anonymous config.json answered $code"

step 'hosted: cargo publish with an API token'
home hosted <<EOF
[registry]
global-credential-providers = ["cargo:token"]

[registries.harness]
index = "sparse+$W/repository/cargo-hosted/index/"
EOF
mkdir -p "$work/hosted/lib/src"
cat > "$work/hosted/lib/Cargo.toml" <<'EOF'
[package]
name = "ws-harness-lib"
version = "0.1.0"
edition = "2021"
description = "Waystation client harness crate"
license = "MIT"
publish = ["harness"]
EOF
printf 'pub fn answer() -> u32 {\n    42\n}\n' > "$work/hosted/lib/src/lib.rs"
printf '%s\n' "$writer" | cargo login --registry harness > /dev/null 2> "$work/cargo.log" \
  || { tail -n 20 "$work/cargo.log"; fail 'cargo login'; }
(cd "$work/hosted/lib" && cargo publish --registry harness --allow-dirty 2> "$work/cargo.log") \
  || { grep -v -i token "$work/cargo.log" | tail -n 30; fail 'cargo publish'; }
(cd "$work/hosted/lib" && cargo publish --registry harness --allow-dirty 2> "$work/cargo.log") \
  && fail 'publishing the same version again succeeded'

step 'hosted: build a program against the published crate'
LIB_DEP='ws-harness-lib = { version = "0.1.0", registry = "harness" }'
LIB_MAIN='fn main() { println!("{}", ws_harness_lib::answer()); }'
project "$work/hosted/app" harness-consumer "$LIB_DEP" "$LIB_MAIN"
run "$work/hosted/app" hosted 42

step 'hosted: a yanked version is not selected for a new lock file; un-yank restores it'
cargo yank --registry harness --version 0.1.0 ws-harness-lib 2> "$work/cargo.log" \
  || { grep -v -i token "$work/cargo.log" | tail -n 20; fail 'cargo yank'; }
rm -f "$work/hosted/app/Cargo.lock"
(cd "$work/hosted/app" && cargo generate-lockfile 2> "$work/cargo.log") && fail 'a yanked version was selected'
cargo yank --undo --registry harness --version 0.1.0 ws-harness-lib 2> "$work/cargo.log" \
  || { grep -v -i token "$work/cargo.log" | tail -n 20; fail 'cargo yank --undo'; }
run "$work/hosted/app" 'hosted after un-yank' 42

step 'hosted: without a token cargo is refused'
home anonymous <<EOF
[registry]
global-credential-providers = ["cargo:token"]

[registries.harness]
index = "sparse+$W/repository/cargo-hosted/index/"
EOF
project "$work/anonymous/app" harness-consumer "$LIB_DEP" "$LIB_MAIN"
(cd "$work/anonymous/app" && cargo generate-lockfile 2> "$work/cargo.log") && fail 'anonymous resolve succeeded'
grep -q -i 'token\|401\|unauthorized' "$work/cargo.log" || { tail -n 20 "$work/cargo.log"; fail 'unexpected error'; }

# ---------------------------------------------------------------------------- group
step 'group: crates.io and the hosted crate through one registry that needs credentials (read-only token)'
home group <<EOF
[registry]
global-credential-providers = ["cargo:token"]

[registries.all]
index = "sparse+$W/repository/cargo-group/index/"

[source.crates-io]
replace-with = "all"
EOF
export CARGO_REGISTRIES_ALL_TOKEN="$reader"
project "$work/group/app" harness-group "$APP_DEPS
ws-harness-lib = { version = \"0.1.0\", registry = \"all\" }" 'fn main() {
    let mut buffer = itoa::Buffer::new();
    cfg_if::cfg_if! { if #[cfg(unix)] { println!("{} {}", buffer.format(41), ws_harness_lib::answer()); } }
}'
run "$work/group/app" group '41 42'
unset CARGO_REGISTRIES_ALL_TOKEN

pass
