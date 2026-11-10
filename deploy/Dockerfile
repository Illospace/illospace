FROM python:3.12-slim
ARG ILLO_REVISION=unknown
LABEL org.opencontainers.image.revision=$ILLO_REVISION
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ILLO_REVISION=$ILLO_REVISION
WORKDIR /app
COPY pyproject.toml README.md LICENSE* ./
COPY illo ./illo
COPY collectors ./collectors
RUN pip install --no-cache-dir . && chmod -R a+rX /app && groupadd --gid 10001 illo && useradd --uid 10001 --gid illo --create-home illo
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=5s --start-period=30s --retries=12 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"]
ENTRYPOINT ["illo"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]
