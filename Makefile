.PHONY: help install up down migrate revision run seed load test lint format \
        check-api-key sync-event list-events psql connector install-hooks snapshot \
        reader-password \
        admin-key secrets sonar-scan sonar-gate coverage

help:
	@echo "Getting started:"
	@echo "  make install       — create the venv and sync deps"
	@echo "  make up            — start the local Postgres"
	@echo "  make migrate       — alembic upgrade head"
	@echo "  make seed email=you@example.com handle=you [admin=1]  — create a reader"
	@echo "  make reader-password — generate a password for CROSSOVER_PASSWORD_<HANDLE>"
	@echo "  make run           — serve on :8020 with reload (override: port=NNNN)"
	@echo ""
	@echo "Catalog data (Marvel's API is discontinued — see docs/gates.md):"
	@echo "  make load                     — load curation YAML + apply vendored snapshots"
	@echo "  make snapshot slug=king-in-black — rebuild a snapshot from the mirror"
	@echo ""
	@echo "Legacy Marvel API (kept for a future replacement API; the old one is dead):"
	@echo "  make check-api-key            — verify credentials + digital-id coverage"
	@echo "  make list-events q=\"King in\"  — find an event's numeric Marvel id"
	@echo "  make sync-event slug=king-in-black — fetch the roster, confirm digital ids"
	@echo ""
	@echo "Development:"
	@echo "  make install-hooks — install the pre-commit / pre-push gates"
	@echo "  make test          — pytest (unit tests need no database)"
	@echo "  make coverage      — pytest with coverage, writes coverage.xml"
	@echo "  make lint / format — ruff"
	@echo "  make secrets       — scan the tree for committed credentials"
	@echo "  make sonar-scan    — scan + quality gate against local Watchtower"
	@echo "  make admin-key     — generate a CROSSOVER_ADMIN_KEY"
	@echo "  make connector name=... email=... redirect=...  — register an OAuth client"

install:
	uv venv --python 3.12
	uv pip install -r pyproject.toml --extra-index-url https://pypi.org/simple
	uv pip install pytest pytest-asyncio pytest-cov respx ruff

up:
	docker compose up -d
	@echo "Waiting for postgres..."
	@until docker compose exec -T postgres pg_isready -U crossover >/dev/null 2>&1; do sleep 1; done
	@echo "Postgres ready on 5433."

down:
	docker compose down

migrate:
	.venv/bin/alembic upgrade head

revision:
	@if [ -z "$(m)" ]; then echo "Usage: make revision m=\"message\""; exit 1; fi
	.venv/bin/alembic revision --autogenerate -m "$(m)"

# 8020: 8000 is conduct-api, 8010 is another local Django app, and Watchtower's Alloy
# scrape target (docker/alloy-config.d/local-scrapes.alloy) points here.
port ?= 8020
run:
	.venv/bin/uvicorn main:app --reload --host 0.0.0.0 --port $(port)

seed:
	@if [ -z "$(email)" ]; then \
		echo 'Usage: make seed email=you@example.com [handle=you] [name=You] [admin=1]'; exit 1; fi
	.venv/bin/python -m scripts.cli seed "$(email)" --name "$(name)" \
		$(if $(handle),--handle "$(handle)") $(if $(admin),--admin)

# Generate a reader password to paste into .env or `heroku config:set`.
reader-password:
	@.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(18))"

load:
	.venv/bin/python -m scripts.cli load-curation

# Rebuild a vendored catalog snapshot. Marvel's API is gone (docs/gates.md), so
# this pulls from a third-party mirror, slowly and once, and commits the result.
snapshot:
	@if [ -z "$(slug)" ]; then echo "Usage: make snapshot slug=king-in-black"; exit 1; fi
	.venv/bin/python -m scripts.fetch_snapshot "$(slug)"

# SPEC §0's precondition. Run this before curating anything else: it prints the
# digital-id coverage number, which is the go/no-go on the whole linking premise.
check-api-key:
	.venv/bin/python -m scripts.cli check-api-key

list-events:
	@if [ -z "$(q)" ]; then echo "Usage: make list-events q=\"King in Black\""; exit 1; fi
	.venv/bin/python -m scripts.cli list-events "$(q)"

sync-event:
	@if [ -z "$(slug)" ]; then echo "Usage: make sync-event slug=king-in-black"; exit 1; fi
	.venv/bin/python -m scripts.cli sync-event "$(slug)"

connector:
	@if [ -z "$(name)" ] || [ -z "$(email)" ] || [ -z "$(redirect)" ]; then \
		echo 'Usage: make connector name="Claude iOS" email=you@example.com redirect=https://claude.ai/api/mcp/auth_callback'; exit 1; fi
	.venv/bin/python -m scripts.cli register-connector "$(name)" "$(email)" "$(redirect)"

test:
	.venv/bin/python -m pytest

lint:
	.venv/bin/ruff check .

format:
	.venv/bin/ruff format .

psql:
	docker compose exec postgres psql -U crossover -d crossover

# --- quality gates ---

# Symlinked rather than copied, so editing scripts/hooks/* takes effect
# immediately and the hooks stay version-controlled.
install-hooks:
	@mkdir -p .git/hooks
	@for hook in pre-commit pre-push; do \
		ln -sf ../../scripts/hooks/$$hook .git/hooks/$$hook; \
		echo "installed .git/hooks/$$hook -> scripts/hooks/$$hook"; \
	done
	@echo "Bypass a gate once with --no-verify."

secrets:
	.venv/bin/python scripts/check_secrets.py

coverage:
	.venv/bin/python -m pytest -q --cov --cov-report=xml --cov-report=term

# Scan against Watchtower's local SonarQube, then block on its quality gate.
# Reads SONAR_TOKEN from .env. Results: http://localhost:9000/dashboard?id=crossover
sonar-scan: coverage
	@if [ -z "$$SONAR_TOKEN" ] && ! grep -q '^SONAR_TOKEN=' .env 2>/dev/null; then \
		echo "Set SONAR_TOKEN in .env (generate one at http://localhost:9000)"; exit 1; \
	fi
	@set -a; . ./.env; set +a; \
	docker run --rm \
		-e SONAR_HOST_URL=http://host.docker.internal:9000 \
		-e SONAR_TOKEN=$$SONAR_TOKEN \
		-v "$$(pwd):/usr/src" \
		sonarsource/sonar-scanner-cli:latest
	@$(MAKE) --no-print-directory sonar-gate

sonar-gate:
	.venv/bin/python scripts/sonar_gate.py

# The admin key has no default on purpose — a default in a public repo is a
# published credential. This generates one to paste into .env.
admin-key:
	@.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(32))"
