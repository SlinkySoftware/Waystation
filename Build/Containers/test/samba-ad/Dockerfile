# Samba Active Directory DC for `make test-integration` (T-A01). Not for production.
# The domain, users and groups are provisioned at build time so the container starts in seconds.
FROM debian:13-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends samba samba-ad-dc samba-ad-provision winbind ldb-tools \
 && rm -rf /var/lib/apt/lists/* /etc/samba/smb.conf
COPY provision.sh /usr/local/bin/provision.sh
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN /usr/local/bin/provision.sh
EXPOSE 389 636
HEALTHCHECK --interval=2s --timeout=5s --retries=60 \
  CMD ldbsearch -H ldaps://127.0.0.1 --option='tls verify peer = no_check' --simple-bind-dn='CN=waystation-svc,CN=Users,DC=example,DC=com' \
      --password='Svc-Passw0rd1' -b 'DC=example,DC=com' -s base dn >/dev/null 2>&1 || exit 1
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
