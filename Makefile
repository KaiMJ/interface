.PHONY: help dev up down build logs shell targetapp console api test lint fmt install clean diagram

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-14s\033[0m %s\n", $$1, $$2}'

up:            ## Bring up all three services
	docker compose up --build

down:          ## Stop everything
	docker compose down

build:         ## Build images without starting
	docker compose build

logs:          ## Tail the automation container
	docker compose logs -f desktop

shell:         ## Shell into the desktop container
	docker compose exec desktop bash

install:       ## Install every workspace's deps locally (no docker)
	cd backend   && uv sync
	cd targetapp && pnpm install
	cd console   && pnpm install

dev:           ## Run targetapp + console + api together, one terminal (Ctrl-C stops all)
	./scripts/dev.sh

targetapp:     ## Run just the mock bank (no docker)
	cd targetapp && pnpm dev

console:       ## Run just the operator console (no docker)
	cd console && pnpm dev

api:           ## Run just the backend (no docker)
	cd backend && uv run uvicorn cua.api.main:app --reload --port 8000

test:          ## Backend tests (no browser, no display, no model, no target app)
	cd backend && uv run pytest -q

lint:          ## ruff + mypy strict
	cd backend && uv run ruff check . && uv run mypy src

fmt:           ## ruff format
	cd backend && uv run ruff format .

clean:         ## Tear down and remove caches
	docker compose down -v
	rm -rf backend/.pytest_cache backend/.ruff_cache backend/.mypy_cache
