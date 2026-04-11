# ── Vibe Stack Makefile ─────────────────────────────────────────
# Usage: make <target> [SVC=service-name]

COMPOSE := docker compose
COMPOSE_ALL := docker compose -f docker-compose.yml -f docker-compose.infra.yml -f docker-compose.gpu.yml
SVC ?=

.DEFAULT_GOAL := help

# ── Help ─────────────────────────────────────────────────────────
.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

# ── Lifecycle ────────────────────────────────────────────────────
.PHONY: up up-all down restart rebuild

up: ## Start core services (server, deerflow, vibe, tailscale)
	$(COMPOSE) up -d

up-all: ## Start all services (core + infra + gpu)
	$(COMPOSE_ALL) up -d

down: ## Stop all services
	$(COMPOSE_ALL) down

restart: ## Restart a service (SVC=name)
	@[ "$(SVC)" ] || (echo "Usage: make restart SVC=deerflow-langgraph" && exit 1)
	$(COMPOSE) restart $(SVC)

rebuild: ## Rebuild and restart a service (SVC=name)
	@[ "$(SVC)" ] || (echo "Usage: make rebuild SVC=vibe" && exit 1)
	$(COMPOSE) up -d --build $(SVC)

# ── Monitoring ───────────────────────────────────────────────────
.PHONY: status logs

status: ## Show health status of all services
	@$(COMPOSE_ALL) ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || $(COMPOSE_ALL) ps

logs: ## Tail logs (SVC=name for specific service)
ifdef SVC
	$(COMPOSE) logs -f --tail=50 $(SVC)
else
	$(COMPOSE) logs -f --tail=50
endif

# ── Development ──────────────────────────────────────────────────
.PHONY: test lint shell

test: ## Run Python test suite
	python -m pytest tests/ -x -m "not e2e" --no-header -q

lint: ## Lint with ruff
	ruff check agents/ vibe/ tests/

shell: ## Open shell in a container (SVC=name)
	@[ "$(SVC)" ] || (echo "Usage: make shell SVC=deerflow-langgraph" && exit 1)
	$(COMPOSE) exec $(SVC) bash

# ── Images ───────────────────────────────────────────────────────
.PHONY: build-deerflow build-vibe

build-deerflow: ## Build DeerFlow image from local Paperclip source
	@PAPERCLIP_DIR=$${PAPERCLIP_SOURCE_DIR:-$(HOME)/Repos/paperclip}; \
	echo "Building from $$PAPERCLIP_DIR/deerflow ..."; \
	docker build -f $$PAPERCLIP_DIR/deerflow/backend/Dockerfile \
		-t ghcr.io/$$(grep GHCR_ORG .env 2>/dev/null | cut -d= -f2 || echo tmartin2113)/paperclip-deerflow:latest \
		$$PAPERCLIP_DIR/deerflow

build-vibe: ## Build Vibe agent image
	$(COMPOSE) build vibe
