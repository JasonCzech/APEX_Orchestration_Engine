.PHONY: dev infra-up infra-down migrate bootstrap test lint typecheck check openapi \
	image-build compose-up compose-down compose-ha-up helm-install aks-up aks-down

dev: ## LangGraph dev server (in-memory; no infra required)
	uv run langgraph dev --no-browser

infra-up: ## Start dev Postgres/Redis/MinIO
	docker compose -f docker-compose.dev.yaml up -d --wait

infra-down:
	docker compose -f docker-compose.dev.yaml down

migrate: ## Apply apex-schema migrations
	uv run alembic upgrade head

bootstrap: ## Apply a bootstrap document (default deploy/bootstrap/example.json)
	uv run python scripts/bootstrap.py deploy/bootstrap/example.json

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run pyright

check: lint typecheck test

openapi: ## Export the committed /v1 OpenAPI spec (M2)
	uv run python scripts/export_openapi.py

# ── deploy (see scripts/deploy.py and docs/runbooks/) ────────────────────────
image-build: ## Build server + dashboard images (TAG=local by default)
	uv run python scripts/deploy.py image-build $(TAG)

compose-up: ## Build + start the full local stack (infra + server + dashboard)
	uv run python scripts/deploy.py compose-up

compose-down: ## Stop the full local stack
	uv run python scripts/deploy.py compose-down

compose-ha-up: ## Start the HA soak rig (needs the license env vars)
	uv run python scripts/deploy.py compose-ha-up

helm-install: ## helm upgrade --install into the apex namespace
	uv run python scripts/deploy.py helm-install

aks-up: ## Provision Azure + deploy (APEX_ENV=dev|staging|prod, requires az login)
	uv run python scripts/deploy.py aks-up

aks-down: ## Destroy the Azure stack for APEX_ENV
	uv run python scripts/deploy.py aks-down
