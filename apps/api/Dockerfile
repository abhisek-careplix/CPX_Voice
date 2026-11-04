# CarePlix API + Studio image (Cloud Run). Build from the repository root:
#   gcloud builds submit --config infra/gcp/cloudbuild.yaml
# The same image also runs the synthetic payer sandbox (different command).
# SEC13: pin base images by digest before production; record the SBOM in CI.

FROM node:24-slim AS studio
WORKDIR /web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/web ./
RUN npm run build

FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv

RUN useradd --create-home --uid 10001 api
WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages packages
COPY apps/worker/pyproject.toml apps/worker/pyproject.toml
COPY apps/browser/pyproject.toml apps/browser/pyproject.toml
COPY apps/api apps/api
# Synthetic fixtures: demo workspace seed and the payer sandbox's decisions.
COPY fixtures fixtures
RUN uv sync --frozen --no-dev --package careplix-api \
 && chown -R api /app
COPY --from=studio /web/dist /app/studio

USER api
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PORT=8080 STUDIO_DIST=/app/studio
CMD ["sh", "-c", "uvicorn careplix_api.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
