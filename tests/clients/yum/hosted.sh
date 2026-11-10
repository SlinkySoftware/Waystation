#!/bin/sh
# Hosted yum (T-P5-YUMH), run by test.sh in the rocky client: build an RPM with rpmbuild, upload it with curl, wait
# for the worker to publish signed repodata, then dnf (repo_gpgcheck=1 with the key the repository serves, an API
# token in the .repo) installs it, upgrades to a second upload, and no longer sees a deleted version.
set -eu
. /harness/lib.sh

repo=yum-hosted
base="$WAYSTATION_URL/repository/$repo"
ws_login
ws_token '["read","write","delete"]' "[\"$repo\"]"
token=$WS_TOKEN
auth="ci-bot:$token"

build() {  # build <version>: /tmp/rpmbuild/RPMS/noarch/ws-hello-<version>-1.noarch.rpm
  mkdir -p /tmp/rpmbuild/SPECS
  cat > /tmp/rpmbuild/SPECS/ws-hello.spec <<SPEC
Name: ws-hello
Version: $1
Release: 1
Summary: Waystation harness package
License: MIT
BuildArch: noarch

%description
A trivial package built by the Waystation client harness.

%install
mkdir -p %{buildroot}/usr/share/ws-hello
echo 'hello $1' > %{buildroot}/usr/share/ws-hello/hello.txt

%files
/usr/share/ws-hello/hello.txt

%changelog
* Tue Nov 10 2026 Harness <harness@example.com> - $1-1
- Build $1
SPEC
  rpmbuild -bb --quiet --define '_topdir /tmp/rpmbuild' /tmp/rpmbuild/SPECS/ws-hello.spec || fail "rpmbuild $1"
}

revision() {  # the published repomd.xml's revision, or nothing
  curl -fsS -u "$auth" "$base/repodata/repomd.xml" 2>/dev/null | sed -n 's:.*<revision>\(.*\)</revision>.*:\1:p'
}

wait_published() {  # wait_published <previous revision>: until the worker published a different repomd.xml
  i=0
  while [ "$(revision)" = "$1" ]; do
    i=$((i + 1)); [ "$i" -lt 60 ] || fail 'the worker did not publish repodata'; sleep 1
  done
}

upload() {  # upload <file>
  code=$(curl -sS -o /tmp/upload.json -w '%{http_code}' -u "$auth" --upload-file "$1" "$base/")
  [ "$code" = 201 ] || fail "upload of $1 returned $code: $(cat /tmp/upload.json)"
}

step 'rpmbuild ws-hello 1.0 and upload it with curl (PUT)'
build 1.0
before=$(revision)
upload /tmp/rpmbuild/RPMS/noarch/ws-hello-1.0-1.noarch.rpm
wait_published "$before"

step 'the repository serves its signing key; repomd.xml.asc verifies with it (rpm keyring via dnf below)'
curl -fsS -u "$auth" -o /tmp/ws-key.asc "$base/RPM-GPG-KEY" || fail 'no RPM-GPG-KEY served'
grep -q 'BEGIN PGP PUBLIC KEY BLOCK' /tmp/ws-key.asc || fail 'RPM-GPG-KEY is not a public key'
curl -fsS -u "$auth" -o /tmp/repomd.asc "$base/repodata/repomd.xml.asc" || fail 'no repomd.xml.asc'

step 'dnf install from the hosted repository (repo_gpgcheck=1, gpgkey served by Waystation, API token in the .repo)'
cat > /etc/yum.repos.d/ws-hosted.repo <<REPO
[ws-hosted]
name=Waystation hosted
baseurl=$base/
enabled=1
repo_gpgcheck=1
gpgcheck=0
gpgkey=$base/RPM-GPG-KEY
username=ci-bot
password=$token
REPO
chmod 600 /etc/yum.repos.d/ws-hosted.repo
dnf_hosted() { dnf -y --disablerepo='*' --enablerepo=ws-hosted --setopt=install_weak_deps=False "$@"; }
dnf_hosted makecache > /tmp/hosted.log 2>&1 || { cat /tmp/hosted.log; fail 'dnf makecache (hosted)'; }
grep -qi 'Importing GPG key\|imported' /tmp/hosted.log || { cat /tmp/hosted.log; fail 'dnf did not import the repository key'; }
dnf_hosted install ws-hello || fail 'dnf install ws-hello'
[ "$(cat /usr/share/ws-hello/hello.txt)" = 'hello 1.0' ] || fail 'ws-hello 1.0 not installed'

step 'a second version: upload, wait for the new repodata, dnf upgrade'
build 1.1
before=$(revision)
upload /tmp/rpmbuild/RPMS/noarch/ws-hello-1.1-1.noarch.rpm
wait_published "$before"
dnf_hosted --refresh upgrade ws-hello || fail 'dnf upgrade ws-hello'
[ "$(cat /usr/share/ws-hello/hello.txt)" = 'hello 1.1' ] || fail 'ws-hello 1.1 not installed'

step 'browse API lists both versions, newest first'
ws_api GET "/repositories/$repo/yum/versions?package=ws-hello" > /tmp/hosted-versions.json || fail 'no yum versions'
tr -d '\n' < /tmp/hosted-versions.json | grep -q '"evr":"1.1-1".*"evr":"1.0-1"' \
  || fail "versions not listed newest first: $(cat /tmp/hosted-versions.json)"

step 'delete 1.0 with curl; after the rebuild dnf no longer offers it'
before=$(revision)
code=$(curl -sS -o /dev/null -w '%{http_code}' -u "$auth" -X DELETE "$base/ws-hello-1.0-1.noarch.rpm")
[ "$code" = 204 ] || fail "delete returned $code"
wait_published "$before"
dnf_hosted --refresh --showduplicates list available ws-hello > /tmp/list.log 2>&1 || true
if grep -q '1.0-1' /tmp/list.log; then cat /tmp/list.log; fail 'ws-hello 1.0 still offered after delete'; fi

step 'without credentials the hosted repository is not readable'
code=$(curl -sS -o /dev/null -w '%{http_code}' "$base/repodata/repomd.xml")
case "$code" in 401 | 404) ;; *) fail "anonymous repomd.xml returned $code" ;; esac
rm -f /etc/yum.repos.d/ws-hosted.repo
