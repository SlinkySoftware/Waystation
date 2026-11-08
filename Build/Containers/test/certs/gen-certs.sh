#!/bin/sh
# Writes a throwaway CA (ca.crt) and one certificate per test directory server into /tls:
#   ldap1.crt / ldap1.key  (OpenLDAP, SAN ldap1.example.com)
#   dc1.crt / dc1.key      (Samba AD, SAN dc1.example.com)
# Keys are world-readable here (test-only); the servers copy them with the modes they need.
set -eu
out=/tls
work=$(mktemp -d)
cd "$work"
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj '/CN=Waystation Test CA' \
  -addext 'basicConstraints=critical,CA:TRUE' -addext 'keyUsage=critical,keyCertSign,cRLSign' \
  -keyout ca.key -out ca.crt 2>/dev/null
for name in ldap1 dc1; do
  host="$name.example.com"
  openssl req -newkey rsa:2048 -nodes -subj "/CN=$host" -keyout "$name.key" -out "$name.csr" 2>/dev/null
  printf 'subjectAltName=DNS:%s\nextendedKeyUsage=serverAuth\nkeyUsage=critical,digitalSignature,keyEncipherment\n' \
    "$host" > "$name.ext"
  openssl x509 -req -in "$name.csr" -CA ca.crt -CAkey ca.key -CAcreateserial -days 2 -extfile "$name.ext" \
    -out "$name.crt" 2>/dev/null
done
cp ca.crt ldap1.crt ldap1.key dc1.crt dc1.key "$out/"
chmod 0644 "$out"/*
echo "certificates written to $out"
