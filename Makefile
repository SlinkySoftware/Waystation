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
PY_SRC      := Application/Common Application/Server Application/Worker tests/clients/harness Build/ci
# Modules held to 100% branch coverage (TASKS T-F04 acceptance).
FULL_COVERAGE := --include="*/waystation_common/paths.py,*/waystation_common/crypto.py"

define not_yet
	@echo "make $@: not yet implemented ($(1))"; exit 2
endef

##@ General
.PHONY: help
help: ## List targets
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z0-9_-]+:.*##/ {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2} /^##@/ {printf "\n\033[1m%s\033[0m\n", substr($$0, 5)}' $(MAKEFILE_LIST)

# CI builds the dev image once with a layer cache and sets SKIP_DEV_IMAGE_BUILD=1 (.github/workflows/ci.yml).
.PHONY: dev-image
dev-image: ## Build the dev image (Python 3.14 + uv + Node LTS)
	@if [ -n "$(SKIP_DEV_IMAGE_BUILD)" ] && docker image inspect $(DEV_IMAGE) >/dev/null 2>&1; then exit 0; fi; \
	  docker build -q -t $(DEV_IMAGE) -f Build/Containers/dev/Dockerfile Build/Containers/dev >/dev/null

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
	$(DEV_RUN) uv run --frozen mypy Application/Common/waystation_common Application/Server/waystation_server Application/Worker/waystation_worker tests/clients/harness Build/ci

.PHONY: test
test: sync ## Unit tests (pytest) with coverage; paths and crypto must stay at 100% branch coverage
	$(DEV_RUN) uv run --frozen pytest -m "not integration" --cov --cov-report=term-missing:skip-covered $(ARGS)
	$(DEV_RUN) uv run --frozen coverage report --fail-under=100 $(FULL_COVERAGE)

# One throwaway MongoDB/Valkey project per checkout, so parallel worktrees don't collide.
TEST_PROJECT := waystation-test-$(shell printf '%s' '$(CURDIR)' | md5sum | cut -c1-8)
COMPOSE_TEST  = docker compose -p $(TEST_PROJECT) -f Build/Containers/compose.test.yaml
.PHONY: test-integration
test-integration: DEV_ENV = --network $(TEST_PROJECT)_default \
  -e WAYSTATION_TEST_MONGO_URI='mongodb://waystation-mongodb:27017/?replicaSet=rs0' \
  -e WAYSTATION_TEST_VALKEY_URL='redis://waystation-valkey:6379/0'
test-integration: sync ## Integration tests (pytest -m integration) against real MongoDB/Valkey containers
	$(COMPOSE_TEST) up -d --wait --quiet-pull || { $(COMPOSE_TEST) logs --no-color --tail 80; \
	  $(COMPOSE_TEST) down -v --remove-orphans >/dev/null 2>&1; exit 1; }
	$(DEV_RUN) uv run --frozen pytest -m integration -p no:cacheprovider $(ARGS); rc=$$?; \
	  $(COMPOSE_TEST) down -v --remove-orphans >/dev/null 2>&1; exit $$rc

.PHONY: check
check: forbidden-strings workflow-lint fmt-check lint typecheck test test-integration clients-check frontend-check budgets ## Everything CI runs on a PR (grows as tasks land)

.PHONY: forbidden-strings budgets workflow-lint
ACTIONLINT_IMAGE ?= rhysd/actionlint:1.7.12@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667
workflow-lint: ## actionlint + shellcheck on the GitHub and Gitea workflows
	docker run --rm --user $(UID):$(GID) -v $(CURDIR):/repo:ro -w /repo $(ACTIONLINT_IMAGE) -color \
	  $(wildcard .github/workflows/*.yml .gitea/workflows/*.yml)

forbidden-strings: dev-image ## Fail on non-allowlisted hosts/IPs, keys and tokens anywhere in the repo (Build/ci/allowed_hosts.txt)
	Build/ci/forbidden_strings.sh

budgets: dev-image ## Report performance budgets (Build/ci/budgets.json); only 'fail' budgets fail, the rest warn until M2
	@$(DEV_RUN) python Build/ci/budgets.py $(if $(MARKDOWN),--markdown)

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
WEB_DIR     := Application/Web
WEB_PORT    ?= 9000
WEB_RUN      = $(DEV_RUN) sh -c
# Dev servers need a TTY-less long-running container with the port published (bound to 0.0.0.0 inside).
WEB_SERVE    = docker run --rm -i $(TTY) --init --user $(UID):$(GID) -v $(CURDIR):/src -w /src/$(WEB_DIR) \
               -p $(WEB_PORT):9000 $(DEV_ENV) $(DEV_IMAGE)

.PHONY: frontend-install frontend-dev frontend-mock frontend-lint frontend-test frontend-build frontend-build-mock gen-api gen-api-check brand brand-check
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

brand: frontend-install ## Regenerate the brand masters and favicon set from src/assets/brand/geometry.ts (UI_DESIGN §8)
	$(WEB_RUN) 'cd $(WEB_DIR) && npm run brand'

brand-check: frontend-install ## Fail if the committed brand SVGs or web manifest are stale (CI)
	$(WEB_RUN) 'cd $(WEB_DIR) && npm run brand:check'

.PHONY: frontend-check
frontend-check: gen-api-check brand-check frontend-lint frontend-test frontend-build ## Everything CI runs for the SPA

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
.PHONY: e2e clients-test clients-check perf
e2e: ## Playwright E2E
	$(call not_yet,T-U09)
FORMAT ?= all
MODE   ?= replay
clients-test: dev-image ## Real-client format tests: FORMAT=<name|all> MODE=replay|record|live (tests/clients/README.md)
	tests/clients/run.sh $(FORMAT) $(MODE)
clients-check: dev-image ## Forbidden-strings + size check on upstream fixtures and the client harness
	tests/clients/check_fixtures.sh
.PHONY: proxies-check
proxies-check: ## Reverse-proxy examples in containers (REVERSE_PROXIES.md; SIZE_MB=1024, SLOW=1 for 60 s timeouts)
	Build/Containers/examples/reverse-proxies/check/check.sh
perf: ## k6 performance budgets
	$(call not_yet,T-P2-PERF)
