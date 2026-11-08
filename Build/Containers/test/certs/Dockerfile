# Throwaway CA and LDAP server certificates for `make test-integration` (one-shot service `ldap-certs`).
# Generated fresh on every run into the `ldap-tls` volume; nothing here is a real secret.
FROM debian:13-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssl \
 && rm -rf /var/lib/apt/lists/*
COPY gen-certs.sh /usr/local/bin/gen-certs.sh
ENTRYPOINT ["/usr/local/bin/gen-certs.sh"]
