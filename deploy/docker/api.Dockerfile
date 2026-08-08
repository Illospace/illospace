FROM python:3.13-slim-bookworm

ARG ILLO_BUILD_COMMIT=unknown
ARG ILLO_BUILD_TIME=unknown

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    ILLO_PRIVATE_HOME=/data/private \
    WORKSPACE_ROOT=/workspaces \
    ILLO_BROWSER_RUNTIME_DIR=/opt/illo-browser \
    ILLO_BROWSER_CHROME_BIN=/usr/bin/chromium \
    ILLO_BUILD_COMMIT=${ILLO_BUILD_COMMIT} \
    ILLO_BUILD_TIME=${ILLO_BUILD_TIME}

LABEL org.opencontainers.image.revision=${ILLO_BUILD_COMMIT} \
      org.opencontainers.image.created=${ILLO_BUILD_TIME}

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        chromium \
        curl \
        ffmpeg \
        fonts-liberation \
        git \
        jq \
        libpq-dev \
        nodejs \
        npm \
        openssh-client \
        ripgrep \
        unzip \
        zip \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-production.txt pyproject.toml alembic.ini ./
RUN python3 -m pip install --upgrade pip setuptools wheel \
    && python3 -m pip install -r requirements-production.txt

COPY brain ./brain
COPY deploy/compose/runtime-services.json ./deploy/compose/runtime-services.json
COPY deploy/compose/workspace-tools.json ./deploy/compose/workspace-tools.json
COPY deploy/compose/provider-alert-severity.json ./deploy/compose/provider-alert-severity.json
COPY ops/install-browser-runtime.sh ./ops/install-browser-runtime.sh
COPY README.md LICENSE NOTICE.md ./

RUN chmod +x ./ops/install-browser-runtime.sh \
    && ./ops/install-browser-runtime.sh python3 \
    && useradd --create-home --uid 10001 illo \
    && mkdir -p /data/private /data/private/npm-cache /data/private/npm-global /app/brain/uploads /workspaces \
    && chown -R illo:illo /data /app/brain/uploads /workspaces

ENV NPM_CONFIG_CACHE=/data/private/npm-cache \
    NPM_CONFIG_PREFIX=/data/private/npm-global \
    NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    PATH=/data/private/npm-global/bin:$PATH

USER illo

EXPOSE 8000

CMD ["uvicorn", "brain.app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
