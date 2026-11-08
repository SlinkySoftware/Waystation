#!/bin/sh
# Install the generated certificate (from the ldap-certs service) and run slapd in the foreground.
set -eu
mkdir -p /run/slapd/tls
cp /tls/ca.crt /run/slapd/tls/ca.crt
cp /tls/ldap1.crt /run/slapd/tls/server.crt
cp /tls/ldap1.key /run/slapd/tls/server.key
chown -R openldap:openldap /run/slapd
chmod 0600 /run/slapd/tls/server.key
sed -i 's/^#TLS //' /etc/ldap/slapd.conf
exec slapd -u openldap -g openldap -f /etc/ldap/slapd.conf -h 'ldap:/// ldaps:///' -d 0
