#!/bin/sh
# apt through Waystation apt proxies (client-debian: Debian 13, client-ubuntu: Ubuntu 26.04; HARNESS_CLIENT says
# which). apt keeps verifying the distribution's own signature (Signed-By unchanged): Waystation passes InRelease
# through untouched, so a modified Release or index would fail apt-get update. Runs identically in every mode;
# assertions use only what survives fixture scrubbing.
set -eu
. /harness/lib.sh

repo="$WAYSTATION_URL/repository"
case "$HARNESS_CLIENT" in
  debian)
    main=debian sec=debian-security suite=trixie-updates sec_suite=trixie-security
    keyring=/usr/share/keyrings/debian-archive-keyring.pgp
    packages=rfkill check='rfkill --version'
    ;;
  ubuntu)
    main=ubuntu sec=ubuntu-security suite=resolute-updates sec_suite=resolute-security
    keyring=/usr/share/keyrings/ubuntu-archive-keyring.gpg
    packages='msr-tools attr' check='getfattr --version'
    ;;
  *) fail "unknown client $HARNESS_CLIENT" ;;
esac
export DEBIAN_FRONTEND=noninteractive

step "point apt at Waystation ($main: deb822 .sources, $sec: one-line .list)"
rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*
cat > /etc/apt/sources.list.d/waystation.sources <<SOURCES
Types: deb
URIs: $repo/$main/
Suites: $suite
Components: main
Signed-By: $keyring
SOURCES
echo "deb [signed-by=$keyring] $repo/$sec/ $sec_suite main" > /etc/apt/sources.list.d/waystation-security.list
# Small fixtures: compressed Packages only, no translations/contents (the owner's fixture decision; README).
cat > /etc/apt/apt.conf.d/99waystation-harness <<'CONF'
Acquire::Languages "none";
#clear Acquire::CompressionTypes::Order;
Acquire::CompressionTypes::Order { "xz"; };
Acquire::IndexTargets::deb::Contents-deb::DefaultEnabled "false";
Acquire::Retries "0";
APT::Get::Assume-Yes "true";
APT::Install-Recommends "false";
CONF

update() {  # update [apt options]: apt-get update that fails on any warning or error
  rm -rf /var/lib/apt/lists/*
  apt-get update --error-on=any "$@" > /tmp/update.log 2>&1 || { cat /tmp/update.log; fail 'apt-get update'; }
  cat /tmp/update.log
  grep -q '^Err' /tmp/update.log && fail 'apt-get update reported errors'
  grep -q "$repo/$main.* $suite InRelease" /tmp/update.log || fail "no InRelease from $main"
  grep -q "$repo/$sec.* $sec_suite InRelease" /tmp/update.log || fail "no InRelease from $sec"
}

step 'apt-get update (InRelease verified by apt, indexes fetched by-hash)'
update
for p in $packages; do
  apt-cache policy "$p" | grep -q "$repo/" || fail "$p is not offered by Waystation: $(apt-cache policy "$p")"
done

step "apt-get install $packages (pool files validated against the cached Packages)"
# shellcheck disable=SC2086
apt-get install $packages || fail 'apt-get install'
for p in $packages; do
  dpkg-query -W -f='${Status}\n' "$p" | grep -q 'install ok installed' || fail "$p is not installed"
done
$check >/dev/null || fail "$check"

step 'a cached pool file is a cache hit, byte-identical to its Packages listing'
p=${packages%% *}
uri=$(cd /tmp && apt-get download --print-uris "$p" | cut -d"'" -f2)
sha=$(apt-cache show --no-all-versions "$p" | sed -n 's/^SHA256: //p')
case "$uri" in "$repo/"*/pool/*) ;; *) fail "unexpected download URI: $uri" ;; esac
code=$(_ws_http GET "$uri")
[ "$code" = 200 ] || fail "pool file $uri returned $code"
grep -qi '^x-ws-cache: HIT' /tmp/ws-api.headers || fail "pool file not a cache hit: $(cat /tmp/ws-api.headers)"
[ "$(sha256sum /tmp/ws-api.out | cut -d' ' -f1)" = "$sha" ] || fail 'pool file does not match its SHA256'

step 'apt-get update without by-hash (plain index paths, served by the hash the cached Release lists)'
update -o Acquire::By-Hash=no

step 'reinstall from an empty local cache (served by the Waystation cache)'
apt-get clean
# shellcheck disable=SC2086
apt-get install --reinstall $packages || fail 'apt-get install --reinstall'

step 'browse API: suites from the cached Release files, installed packages listed as cached and verified'
ws_login
ws_api GET "/repositories/$main/apt/suites" | grep -q "\"suite\":\"$suite\"" || fail "$suite is not a suite of $main"
for p in $packages; do
  found=
  for r in "$main:$suite" "$sec:$sec_suite"; do
    ws_api GET "/repositories/${r%%:*}/apt/packages?suite=${r#*:}&cached=true&q=$p&limit=500" \
      | grep -q "\"name\":\"$p\",.*\"cached\":true,\"verified\":true" && found=1
  done
  [ -n "$found" ] || fail "$p is not listed as cached and verified by the browse API"
done

step 'API token in auth.conf (basic auth, token as the password): update with it, refused once revoked'
# apt applies auth.conf entries without a scheme to https only; the harness stack is plain http.
ws_token '["read"]' "[\"$main\",\"$sec\"]"
token_id=$WS_TOKEN_ID
mkdir -p /etc/apt/auth.conf.d
printf 'machine %s/repository/\nlogin ci-bot\npassword %s\n' "$WAYSTATION_URL" "$WS_TOKEN" \
  > /etc/apt/auth.conf.d/waystation.conf
chmod 600 /etc/apt/auth.conf.d/waystation.conf
update
ws_revoke "$token_id"
rm -rf /var/lib/apt/lists/*
if apt-get update --error-on=any > /tmp/revoked.log 2>&1; then
  cat /tmp/revoked.log; fail 'apt-get update worked with a revoked token'
fi
grep -q '401' /tmp/revoked.log || { cat /tmp/revoked.log; fail 'revoked token: no 401'; }
rm -f /etc/apt/auth.conf.d/waystation.conf

pass
