# Build context is the repository root (see docker-compose.yml):
#   docker build -f server/Dockerfile .

# ---- web UI
FROM node:24-alpine AS ui
WORKDIR /ui
COPY WEBUI/package.json WEBUI/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY WEBUI/ ./
RUN npm run build

# ---- server
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY server/pyproject.toml ./
COPY server/smp ./smp
RUN pip install --quiet . && useradd --no-create-home --shell /usr/sbin/nologin --uid 10001 smp
COPY --from=ui /ui/dist /app/ui
ENV SMP_UI_DIR=/app/ui
USER smp

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
# preflight: explains a wrong .env (secret key, database password) in plain words
# instead of a fast restart loop with a traceback. Migrations are idempotent; running
# them on start keeps upgrades a one-step affair.
CMD ["sh", "-c", "smp preflight && smp migrate && exec smp serve --host 0.0.0.0 --port 8000"]
