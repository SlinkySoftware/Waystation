# Waystation web tier: nginx (unprivileged) serving the SPA under /ui/, proxying dynamic paths to the server and
# serving cached blobs via X-Accel-Redirect. Build context: repository root.
ARG NODE_IMAGE=node:26-trixie-slim
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:1.30-alpine

# ---- SPA build (Quasar/Vite; output Application/Web/dist/spa, public path /ui/). Until the SPA exists the image
# ships a placeholder page.
# The SPA is architecture-independent: build it once on the build platform, even for multi-arch images.
FROM --platform=$BUILDPLATFORM ${NODE_IMAGE} AS spa
WORKDIR /web
COPY Application/Web/ ./
COPY Build/Containers/web/placeholder/ /placeholder/
RUN if [ -f package.json ]; then \
      npm ci --no-audit --no-fund && npm run build && mv dist/spa /out; \
    else \
      mkdir -p /out && cp /placeholder/index.html /out/index.html; \
    fi

FROM ${NGINX_IMAGE}
USER root
RUN rm -f /etc/nginx/conf.d/default.conf \
 && printf '#!/bin/sh\nmkdir -p /tmp/nginx-conf\n' > /docker-entrypoint.d/05-waystation-conf-dir.sh \
 && chmod 0755 /docker-entrypoint.d/05-waystation-conf-dir.sh
COPY Build/Containers/web/nginx.conf /etc/nginx/nginx.conf
COPY Build/Containers/web/nginx.conf.template /etc/nginx/templates/waystation.conf.template
COPY --from=spa /out/ /usr/share/nginx/html/ui/
USER 101
ENV NGINX_ENVSUBST_OUTPUT_DIR=/tmp/nginx-conf \
    NGINX_ENVSUBST_FILTER=^WAYSTATION_ \
    NGINX_ENTRYPOINT_QUIET_LOGS=1 \
    WAYSTATION_SERVER_ADDR=waystation-server:8000 \
    WAYSTATION_DATA_DIR=/var/lib/waystation \
    WAYSTATION_WEB_PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -q -O /dev/null "http://127.0.0.1:${WAYSTATION_WEB_PORT}/ui/" || exit 1
