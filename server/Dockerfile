# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE
# Build context is the repository root (see docker-compose.yml):
#   docker build -f server/Dockerfile .

# ---- versions: each build stage gets only its own section of Version.toml, so a stage is
# rebuilt when its own version changes, not on every release (the agent takes minutes)
FROM python:3.13-slim AS versions
COPY Version.toml tools/version.sh /v/
RUN for s in WEBUI agent; do \
        v=$(sh /v/version.sh "$s" /v/Version.toml) || exit 1; \
        printf '[%s]\nversion = "%s"\n' "$s" "$v" > "/v/$s.toml"; \
    done

# ---- web UI
FROM node:24-alpine AS ui
WORKDIR /ui
COPY WEBUI/package.json WEBUI/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY WEBUI/ ./
# vite.config.ts compiles in [WEBUI] of ../Version.toml (here: /Version.toml)
COPY --from=versions /v/WEBUI.toml /Version.toml
RUN npm run build

# ---- Windows agent (Rust, cross-compiled here; SMP offers it at /downloads/smp-agent.exe)
# Same toolchain as agent/Dockerfile.build: keep the two versions in step.
FROM rust:1.100.0-slim-trixie AS agent
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc-mingw-w64-x86-64 \
 && rm -rf /var/lib/apt/lists/* \
 && rustup target add x86_64-pc-windows-gnu
WORKDIR /agent
# 1) The dependencies, with an empty main.rs: Docker keeps this layer until
#    Cargo.toml / Cargo.lock change, so later builds compile only SMP's own code.
#    (Cargo warns "unused dependency" here: expected, main.rs is empty.)
COPY agent/Cargo.toml agent/Cargo.lock ./
COPY agent/.cargo ./.cargo
COPY agent/smp-agent/Cargo.toml ./smp-agent/
RUN mkdir smp-agent/src && echo 'fn main() {}' > smp-agent/src/main.rs \
 && cargo build --release --locked --target x86_64-pc-windows-gnu \
 && rm -r smp-agent/src
# 2) The agent itself. Its version is [agent] of Version.toml: build.rs reads
#    ../../Version.toml (here: /Version.toml, only the [agent] section), and the Devices
#    page shows the same value from smp-agent.version.
COPY agent/smp-agent ./smp-agent
COPY --from=versions /v/agent.toml /Version.toml
COPY tools/version.sh /
RUN touch smp-agent/src/main.rs \
 && cargo build --release --locked --target x86_64-pc-windows-gnu \
 && mkdir /out \
 && cp target/x86_64-pc-windows-gnu/release/smp-agent.exe /out/ \
 && sh /version.sh agent /Version.toml > /out/smp-agent.version

# ---- server
FROM python:3.13-slim
# PIP_RETRIES / PIP_DEFAULT_TIMEOUT: a slow or briefly unreachable PyPI (school networks)
# doesn't fail the build at once; pip also downloads setuptools for every build here.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_RETRIES=10 PIP_DEFAULT_TIMEOUT=60
WORKDIR /app

COPY server/pyproject.toml Version.toml ./
COPY server/smp ./smp
# smp/version.py reads [server] (and the others) from here, when installing and at runtime
ENV SMP_VERSION_FILE=/app/Version.toml
RUN pip install --quiet . && useradd --no-create-home --shell /usr/sbin/nologin --uid 10001 smp
COPY --from=ui /ui/dist /app/ui
COPY --from=agent /out /app/downloads
ENV SMP_UI_DIR=/app/ui SMP_AGENT_DOWNLOAD_DIR=/app/downloads
USER smp

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
# preflight: explains a wrong .env (secret key, database password) in plain words
# instead of a fast restart loop with a traceback. Migrations are idempotent; running
# them on start keeps upgrades a one-step affair.
CMD ["sh", "-c", "smp preflight && smp migrate && exec smp serve --host 0.0.0.0 --port 8000"]
