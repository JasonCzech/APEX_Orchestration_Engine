.PHONY: dev infra-up infra-down migrate test lint typecheck check openapi

dev: ## LangGraph dev server (in-memory; no infra required)
	uv run langgraph dev --no-browser

infra-up: ## Start dev Postgres/Redis/MinIO
	docker compose -f docker-compose.dev.yaml up -d --wait

infra-down:
	docker compose -f docker-compose.dev.yaml down

migrate: ## Apply apex-schema migrations
	uv run alembic upgrade head

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
