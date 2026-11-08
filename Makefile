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
test: sync ## Unit tests (pytest) with coverage
	$(DEV_RUN) uv run --frozen pytest --cov --cov-report=term-missing:skip-covered $(ARGS)

.PHONY: test-integration
test-integration: ## Integration tests against MongoDB/Valkey containers
	$(call not_yet,T-F06)

.PHONY: check
check: fmt-check lint typecheck test frontend-check ## Everything CI runs on a PR (grows as tasks land)

.PHONY: env-example
env-example: ## Regenerate .env.example from settings
	$(call not_yet,T-F04)

.PHONY: set-version
set-version: ## Set the version everywhere: make set-version V=X.Y.Z
	@[ -n "$(V)" ] || { echo "usage: make set-version V=X.Y.Z"; exit 1; }
	@echo "$(V)" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$$' || { echo "bad version: $(V)"; exit 1; }
	echo "$(V)" > VERSION
	sed -i -E 's/^version = "[^"]+"/version = "$(V)"/' pyproject.toml Application/*/pyproject.toml
	@if [ -f Application/Web/package.json ]; then $(DEV_RUN) sh -c 'cd Application/Web && npm version --no-git-tag-version --allow-same-version $(V)'; fi
	$(DEV_RUN) uv lock

##@ Frontend
WEB_DIR     := Application/Web
WEB_PORT    ?= 9000
WEB_RUN      = $(DEV_RUN) sh -c
# Dev servers need a TTY-less long-running container with the port published (bound to 0.0.0.0 inside).
WEB_SERVE    = docker run --rm -i $(TTY) --init --user $(UID):$(GID) -v $(CURDIR):/src -w /src/$(WEB_DIR) \
               -p $(WEB_PORT):9000 $(DEV_ENV) $(DEV_IMAGE)

.PHONY: frontend-install frontend-dev frontend-mock frontend-lint frontend-test frontend-build frontend-build-mock gen-api gen-api-check
$(WEB_DIR)/node_modules/.installed: $(WEB_DIR)/package.json $(WEB_DIR)/package-lock.json | dev-image
	$(WEB_RUN) 'cd $(WEB_DIR) && npm ci --no-audit --no-fund'
	@touch $@

frontend-install: $(WEB_DIR)/node_modules/.installed ## npm ci for the SPA (inside the dev image)

frontend-dev: frontend-install ## SPA dev server against the real server (WAYSTATION_DEV_API_TARGET, port WEB_PORT)
	$(WEB_SERVE) npm run dev

frontend-mock: frontend-install ## SPA dev server on MSW mocks + live generator: http://localhost:$(WEB_PORT)/ui/
	$(WEB_SERVE) npm run dev:mock

frontend-lint: frontend-install ## ESLint + vue-tsc
	$(WEB_RUN) 'cd $(WEB_DIR) && npm run lint'

frontend-test: frontend-install ## Vitest
	$(WEB_RUN) 'cd $(WEB_DIR) && npm test'

frontend-build: frontend-install ## Production SPA build (dist/spa, served under /ui/) + bundle budget
	$(WEB_RUN) 'cd $(WEB_DIR) && npm run build'

frontend-build-mock: frontend-install ## SPA build that runs on mocks (static demo / screenshots)
	$(WEB_RUN) 'cd $(WEB_DIR) && npm run build:mock'

gen-api: frontend-install ## Regenerate src/api/schema.d.ts from openapi/waystation-m0.yaml
	$(WEB_RUN) 'cd $(WEB_DIR) && npm run gen-api'

gen-api-check: frontend-install ## Fail if the generated API types are stale (CI)
	$(WEB_RUN) 'cd $(WEB_DIR) && npm run gen-api:check'

.PHONY: frontend-check
frontend-check: gen-api-check frontend-lint frontend-test frontend-build ## Everything CI runs for the SPA

##@ Stack
.PHONY: dev-up dev-down images backup
dev-up: ## Start the dev stack (hot reload)
	$(call not_yet,T-F05)
dev-down: ## Stop the dev stack
	$(call not_yet,T-F05)
images: ## Build server, worker and web images
	$(call not_yet,T-F05)
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
