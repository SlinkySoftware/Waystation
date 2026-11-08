# Fake upstream (record/replay) and echo origin for the real-client harness (tests/clients/README.md).
# Stdlib Python plus openssl for the per-run throwaway CA. Test infrastructure only; never shipped.
ARG PYTHON_IMAGE=python:3.14-slim-trixie
FROM ${PYTHON_IMAGE}
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY upstream_store.py fake_upstream.py echo_origin.py gen_certs.sh /harness/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /harness
CMD ["sh", "-c", "sh /harness/gen_certs.sh && exec python /harness/fake_upstream.py"]
