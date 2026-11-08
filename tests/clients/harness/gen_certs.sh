#!/bin/sh
# Generate a throwaway CA and one leaf certificate covering every impersonated host (HARNESS_HOSTS).
# Runs inside the fake-upstream container at start-up; output goes to the per-run directory (gitignored, deleted
# at teardown). The CA key is discarded after signing; only ca.crt is shared (clients and the Waystation server
# trust it for upstream TLS).
set -eu
dir=${HARNESS_CA_DIR:-/harness-ca}
[ -n "${HARNESS_HOSTS:-}" ] || { echo "gen_certs: HARNESS_HOSTS is empty" >&2; exit 1; }
mkdir -p "$dir"
cd "$dir"
san=""
for h in $HARNESS_HOSTS; do san="${san:+$san,}DNS:$h"; done
umask 077
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 2 \
  -subj "/CN=Waystation harness CA" -keyout ca.key -out ca.crt \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=harness-upstream" \
  -keyout leaf.key -out leaf.csr 2>/dev/null
printf 'subjectAltName=%s\nbasicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n' \
  "$san" > leaf.ext
openssl x509 -req -in leaf.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 2 -extfile leaf.ext \
  -out leaf.crt 2>/dev/null
rm -f leaf.csr leaf.ext ca.srl ca.key
chmod 644 ca.crt leaf.crt
echo "gen_certs: leaf certificate for: $HARNESS_HOSTS"
