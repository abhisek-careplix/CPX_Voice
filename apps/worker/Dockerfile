# CarePlix agent worker image. Build from the repository root:
#   docker build -f apps/worker/Dockerfile -t careplix-worker .
# SEC13: pin base images by digest before staging; record the SBOM in CI.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv

RUN useradd --create-home --uid 10001 agent
WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages packages
COPY apps/api/pyproject.toml apps/api/pyproject.toml
COPY apps/worker apps/worker
RUN uv sync --frozen --no-dev --package careplix-worker \
 && chown -R agent /app

USER agent
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
# Fetch model assets (e.g. turn detection, noise filters) at build time, not call time.
RUN python -m careplix_worker.main download-files

CMD ["python", "-m", "careplix_worker.main", "start"]
