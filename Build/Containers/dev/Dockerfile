# Waystation development image: Python 3.14 + uv + Node LTS.
# Used by every `make` target; nothing runs on the host. Source is bind-mounted at /src.
ARG PYTHON_IMAGE=python:3.14-slim-trixie
ARG NODE_IMAGE=node:26-trixie-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.7

FROM ${UV_IMAGE} AS uv
FROM ${NODE_IMAGE} AS node

FROM ${PYTHON_IMAGE}
RUN apt-get update \
 && apt-get install -y --no-install-recommends git make ca-certificates curl libatomic1 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /uvx /usr/local/bin/
COPY --from=node /usr/local/bin/node /usr/local/bin/
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
ENV UV_PROJECT_ENVIRONMENT=/src/.venv \
    UV_CACHE_DIR=/src/.cache/uv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    npm_config_cache=/src/.cache/npm \
    HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
