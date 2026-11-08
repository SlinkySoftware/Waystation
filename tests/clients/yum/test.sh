#!/bin/sh
# dnf through Waystation yum proxies (client-rocky: Rocky Linux 10, client-alma: AlmaLinux 10; HARNESS_CLIENT says
# which). dnf keeps verifying the distribution's own signatures: repomd.xml against repomd.xml.asc
# (repo_gpgcheck=1) and every package (gpgcheck=1), both with the key shipped in the image, so a modified repomd.xml
# or package would fail. Runs identically in every mode; assertions use only what survives fixture scrubbing.
set -eu
. /harness/lib.sh

case "$HARNESS_CLIENT" in
  rocky) repo=rocky name='Rocky Linux' key=/etc/pki/rpm-gpg/RPM-GPG-KEY-Rocky-10 package=rocky-release-core ;;
  alma) repo=almalinux name=AlmaLinux key=/etc/pki/rpm-gpg/RPM-GPG-KEY-AlmaLinux-10 package=almalinux-release-devel ;;
  *) fail "unknown client $HARNESS_CLIENT" ;;
esac
base="$WAYSTATION_URL/repository/$repo"
# Only the Waystation repository: the image's own repositories (mirrorlists) and any .repo file an installed release
# package drops are ignored.
dnf_ws() { dnf -y --disablerepo='*' --enablerepo=ws-extras --setopt=install_weak_deps=False "$@"; }

step "point dnf at Waystation ($repo: a .repo file with baseurl, \$releasever/\$basearch kept)"
mkdir -p /etc/yum.repos.d/stock && mv /etc/yum.repos.d/*.repo /etc/yum.repos.d/stock/ 2>/dev/null || true
cat > /etc/yum.repos.d/waystation.repo <<REPO
[ws-extras]
name=$name \$releasever - Extras (Waystation)
baseurl=$base/\$releasever/extras/\$basearch/os/
enabled=1
gpgcheck=1
repo_gpgcheck=1
gpgkey=file://$key
REPO

step 'dnf makecache (repomd.xml verified by dnf, repodata served by the repomd checksums)'
dnf_ws makecache > /tmp/makecache.log 2>&1 || { cat /tmp/makecache.log; fail 'dnf makecache'; }
cat /tmp/makecache.log
dnf_ws repoquery --location "$package" > /tmp/location 2>/dev/null || fail "$package is not offered by Waystation"
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

step 'reinstall with an empty local dnf cache (metadata and package served by the Waystation cache)'
dnf clean all >/dev/null
dnf_ws reinstall "$package" || fail "dnf reinstall $package"

step 'browse API: the generic tree lists the cached package'
ws_login
directory=${uri#"$base/"}
directory=${directory%/*}
ws_api GET "/repositories/$repo/browse?path=$directory" | grep -q "\"name\":\"${uri##*/}\"" \
  || fail "${uri##*/} is not listed in $directory by the browse API"

pass
