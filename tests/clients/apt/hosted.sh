#!/bin/bash
# Hosted apt (T-P5-APTH), run by test.sh in the debian client: build a .deb with dpkg-deb, upload it (PUT, the
# request `curl --upload-file` sends; debian-slim has no curl, so bash sends it), wait for the worker to publish a
# signed dists/, then apt (Signed-By the key the repository serves, an API token in auth.conf) updates with signature
# verification, installs, upgrades to a second upload, and no longer sees a deleted version.
set -eu
. /harness/lib.sh

repo=apt-hosted
base="$WAYSTATION_URL/repository/$repo"
ws_login
ws_token '["read","write","delete"]' "[\"$repo\"]"
token=$WS_TOKEN
basic=$(printf 'ci-bot:%s' "$token" | base64 -w0)

# send <METHOD> <URL> [file]: an HTTP/1.0 request with the token as basic auth ($basic; none when empty) and the file
# as the body. Prints the status; the body goes to /tmp/ws-send.out.
send() {
  rest=${2#http://}; hostport=${rest%%/*}; path=/${rest#*/}
  host=${hostport%:*}; port=${hostport##*:}; [ "$port" != "$hostport" ] || port=80
  exec 3<>"/dev/tcp/$host/$port"
  {
    printf '%s %s HTTP/1.0\r\nHost: %s\r\n' "$1" "$path" "$hostport"
    if [ -n "$basic" ]; then printf 'Authorization: Basic %s\r\n' "$basic"; fi
    if [ $# -ge 3 ]; then
      printf 'Content-Type: application/vnd.debian.binary-package\r\nContent-Length: %s\r\n\r\n' "$(stat -c %s "$3")"
      cat "$3"
    else
      printf '\r\n'
    fi
  } >&3
  IFS= read -r line <&3; status=${line#* }; status=${status%% *}
  while IFS= read -r line <&3; do line=${line%$'\r'}; [ -n "$line" ] || break; done
  cat <&3 > /tmp/ws-send.out
  exec 3<&-
  printf '%s\n' "$status"
}

build() {  # build <version>: /tmp/ws-hello_<version>_all.deb
  rm -rf /tmp/wsdeb && mkdir -p /tmp/wsdeb/DEBIAN /tmp/wsdeb/usr/share/ws-hello
  cat > /tmp/wsdeb/DEBIAN/control <<CONTROL
Package: ws-hello
Version: $1
Architecture: all
Maintainer: Harness <harness@example.com>
Section: misc
Priority: optional
Description: Waystation harness package
 A trivial package built by the Waystation client harness.
CONTROL
  echo "hello $1" > /tmp/wsdeb/usr/share/ws-hello/hello.txt
  dpkg-deb --root-owner-group --build /tmp/wsdeb "/tmp/ws-hello_$1_all.deb" >/dev/null || fail "dpkg-deb $1"
}

published() {  # the sha256 of the published InRelease, or nothing
  [ "$(send GET "$base/dists/stable/InRelease")" = 200 ] && sha256sum /tmp/ws-send.out | cut -d' ' -f1 || true
}

wait_published() {  # wait_published <previous InRelease sha256>: until the worker published a different one
  i=0
  while [ "$(published)" = "$1" ]; do
    i=$((i + 1)); [ "$i" -lt 60 ] || fail 'the worker did not publish the distribution'; sleep 1
  done
}

upload() {  # upload <file>
  code=$(send PUT "$base/$(basename "$1")" "$1")
  [ "$code" = 201 ] || fail "upload of $1 returned $code: $(cat /tmp/ws-send.out)"
}

step 'dpkg-deb builds ws-hello 1.0; upload it (PUT, as curl --upload-file)'
build 1.0
before=$(published)
upload /tmp/ws-hello_1.0_all.deb
grep -q '"path":"pool/main/w/ws-hello/ws-hello_1.0_all.deb"' /tmp/ws-send.out || fail "unexpected answer: $(cat /tmp/ws-send.out)"
wait_published "$before"

step 'the repository serves its signing key (armored for Signed-By)'
[ "$(send GET "$base/public.asc")" = 200 ] || fail 'no public.asc served'
grep -q 'BEGIN PGP PUBLIC KEY BLOCK' /tmp/ws-send.out || fail 'public.asc is not a public key'
mkdir -p /etc/apt/keyrings
cp /tmp/ws-send.out /etc/apt/keyrings/ws-hosted.asc

step 'apt-get update verifies InRelease with that key (deb822 Signed-By, API token in auth.conf), then install'
rm -f /etc/apt/sources.list.d/* /etc/apt/auth.conf.d/*
cat > /etc/apt/sources.list.d/ws-hosted.sources <<SOURCES
Types: deb
URIs: $base/
Suites: stable
Components: main
Signed-By: /etc/apt/keyrings/ws-hosted.asc
SOURCES
mkdir -p /etc/apt/auth.conf.d
printf 'machine %s/\nlogin ci-bot\npassword %s\n' "$base" "$token" > /etc/apt/auth.conf.d/ws-hosted.conf
chmod 600 /etc/apt/auth.conf.d/ws-hosted.conf
hosted_update() {
  rm -rf /var/lib/apt/lists/*
  apt-get update --error-on=any > /tmp/hosted.log 2>&1 || { cat /tmp/hosted.log; fail 'apt-get update (hosted)'; }
  grep -q "$base.* stable InRelease" /tmp/hosted.log || { cat /tmp/hosted.log; fail 'no InRelease from the hosted repository'; }
  if grep -qi 'NO_PUBKEY\|not signed\|BADSIG\|^W:' /tmp/hosted.log; then cat /tmp/hosted.log; fail 'signature problem'; fi
}
hosted_update
apt-get install ws-hello || fail 'apt-get install ws-hello'
[ "$(cat /usr/share/ws-hello/hello.txt)" = 'hello 1.0' ] || fail 'ws-hello 1.0 not installed'

step 'with another key in Signed-By (the Debian archive keyring) apt-get update refuses the InRelease'
mv /etc/apt/sources.list.d/ws-hosted.sources /tmp/ws-hosted.sources.good
sed 's|^Signed-By: .*|Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp|' /tmp/ws-hosted.sources.good \
  > /etc/apt/sources.list.d/ws-hosted.sources
rm -rf /var/lib/apt/lists/*
if apt-get update --error-on=any > /tmp/badkey.log 2>&1; then cat /tmp/badkey.log; fail 'apt accepted InRelease with the wrong key'; fi
grep -qi 'signature\|NO_PUBKEY\|not signed\|key' /tmp/badkey.log || { cat /tmp/badkey.log; fail 'no signature error'; }
cp /tmp/ws-hosted.sources.good /etc/apt/sources.list.d/ws-hosted.sources

step 'a second version: upload, wait for the new index, apt-get upgrade'
build 1.1
before=$(published)
upload /tmp/ws-hello_1.1_all.deb
wait_published "$before"
hosted_update
apt-get install --only-upgrade ws-hello || fail 'apt-get upgrade ws-hello'
[ "$(cat /usr/share/ws-hello/hello.txt)" = 'hello 1.1' ] || fail 'ws-hello 1.1 not installed'

step 'browse API lists both versions'
ws_api GET "/repositories/$repo/apt/packages?suite=stable&arch=amd64&q=ws-hello" > /tmp/hosted-packages.json \
  || fail 'no apt packages'
grep -q '"version":"1.0"' /tmp/hosted-packages.json && grep -q '"version":"1.1"' /tmp/hosted-packages.json \
  || fail "versions not listed: $(cat /tmp/hosted-packages.json)"

step 'delete 1.0; after the rebuild apt no longer offers it'
before=$(published)
code=$(send DELETE "$base/pool/main/w/ws-hello/ws-hello_1.0_all.deb")
[ "$code" = 204 ] || fail "delete returned $code"
wait_published "$before"
hosted_update
if apt-cache madison ws-hello | grep -q ' 1.0 '; then apt-cache madison ws-hello; fail 'ws-hello 1.0 still offered'; fi
apt-cache madison ws-hello | grep -q ' 1.1 ' || fail 'ws-hello 1.1 no longer offered'

step 'without credentials the hosted repository is not readable'
code=$(basic='' send GET "$base/dists/stable/InRelease")
case "$code" in 401 | 404) ;; *) fail "anonymous InRelease returned $code" ;; esac
apt-get remove -y ws-hello >/dev/null 2>&1 || true
rm -f /etc/apt/sources.list.d/ws-hosted.sources /etc/apt/auth.conf.d/ws-hosted.conf
