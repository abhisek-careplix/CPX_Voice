# CarePlix API image (Cloud Run). Build from the repository root:
#   docker build -f apps/api/Dockerfile -t careplix-api .
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv

RUN useradd --create-home --uid 10001 api
WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages packages
COPY apps/worker/pyproject.toml apps/worker/pyproject.toml
COPY apps/api apps/api
# Synthetic fixtures seed development workspaces only (SEED_DEMO_DATA).
COPY fixtures fixtures
RUN uv sync --frozen --no-dev --package careplix-api \
 && chown -R api /app

USER api
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PORT=8080
CMD ["sh", "-c", "uvicorn careplix_api.main:app --host 0.0.0.0 --port ${PORT}"]
