.PHONY: setup lint fmt test check api worker-dev db-up probe-readiness probe-gemini browser-session

setup:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

test:
	uv run pytest -q

check: lint test

db-up:
	docker compose up -d postgres

api:
	uv run --env-file .env uvicorn careplix_api.main:app --reload --port 8080

# Runs the spike worker locally against LiveKit Cloud (registers for explicit dispatch).
worker-dev:
	uv run --env-file .env python -m careplix_worker.main dev

# G0 evidence: connection inventory (LiveKit, trunks, routes, DB, model config).
probe-readiness:
	uv run --env-file .env python -m careplix_api.probes.readiness_evidence

# G0 evidence: Vertex Gemini Live connect, first-audio latency, optional soak for session limits.
probe-gemini:
	uv run --env-file .env python -m careplix_worker.probes.gemini_live $(ARGS)

# G0 evidence: room + explicit dispatch + short-lived browser token for a two-way test.
browser-session:
	uv run --env-file .env python -m careplix_api.probes.browser_session
