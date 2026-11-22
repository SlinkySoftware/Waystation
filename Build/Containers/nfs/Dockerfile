# Userspace NFSv4 server (NFS-Ganesha, VFS backend) for `make test-nfs` (T-F07): the blob store's tests run on a
# directory mounted from it. Userspace, so the test never starts or reconfigures the host kernel's nfsd.
FROM debian:trixie-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends nfs-ganesha nfs-ganesha-vfs \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /export /var/run/ganesha
COPY ganesha.conf /etc/ganesha/ganesha.conf
EXPOSE 2049
CMD ["/usr/bin/ganesha.nfsd", "-F", "-L", "/dev/stderr", "-f", "/etc/ganesha/ganesha.conf"]
