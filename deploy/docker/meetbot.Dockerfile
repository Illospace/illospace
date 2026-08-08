FROM python:3.12-slim-bookworm

ARG ILLO_BUILD_COMMIT=unknown

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

ENV ILLO_BUILD_COMMIT=${ILLO_BUILD_COMMIT}

LABEL org.opencontainers.image.revision=${ILLO_BUILD_COMMIT}

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        xauth \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY meetbot/requirements.txt /tmp/meetbot-requirements.txt
RUN python3 -m pip install --upgrade pip setuptools wheel \
    && python3 -m pip install -r /tmp/meetbot-requirements.txt \
    && python3 -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY meetbot ./meetbot
COPY deploy/docker/meetbot-entrypoint.sh /usr/local/bin/meetbot-entrypoint.sh

RUN useradd --create-home --uid 10001 illo \
    && mkdir -p /data/private/meetbot /app/brain/uploads \
    && chown -R illo:illo /data /app/brain/uploads

USER illo

EXPOSE 8010

ENTRYPOINT ["/usr/local/bin/meetbot-entrypoint.sh"]
