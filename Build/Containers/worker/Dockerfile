# Waystation worker image. Build context: repository root.
ARG PYTHON_IMAGE=python:3.14-slim-trixie
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.7

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY Application/Common/pyproject.toml Application/Common/
COPY Application/Server/pyproject.toml Application/Server/
COPY Application/Worker/pyproject.toml Application/Worker/
RUN uv sync --frozen --no-dev --package waystation-worker --no-install-workspace
COPY Application/Common/waystation_common Application/Common/waystation_common
COPY Application/Worker/waystation_worker Application/Worker/waystation_worker
RUN uv sync --frozen --no-dev --package waystation-worker --no-editable

FROM ${PYTHON_IMAGE}
ARG WAYSTATION_UID=10001
ARG WAYSTATION_GID=10001
RUN groupadd --system --gid ${WAYSTATION_GID} waystation \
 && useradd --system --uid ${WAYSTATION_UID} --gid ${WAYSTATION_GID} --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin waystation \
 && install -d -o ${WAYSTATION_UID} -g ${WAYSTATION_GID} -m 2750 /var/lib/waystation
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER ${WAYSTATION_UID}:${WAYSTATION_GID}
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-m", "waystation_worker.health"]
CMD ["python", "-m", "waystation_worker"]
