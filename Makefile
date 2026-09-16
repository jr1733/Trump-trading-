# Convenience targets. `make help` lists them.
.DEFAULT_GOAL := help
VENV ?= .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

.PHONY: help venv install migrate seed demo api worker test frontend build up down logs

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

demo:  ## Seed + load the sample archive + poll + run the pipeline
	cd backend && ../$(PY) manage.py demo

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
