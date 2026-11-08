#!/bin/sh
# Install the generated certificate (from the ldap-certs service) and run the DC in the foreground.
set -eu
mkdir -p /etc/samba/tls
cp /tls/ca.crt /etc/samba/tls/ca.pem
cp /tls/dc1.crt /etc/samba/tls/cert.pem
cp /tls/dc1.key /etc/samba/tls/key.pem
chmod 0600 /etc/samba/tls/key.pem
exec samba --foreground --no-process-group --debug-stdout -d 1
