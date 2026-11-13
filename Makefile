TEST_DATABASE_URL ?= postgresql+asyncpg://agrobot:agrobot@localhost:5432/agrobot_test

.PHONY: up down logs seed evaluate test lint typecheck migration-check check

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f

seed:
	uv run python -m app.seed

evaluate:
	uv run python -m app.worker --once

test:
	docker compose up -d --wait db
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy app tests

migration-check:
	DATABASE_URL=$(TEST_DATABASE_URL) uv run alembic upgrade head
	DATABASE_URL=$(TEST_DATABASE_URL) uv run alembic downgrade base
	DATABASE_URL=$(TEST_DATABASE_URL) uv run alembic upgrade head

check: lint typecheck
	docker compose up -d --wait db
	$(MAKE) migration-check
	uv run pytest --cov=app --cov-report=term-missing --cov-fail-under=90
