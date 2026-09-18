# Convenience targets. `make help` lists them.
.DEFAULT_GOAL := help
VENV ?= .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

.PHONY: help venv install migrate seed demo api worker test frontend build up down logs status verify-sources compose-check purge-mock backup-now restore deploy

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-12s %s\n", $$1, $$2}'

venv:  ## Create the Python virtualenv
	python3 -m venv $(VENV)

install: venv  ## Install backend dependencies (plus pytest)
	$(PIP) install -q -r backend/requirements.txt pytest

migrate:  ## Apply database migrations
	cd backend && ../$(PY) -m alembic upgrade head

seed:  ## Seed reference data and the default user
	cd backend && ../$(PY) manage.py seed

demo:  ## Seed + load the sample archive + poll + run the pipeline + embed
	cd backend && ../$(PY) manage.py demo

embed:  ## Embed events that have no vector (or whose vector is stale)
	cd backend && ../$(PY) manage.py embed

deploy:  ## Print the production deploy command (runs on the VPS, as root)
	@echo 'Run this ON THE SERVER, as root:'
	@echo '  DOMAIN=your.domain LETSENCRYPT_EMAIL=you@example.com sudo -E ./deploy/deploy.sh'
	@echo 'It is idempotent and stops to ask before anything rate-limited or destructive.'

compose-check:  ## Fail if anything but nginx publishes a port
	python3 scripts/check_compose_exposure.py

purge-mock:  ## Delete synthetic events/signals/prices (ARGS="--dry-run")
	cd backend && ../$(PY) manage.py purge-mock $(ARGS)

backup-now:  ## Take a database backup immediately
	docker compose run --rm backup /usr/local/bin/backup.sh once

restore:  ## Restore from a dump: make restore DUMP=backups/trumpmarket-....dump
	@test -n "$(DUMP)" || (echo "usage: make restore DUMP=backups/trumpmarket-....dump" && exit 1)
	docker compose stop api worker
	docker compose exec -T db dropdb -U postgres --if-exists trumpmarket
	docker compose exec -T db createdb -U postgres trumpmarket
	docker compose exec -T db pg_restore -U postgres -d trumpmarket --no-owner < $(DUMP)
	docker compose start api worker
	@echo "restored from $(DUMP)"

verify-sources:  ## Probe live feeds + market data (ARGS="--all-sources -v")
	$(PY) scripts/verify_sources.py $(ARGS)

status:  ## Print pipeline, source and market-data health
	cd backend && ../$(PY) manage.py status

vapid:  ## Generate a VAPID keypair for Web Push
	$(PY) scripts/generate_vapid_keys.py

api:  ## Run the API on :8000
	cd backend && ../$(PY) -m uvicorn app.main:app --reload --port 8000

worker:  ## Run the background worker
	cd backend && ../$(PY) -m app.worker.scheduler

test:  ## Run the backend test suite
	cd backend && ../$(PY) -m pytest -q

frontend:  ## Run the frontend dev server on :5173
	cd frontend && npm run dev

build:  ## Build the frontend bundle
	cd frontend && npm run build

up:  ## docker compose up --build
	docker compose up --build

down:  ## docker compose down
	docker compose down
