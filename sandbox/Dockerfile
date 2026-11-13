FROM python:3.12-slim-bookworm
ARG ILLO_REVISION=unknown
LABEL org.opencontainers.image.revision=$ILLO_REVISION
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ripgrep jq less file findutils diffutils \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10002 --create-home sandbox
RUN mkdir -p /repos && chown 10001:10001 /repos && chmod 755 /repos
WORKDIR /app
COPY . /app/sandbox
RUN pip install --no-cache-dir 'fastapi>=0.115,<1' 'uvicorn>=0.31,<1' \
 && chmod -R a+rX /app && chmod 444 /app/sandbox/gitconfig
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10002:10002
EXPOSE 8600
CMD ["python", "-m", "uvicorn", "sandbox.server:app", "--host", "0.0.0.0", "--port", "8600", "--no-access-log"]
