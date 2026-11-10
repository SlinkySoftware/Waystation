#!/bin/sh
# dnf through Waystation yum proxies. HARNESS_CLIENT says which image runs:
# - rocky (Rocky Linux 10): the `extras` repository through `rocky` (repomd_keys: Waystation verifies repomd.xml.asc
#   with the distribution key before caching repomd.xml), then EPEL 10 `Everything` through `epel` (the EPEL key is
#   fetched through the proxy as gpgkey);
# - alma (AlmaLinux 10): `extras` through `almalinux`;
# - oracle (Oracle Linux 9): `addons` through `oracle` (packages under getPackage/, no repomd.xml.asc upstream);
# - fedora (dnf 5) and opensuse (zypper): smoke runs against the Rocky `extras` repository the rocky run cached.
# dnf keeps verifying the distribution's own signatures: repomd.xml against repomd.xml.asc (repo_gpgcheck=1, where
# the upstream signs it) and every package (gpgcheck=1), so a modified repomd.xml or package would fail. Runs
# identically in every mode; assertions use only what survives fixture scrubbing.
set -eu
. /harness/lib.sh

# Only the Waystation repositories: the image's own repositories (mirrorlists) and any .repo file an installed
# release package drops are moved aside / ignored.
mkdir -p /etc/yum.repos.d/stock && mv /etc/yum.repos.d/*.repo /etc/yum.repos.d/stock/ 2>/dev/null || true

# check <repo> <repo id> <title> <path below the repository> <gpgkey URL> <repo_gpgcheck> <package>
check() {
  repo=$1 id=$2 title=$3 path=$4 key=$5 repo_gpgcheck=$6 package=$7
  base="$WAYSTATION_URL/repository/$repo"
  dnf_ws() { dnf -y --disablerepo='*' --enablerepo="$id" --setopt=install_weak_deps=False "$@"; }

  step "point dnf at Waystation ($repo: a .repo file with baseurl, \$releasever/\$basearch kept)"
  cat > "/etc/yum.repos.d/$id.repo" <<REPO
[$id]
name=$title (Waystation)
baseurl=$base/$path
enabled=1
gpgcheck=1
repo_gpgcheck=$repo_gpgcheck
gpgkey=$key
REPO

  step "dnf makecache ($repo: repomd.xml, repodata served by the repomd checksums)"
  dnf_ws makecache > /tmp/makecache.log 2>&1 || { cat /tmp/makecache.log; fail "dnf makecache ($repo)"; }
  cat /tmp/makecache.log
  dnf_ws repoquery --location "$package" > /tmp/location 2>/dev/null || fail "$package is not offered by $repo"
  uri=$(tail -n 1 /tmp/location)
  case "$uri" in "$base/"*.rpm) ;; *) fail "unexpected package location: $uri" ;; esac

  step "dnf install $package (validated against the cached primary.xml)"
  dnf_ws install "$package" || fail "dnf install $package"
  rpm -q "$package" || fail "$package is not installed"

  step 'a cached package is a cache hit and keeps its distribution signature'
  code=$(curl -sS -o /tmp/pkg.rpm -D /tmp/pkg.headers -w '%{http_code}' "$uri")
  [ "$code" = 200 ] || fail "package $uri returned $code"
  grep -qi '^x-ws-cache: HIT' /tmp/pkg.headers || fail "package not a cache hit: $(cat /tmp/pkg.headers)"
  rpm -K /tmp/pkg.rpm | grep -q 'digests signatures OK' || fail "signature check failed: $(rpm -K /tmp/pkg.rpm)"

  step "reinstall with an empty local dnf cache ($repo metadata and package served by the Waystation cache)"
  dnf clean all >/dev/null
  dnf_ws reinstall "$package" || fail "dnf reinstall $package"

  step "browse API: the yum package view and the folder tree list $package"
  ws_login
  ws_api GET "/repositories/$repo/yum/versions?package=$package" > /tmp/versions.json || fail "no yum versions of $package"
  file=${uri#"$base/"}
  grep -q "\"path\":\"$file\"" /tmp/versions.json || fail "$file is not in the yum package view: $(cat /tmp/versions.json)"
  grep -q '"cached":true' /tmp/versions.json || fail "$package is not shown as cached"
  directory=${file%/*}
  ws_api GET "/repositories/$repo/browse?path=$directory" | grep -q "\"name\":\"${uri##*/}\"" \
    || fail "${uri##*/} is not listed in $directory by the browse API"
  rm -f "/etc/yum.repos.d/$id.repo"
}

case "$HARNESS_CLIENT" in
  rocky)
    check rocky ws-extras "Rocky Linux \$releasever - Extras" "\$releasever/extras/\$basearch/os/" \
      file:///etc/pki/rpm-gpg/RPM-GPG-KEY-Rocky-10 1 rocky-release-core
    step 'repomd.xml was verified by Waystation: its repomd.xml.asc is served with it'
    asc="$WAYSTATION_URL/repository/rocky/10/extras/x86_64/os/repodata/repomd.xml.asc"
    curl -sS -D /tmp/asc.headers -o /tmp/asc "$asc" && grep -q 'BEGIN PGP SIGNATURE' /tmp/asc \
      || fail "no repomd.xml.asc served for the verified repomd.xml"
    step 'API token in the .repo (username + token as password): makecache with it, refused once revoked'
    ws_token '["read"]' '["rocky"]'
    token_id=$WS_TOKEN_ID
    cat > /etc/yum.repos.d/ws-auth.repo <<REPO
