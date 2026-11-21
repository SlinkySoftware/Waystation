#!/bin/sh
# client-rocky: dnf with basic auth in the .repo file (username + API token) against a private hosted yum repository.
# The package is built here with rpmbuild and uploaded with curl: no upstream.
set -eu
. /harness/lib.sh
. /harness/auth/common.sh

base="$WAYSTATION_URL/repository/$YUM"

step "rpmbuild a package and upload it with LDAP user $LDAP_USER's API token (curl basic auth)"
ldap_token '["read","write"]'
ldap_tok=$WS_TOKEN
mkdir -p /tmp/rpmbuild/SPECS
cat > /tmp/rpmbuild/SPECS/ws-auth.spec <<'SPEC'
Name: ws-auth
Version: 1.0
Release: 1
Summary: Waystation harness package (auth matrix)
License: MIT
BuildArch: noarch

%description
A trivial package built by the Waystation client harness.

%install
mkdir -p %{buildroot}/usr/share/ws-auth
echo 'auth matrix' > %{buildroot}/usr/share/ws-auth/hello.txt

%files
/usr/share/ws-auth/hello.txt
SPEC
rpmbuild -bb --quiet --define '_topdir /tmp/rpmbuild' /tmp/rpmbuild/SPECS/ws-auth.spec || fail 'rpmbuild'
code=$(curl -sS -o /tmp/upload.json -w '%{http_code}' -u "$LDAP_USER:$ldap_tok" \
  --upload-file /tmp/rpmbuild/RPMS/noarch/ws-auth-1.0-1.noarch.rpm "$base/")
[ "$code" = 201 ] || fail "upload returned $code: $(cat /tmp/upload.json)"
i=0
until curl -fsS -u "$LDAP_USER:$ldap_tok" "$base/repodata/repomd.xml" 2>/dev/null | grep -q '<revision>'; do
  i=$((i + 1)); [ "$i" -lt 60 ] || fail 'the worker did not publish repodata'; sleep 1
done

repo_file() {  # repo_file <username> <password>: the .repo with basic auth (no credentials when both are empty)
  {
    printf '[ws-auth]\nname=Waystation private\nbaseurl=%s/\nenabled=1\ngpgcheck=0\nrepo_gpgcheck=0\n' "$base"
    [ -z "$1" ] || printf 'username=%s\npassword=%s\n' "$1" "$2"
  } > /etc/yum.repos.d/ws-auth.repo
  chmod 600 /etc/yum.repos.d/ws-auth.repo
}
dnf_private() { dnf -y -q --disablerepo='*' --enablerepo=ws-auth --setopt=install_weak_deps=False "$@"; }
makecache_refused() {  # makecache_refused <what>
  if dnf -y -v --disablerepo='*' --enablerepo=ws-auth --refresh makecache > /tmp/dnf.log 2>&1; then
    cat /tmp/dnf.log; fail "$1: not refused"
  fi
  grep -Eqi '401|unauthori' /tmp/dnf.log || { cat /tmp/dnf.log; fail "$1: no 401"; }
}

step 'anonymous dnf is refused (401: anonymous access is off)'
repo_file '' ''
makecache_refused 'anonymous'

step "dnf with LDAP user $LDAP_USER's API token in the .repo: install"
repo_file "$LDAP_USER" "$ldap_tok"
dnf_private --refresh install ws-auth || fail 'dnf install (LDAP token)'
[ "$(cat /usr/share/ws-auth/hello.txt)" = 'auth matrix' ] || fail 'ws-auth not installed'

step "dnf with OIDC user $OIDC_USER's API token in the .repo: reinstall"
repo_file ci-bot "$(shared_get OIDC_TOKEN_YUM)"
dnf clean all > /dev/null
dnf_private --refresh reinstall ws-auth || fail 'dnf reinstall (OIDC token)'

step 'the LDAP password itself and a wrong password are refused (401)'
repo_file "$LDAP_USER" "$LDAP_PASSWORD"
makecache_refused 'LDAP password'
repo_file "$LDAP_USER" "$NOT_A_TOKEN"
makecache_refused 'wrong password'

step 'the OIDC token is revoked by an administrator: refused (401)'
revoke_as_admin "$(shared_get OIDC_TOKEN_ID_YUM)"
repo_file ci-bot "$(shared_get OIDC_TOKEN_YUM)"
makecache_refused 'revoked token'
rm -f /etc/yum.repos.d/ws-auth.repo

pass
