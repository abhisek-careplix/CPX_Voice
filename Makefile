.PHONY: payer-sandbox connect-livekit connect-nexhealth nexhealth-inventory connect-stedi openemr-up openemr-down openemr-setup setup lint fmt test check api studio worker-dev db-up probe-readiness probe-gemini browser-session web-check

setup:
	uv sync
	npm --prefix apps/web ci

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

test:
	uv run pytest -q

web-check:
	npm --prefix apps/web run build

check: lint test web-check

db-up:
	docker compose up -d postgres

# Prompts for LiveKit URL, key and secret (hidden), writes .env and checks the connection.
connect-livekit:
	./scripts/connect-livekit.sh

# Prompts for the NexHealth API key (hidden), checks it, picks the practice location, writes .env.
connect-nexhealth:
	./scripts/connect-nexhealth.sh

# Lists every practice the NexHealth production key reaches: EHR, sync health, what CarePlix can do.
nexhealth-inventory:
	./scripts/nexhealth-inventory.sh

# Local OpenEMR (real EHR) for end-to-end testing: start it, then connect and seed a synthetic clinic.
openemr-up:
	docker compose -f infra/openemr/docker-compose.yml up -d

openemr-down:
	docker compose -f infra/openemr/docker-compose.yml down

openemr-setup:
	uv run python scripts/openemr_setup.py

# Prompts for the Stedi API key (hidden), checks it and runs Stedi's free mock eligibility check.
connect-stedi:
	./scripts/connect-stedi.sh

# Settings load .env automatically when present.
api:
	uv run uvicorn careplix_api.main:app --reload --reload-dir apps/api/src --port 8080

# Synthetic payer exposing Da Vinci PAS Claim/$inquire and $submit (development only).
payer-sandbox:
	uv run uvicorn careplix_api.payer_sandbox:app --port 8091

# Studio web app on http://localhost:5173 (proxies /api to the API above).
studio:
	npm --prefix apps/web run dev

# Runs the agent worker locally: registers with LiveKit Cloud and claims calls from the local API.
worker-dev:
	uv run --env-file .env python -m careplix_worker.main dev

# G0 evidence: connection inventory (LiveKit, trunks, routes, DB, model config).
probe-readiness:
	uv run python -m careplix_api.probes.readiness_evidence

# G0 evidence: Vertex Gemini Live connect, first-audio latency, optional soak for session limits.
probe-gemini:
	uv run python -m careplix_worker.probes.gemini_live $(ARGS)

# G0 evidence: room + explicit dispatch + short-lived browser token for a two-way test.
browser-session:
	uv run python -m careplix_api.probes.browser_session
