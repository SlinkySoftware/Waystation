# Waystation: every target runs inside the dev image (Docker only; no host Python or Node).
# `make help` lists targets. Targets whose tooling arrives in a later task say so and fail.

SHELL := /bin/bash
.DEFAULT_GOAL := help

DEV_IMAGE   ?= waystation-dev:local
UID         := $(shell id -u)
GID         := $(shell id -g)
VERSION     := $(shell cat VERSION)
TTY         := $(shell [ -t 0 ] && echo -t)
DEV_RUN      = docker run --rm -i $(TTY) --user $(UID):$(GID) -v $(CURDIR):/src -w /src $(DEV_ENV) $(DEV_IMAGE)
PY_SRC      := Application/Common Application/Server Application/Worker
# Modules held to 100% branch coverage (TASKS T-F04 acceptance).
FULL_COVERAGE := --include="*/waystation_common/paths.py,*/waystation_common/crypto.py"

define not_yet
	@echo "make $@: not yet implemented ($(1))"; exit 2
endef

##@ General
.PHONY: help
help: ## List targets
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z0-9_-]+:.*##/ {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2} /^##@/ {printf "\n\033[1m%s\033[0m\n", substr($$0, 5)}' $(MAKEFILE_LIST)

.PHONY: dev-image
dev-image: ## Build the dev image (Python 3.14 + uv + Node LTS)
	@docker build -q -t $(DEV_IMAGE) -f Build/Containers/dev/Dockerfile Build/Containers/dev >/dev/null

.PHONY: shell
shell: dev-image ## Open a shell in the dev image
	$(DEV_RUN) bash

.PHONY: hooks
hooks: ## Install the git pre-commit hook (runs make fmt-check lint)
	git config core.hooksPath Build/githooks

