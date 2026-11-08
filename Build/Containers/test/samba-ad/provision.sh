#!/bin/sh
# Build-time provisioning of the EXAMPLE.COM test domain (throwaway passwords, documentation names only).
set -eu
# NT ACLs go to a user.* xattr: security.* xattrs need CAP_SYS_ADMIN, which image builds do not have.
samba-tool domain provision \
  --realm=EXAMPLE.COM --domain=EXAMPLE --server-role=dc --dns-backend=NONE \
  --host-name=dc1 --adminpass='Admin-Passw0rd1' --use-rfc2307 \
  --option='acl_xattr:security_acl_name = user.NTACL' \
  --option='tls enabled = yes' --option='dns update command = /bin/true' \
  --option='tls certfile = /etc/samba/tls/cert.pem' \
  --option='tls keyfile = /etc/samba/tls/key.pem' \
  --option='tls cafile = /etc/samba/tls/ca.pem' >/dev/null
samba-tool domain passwordsettings set --complexity=off --history-length=0 --min-pwd-age=0 --max-pwd-age=0 >/dev/null

user() {  # name password given
  samba-tool user create "$1" "$2" --use-username-as-cn --given-name="$3" --surname=Example \
    --mail-address="$1@example.com" >/dev/null
  samba-tool user setexpiry "$1" --noexpiry >/dev/null
}
user alice Alice-Passw0rd1 Alice
user bob Bob-Passw0rd1 Bob
user carol Carol-Passw0rd1 Carol
user dave Dave-Passw0rd1 Dave
user erin Erin-Passw0rd1 Erin
samba-tool user disable carol >/dev/null
samba-tool user create waystation-svc Svc-Passw0rd1 >/dev/null
samba-tool user setexpiry waystation-svc --noexpiry >/dev/null

for g in developers ops platform waystation-users; do samba-tool group add "$g" >/dev/null; done
samba-tool group addmembers developers alice >/dev/null
samba-tool group addmembers ops bob >/dev/null
samba-tool group addmembers platform ops >/dev/null
samba-tool group addmembers waystation-users alice,bob,carol,erin >/dev/null
