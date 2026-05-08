FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    ILLO_PRIVATE_HOME=/data/private \
    WORKSPACE_ROOT=/workspaces \
    ILLO_BROWSER_RUNTIME_DIR=/opt/illo-browser \
    ILLO_BROWSER_CHROME_BIN=/usr/bin/chromium

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        chromium \
        curl \
        fonts-liberation \
        git \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-production.txt pyproject.toml alembic.ini ./
RUN python3 -m pip install --upgrade pip setuptools wheel \
    && python3 -m pip install -r requirements-production.txt

COPY brain ./brain
COPY ops/install-browser-runtime.sh ./ops/install-browser-runtime.sh
COPY README.md LICENSE NOTICE.md ./

RUN chmod +x ./ops/install-browser-runtime.sh \
    && ./ops/install-browser-runtime.sh python3 \
    && useradd --create-home --uid 10001 illo \
    && mkdir -p /data/private /app/brain/uploads /workspaces \
    && chown -R illo:illo /data /app/brain/uploads /workspaces

USER illo

EXPOSE 8000

CMD ["uvicorn", "brain.app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