[ws-auth]
name=Rocky Linux \$releasever - Extras (Waystation, token)
baseurl=$WAYSTATION_URL/repository/rocky/\$releasever/extras/\$basearch/os/
enabled=1
gpgcheck=1
repo_gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-Rocky-10
username=ci-bot
password=$WS_TOKEN
REPO
    chmod 600 /etc/yum.repos.d/ws-auth.repo
    dnf clean all >/dev/null
    dnf -y --disablerepo='*' --enablerepo=ws-auth makecache > /tmp/auth.log 2>&1 || { cat /tmp/auth.log; fail 'makecache with a token'; }
    ws_revoke "$token_id"
    dnf clean all >/dev/null
    if dnf -y --disablerepo='*' --enablerepo=ws-auth --setopt=skip_if_unavailable=False makecache > /tmp/revoked.log 2>&1; then
      cat /tmp/revoked.log; fail 'makecache worked with a revoked token'
    fi
    grep -q '401' /tmp/revoked.log || { cat /tmp/revoked.log; fail 'revoked token: no 401'; }
    rm -f /etc/yum.repos.d/ws-auth.repo
    check epel ws-epel "EPEL \$releasever - Everything" "\$releasever/Everything/\$basearch/" \
      "$WAYSTATION_URL/repository/epel/RPM-GPG-KEY-EPEL-10" 0 epel-release
    ;;
  alma)
    check almalinux ws-extras "AlmaLinux \$releasever - Extras" "\$releasever/extras/\$basearch/os/" \
      file:///etc/pki/rpm-gpg/RPM-GPG-KEY-AlmaLinux-10 1 almalinux-release-devel
    ;;
  oracle)
    check oracle ws-addons "Oracle Linux \$releasever Addons" "OL\$releasever/addons/\$basearch/" \
      file:///etc/pki/rpm-gpg/RPM-GPG-KEY-oracle 0 WALinuxAgent-udev
    ;;
  fedora)  # dnf 5 smoke (Fedora): the Rocky Linux 10 extras repository cached by the rocky run
    base="$WAYSTATION_URL/repository/rocky"
    step 'dnf 5: makecache and download through Waystation (repomd.xml and the package signature verified)'
    cat > /etc/yum.repos.d/ws-extras.repo <<REPO
[ws-extras]
name=Rocky Linux 10 - Extras (Waystation)
baseurl=$base/10/extras/x86_64/os/
enabled=1
gpgcheck=1
repo_gpgcheck=1
gpgkey=$base/RPM-GPG-KEY-Rocky-10
REPO
    dnf --version | head -n 1
    dnf -y --repo=ws-extras makecache || fail 'dnf5 makecache'
    (cd /tmp && dnf -y --repo=ws-extras download rocky-release-core) || fail 'dnf5 download rocky-release-core'
    curl -fsS -o /tmp/rocky.key "$base/RPM-GPG-KEY-Rocky-10" && rpm --import /tmp/rocky.key || fail 'key import'
    rpm -K /tmp/rocky-release-core-*.rpm | grep -q 'digests signatures OK' || fail 'dnf5: package signature'
    ;;
  opensuse)  # zypper smoke (openSUSE Leap): the same repository, repomd.xml.asc checked by zypper
    base="$WAYSTATION_URL/repository/rocky"
    step 'zypper: add the repository, refresh (repomd.xml.asc verified) and download a package'
    # Only the Waystation repository: the image's own repositories and services (internet) are moved aside.
    mkdir -p /etc/zypp/stock && mv /etc/zypp/repos.d /etc/zypp/services.d /etc/zypp/stock/ 2>/dev/null || true
    mkdir -p /etc/zypp/repos.d /etc/zypp/services.d
    curl -fsS -o /tmp/rocky.key "$base/RPM-GPG-KEY-Rocky-10" && rpm --import /tmp/rocky.key || fail 'key import'
    zypper --non-interactive addrepo --check --gpgcheck "$base/10/extras/x86_64/os/" ws-extras || fail 'zypper addrepo'
    zypper --non-interactive refresh ws-extras || fail 'zypper refresh'
    zypper --non-interactive download rocky-release-core || fail 'zypper download rocky-release-core'
    file=''
    for f in /var/cache/zypp/packages/ws-extras/*/rocky-release-core-*.rpm /var/cache/zypp/packages/ws-extras/*/*/rocky-release-core-*.rpm; do [ -f "$f" ] && file=$f; done
    [ -n "$file" ] || fail 'zypper did not keep the package'
    rpm -K "$file" | grep -q 'digests signatures OK' || fail "zypper: package signature: $(rpm -K "$file")"
    ;;
  *) fail "unknown client $HARNESS_CLIENT" ;;
esac

pass
