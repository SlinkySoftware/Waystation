# OpenLDAP test directory with a FreeIPA-like tree for `make test-integration` (T-A01). Not for production.
# The tree is loaded at build time through a temporary slapd so the memberof overlay fills memberOf.
FROM debian:13-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends slapd ldap-utils \
 && rm -rf /var/lib/apt/lists/* /etc/ldap/slapd.d /var/lib/ldap/* \
 && mkdir -p /run/slapd /var/lib/ldap \
 && chown openldap:openldap /run/slapd /var/lib/ldap
COPY slapd.conf /etc/ldap/slapd.conf
COPY directory.ldif /tmp/directory.ldif
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
# Build-time load: a temporary root password exists only in this throwaway config.
RUN set -eu; \
    { cat /etc/ldap/slapd.conf; echo 'rootpw build-only-root'; } > /tmp/build.conf; \
    slapd -u openldap -g openldap -f /tmp/build.conf -h 'ldap://127.0.0.1:3890/'; \
    for _ in $(seq 50); do ldapsearch -x -H ldap://127.0.0.1:3890 -b '' -s base >/dev/null 2>&1 && break; sleep 0.2; done; \
    ldapadd -x -H ldap://127.0.0.1:3890 -D 'cn=Manager,dc=example,dc=com' -w build-only-root -f /tmp/directory.ldif >/dev/null; \
    printf 'dn: uid=carol,cn=users,cn=accounts,dc=example,dc=com\nchangetype: modify\nadd: pwdAccountLockedTime\npwdAccountLockedTime: 000001010000Z\n' \
      | ldapmodify -x -H ldap://127.0.0.1:3890 -D 'cn=Manager,dc=example,dc=com' -w build-only-root >/dev/null; \
    kill "$(cat /run/slapd/slapd.pid)"; \
    for _ in $(seq 50); do [ -f /run/slapd/slapd.pid ] || break; sleep 0.2; done; \
    rm -f /tmp/build.conf /tmp/directory.ldif
EXPOSE 389 636
HEALTHCHECK --interval=2s --timeout=3s --retries=30 \
  CMD ldapsearch -x -H ldap://127.0.0.1 -b '' -s base -LLL supportedExtension >/dev/null || exit 1
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
