# CarePlix browser runtime (Cloud Run, private). Build from the repository root.
# The Playwright image ships Chromium and its system libraries matching playwright==1.63.0.
# SEC13: pin base images by digest before production.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv

RUN useradd --create-home --uid 10001 browser
WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages packages
COPY apps/api/pyproject.toml apps/api/pyproject.toml
COPY apps/worker/pyproject.toml apps/worker/pyproject.toml
COPY apps/browser apps/browser
RUN uv sync --frozen --no-dev --package careplix-browser --python /usr/bin/python3 \
 && chown -R browser /app

USER browser
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PORT=8080 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
CMD ["sh", "-c", "uvicorn --factory careplix_browser.app:app --host 0.0.0.0 --port ${PORT}"]
