# Shared image for both backend services (state_service, orchestrator_service)
# — same codebase, same dependency set (pyproject.toml packages agents/state/
# tools/services as one wheel). Which service actually runs is selected at
# deploy time via the SERVICE_MODULE env var, so this image is built once and
# deployed twice rather than duplicating the dependency-install layer.
FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

# Dependencies first so this layer only rebuilds when pyproject.toml/uv.lock
# actually change, not on every source edit.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

ENV PORT=8080
ENV SERVICE_MODULE=services.state_service.main

# Shell form (not exec-array) so $PORT/$SERVICE_MODULE — both set by Cloud
# Run/`--set-env-vars` at deploy time, not known at build time — are actually
# substituted.
CMD uv run uvicorn ${SERVICE_MODULE}:app --host 0.0.0.0 --port ${PORT}
