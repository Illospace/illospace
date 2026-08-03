FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

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

RUN useradd --create-home --uid 10001 illo \
    && mkdir -p /data/private/meetbot /app/brain/uploads \
    && chown -R illo:illo /data /app/brain/uploads

USER illo

EXPOSE 8010

ENTRYPOINT ["xvfb-run", "-a", "--server-args=-screen 0 1280x720x24"]
CMD ["uvicorn", "meetbot.app:app", "--host", "0.0.0.0", "--port", "8010"]
