#!/usr/bin/env bash
# Runs inside a privileged dev-image container on the `make test-nfs` network (T-F07): mounts the NFS-Ganesha export
# over NFSv4.1 (the kernel client, as a NAS-backed deployment would) and runs the blob store's tests on it as the
# calling user. The mount lives in this container's mount namespace only. Usage: test_nfs.sh <server> <uid> <gid> [pytest args]
set -euo pipefail

server=${1:?server host}
uid=${2:?uid}
gid=${3:?gid}
shift 3

# The kernel's in-tree mount takes an address, not a name (no mount.nfs helper in the dev image).
addr=$(getent hosts "$server" | awk '{print $1; exit}')
[ -n "$addr" ] || { echo "test_nfs: cannot resolve $server" >&2; exit 1; }
mnt=/mnt/nfs
mkdir -p "$mnt"
for _ in $(seq 1 30); do
  mount -t nfs4 -o "vers=4.1,addr=$addr,clientaddr=0.0.0.0,hard,timeo=50" "$addr:/export" "$mnt" 2>/dev/null && break
  sleep 1
done
grep -q " $mnt nfs4 " /proc/mounts || { echo "test_nfs: mounting $addr:/export failed" >&2; exit 1; }
grep " $mnt " /proc/mounts

dir="$mnt/run-$$"
mkdir "$dir"
chown "$uid:$gid" "$dir"
rc=0
setpriv --reuid "$uid" --regid "$gid" --clear-groups \
  env HOME=/tmp WAYSTATION_TEST_BLOB_DIR="$dir" \
  uv run --frozen pytest -m 'not integration and not nexus' -p no:cacheprovider --no-cov \
  Application/Common/tests/test_storage.py "$@" || rc=$?
rm -rf "$dir"
umount "$mnt"
exit "$rc"