##@ Python
.venv/.synced: pyproject.toml uv.lock $(wildcard Application/*/pyproject.toml) | dev-image
	$(DEV_RUN) uv sync --frozen --all-packages
	@touch $@

.PHONY: sync
sync: .venv/.synced ## Install the workspace into .venv (inside the dev image)

.PHONY: lock
lock: dev-image ## Update uv.lock (and package-lock.json once the SPA exists)
	$(DEV_RUN) uv lock
	@if [ -f Application/Web/package.json ]; then $(DEV_RUN) sh -c 'cd Application/Web && npm install --package-lock-only --no-audit --no-fund'; fi

.PHONY: fmt
fmt: sync ## Format Python code (ruff)
	$(DEV_RUN) uv run --frozen ruff format $(PY_SRC)
	$(DEV_RUN) uv run --frozen ruff check --fix $(PY_SRC)

.PHONY: fmt-check
fmt-check: sync ## Fail if formatting is needed
	$(DEV_RUN) uv run --frozen ruff format --check $(PY_SRC)

.PHONY: lint
lint: sync ## ruff + import-linter contracts (ARCHITECTURE §3)
	$(DEV_RUN) uv run --frozen ruff check $(PY_SRC)
	$(DEV_RUN) uv run --frozen lint-imports

.PHONY: typecheck
typecheck: sync ## mypy --strict
	$(DEV_RUN) uv run --frozen mypy Application/Common/waystation_common Application/Server/waystation_server Application/Worker/waystation_worker

.PHONY: test
test: sync ## Unit tests (pytest) with coverage; paths and crypto must stay at 100% branch coverage
	$(DEV_RUN) uv run --frozen pytest -m "not integration" --cov --cov-report=term-missing:skip-covered $(ARGS)
	$(DEV_RUN) uv run --frozen coverage report --fail-under=100 $(FULL_COVERAGE)

.PHONY: test-integration
test-integration: ## Integration tests against MongoDB/Valkey containers
	$(call not_yet,T-F06)

.PHONY: check
check: fmt-check lint typecheck test ## Everything CI runs on a PR (grows as tasks land)

.PHONY: env-example
env-example: sync ## Regenerate .env.example from waystation_common.settings (idempotent)
	$(DEV_RUN) sh -c "uv run --frozen python -m waystation_common.settings env-example > .env.example.tmp && mv .env.example.tmp .env.example"

.PHONY: set-version
set-version: ## Set the version everywhere: make set-version V=X.Y.Z
	@[ -n "$(V)" ] || { echo "usage: make set-version V=X.Y.Z"; exit 1; }
	@echo "$(V)" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$$' || { echo "bad version: $(V)"; exit 1; }
	echo "$(V)" > VERSION
	sed -i -E 's/^version = "[^"]+"/version = "$(V)"/' pyproject.toml Application/*/pyproject.toml
	@if [ -f Application/Web/package.json ]; then $(DEV_RUN) sh -c 'cd Application/Web && npm version --no-git-tag-version --allow-same-version $(V)'; fi
	$(DEV_RUN) uv lock

##@ Frontend
.PHONY: frontend-install frontend-dev frontend-mock frontend-lint frontend-test frontend-build gen-api
frontend-install: ## npm ci for the SPA
	$(call not_yet,T-V01)
frontend-dev: ## SPA dev server against the real server
	$(call not_yet,T-F10)
frontend-mock: ## SPA dev server on MSW mocks (npm run dev:mock)
	$(call not_yet,T-V01)
frontend-lint: ## ESLint + vue-tsc
	$(call not_yet,T-V01)
frontend-test: ## Vitest
	$(call not_yet,T-V01)
frontend-build: ## Production SPA build + bundle budget
	$(call not_yet,T-F10)
gen-api: ## Regenerate the TS API client from the OpenAPI schema
	$(call not_yet,T-V01)

##@ Stack
COMPOSE_DIR    := Build/Containers
DEV_ENV_FILE   := .dev-data/dev.env
IMAGE_PREFIX   ?= waystation
IMAGE_TAG      ?= $(VERSION)
DEV_HTTP_PORT  ?= 8080
COMPOSE_DEV     = WAYSTATION_IMAGE_PREFIX=$(IMAGE_PREFIX) WAYSTATION_IMAGE_TAG=dev \
                  docker compose -p waystation-dev --env-file $(DEV_ENV_FILE) \
                  -f $(COMPOSE_DIR)/compose.yaml -f $(COMPOSE_DIR)/compose.dev.yaml \
                  $(if $(wildcard Application/Web/dist/spa/index.html),-f $(COMPOSE_DIR)/compose.dev-spa.yaml)

# Throwaway dev secrets, generated once, gitignored (.dev-data/). Never reuse them anywhere else.
$(DEV_ENV_FILE):
	@mkdir -p $(dir $@)
	@umask 077; { \
	  echo '# Dev stack env (generated by make; throwaway values).'; \
	  echo 'WAYSTATION_HTTP_PORT=$(DEV_HTTP_PORT)'; \
	  echo 'WAYSTATION_BASE_URL=http://localhost:$(DEV_HTTP_PORT)'; \
	  echo 'WAYSTATION_LOG_FORMAT=console'; \
	  echo 'WAYSTATION_LOG_LEVEL=DEBUG'; \
	  echo "WAYSTATION_SECRET_KEYS=$$(head -c 32 /dev/urandom | base64)"; \
	  echo "WAYSTATION_SESSION_SECRET=$$(head -c 32 /dev/urandom | base64)"; \
	} > $@
	@echo "created $@"

.PHONY: dev-up dev-down dev-clean dev-logs dev-ps images backup
dev-up: $(DEV_ENV_FILE) ## Start the dev stack (build, hot reload, wait until healthy); UI on http://localhost:8080/
	$(COMPOSE_DEV) up -d --build --wait --wait-timeout 300
	@$(COMPOSE_DEV) ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'
dev-down: ## Stop the dev stack (keeps data volumes)
	@[ -f $(DEV_ENV_FILE) ] || exit 0; $(COMPOSE_DEV) down
dev-clean: ## Stop the dev stack and delete its volumes (MongoDB, Valkey, blobs)
	@[ -f $(DEV_ENV_FILE) ] || exit 0; $(COMPOSE_DEV) down -v
dev-logs: ## Follow dev stack logs (S=<service> for one)
	$(COMPOSE_DEV) logs -f $(S)
dev-ps: ## Show dev stack containers
	$(COMPOSE_DEV) ps
images: ## Build server, worker and web images (IMAGE_PREFIX/waystation-<role>:IMAGE_TAG, tag defaults to VERSION)
	docker build -f $(COMPOSE_DIR)/server/Dockerfile -t $(IMAGE_PREFIX)/waystation-server:$(IMAGE_TAG) .
	docker build -f $(COMPOSE_DIR)/worker/Dockerfile -t $(IMAGE_PREFIX)/waystation-worker:$(IMAGE_TAG) .
	docker build -f $(COMPOSE_DIR)/web/Dockerfile -t $(IMAGE_PREFIX)/waystation-web:$(IMAGE_TAG) .
backup: ## mongodump + blob copy
	$(call not_yet,T-O07)

##@ Test suites
.PHONY: e2e clients-test perf
e2e: ## Playwright E2E
	$(call not_yet,T-U09)
clients-test: ## Real-client format tests
	$(call not_yet,T-P2-HARNESS)
perf: ## k6 performance budgets
	$(call not_yet,T-P2-PERF)
