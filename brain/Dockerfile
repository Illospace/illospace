FROM python:3.12-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates libstdc++6 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10001 --create-home brain
WORKDIR /app
COPY . /app/brain
RUN pip install --no-cache-dir /app/brain \
 && chmod -R a+rX /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp TMPDIR=/tmp
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "brain.server:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
