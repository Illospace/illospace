# CI uses this fixture only until the real brain Dockerfile exists.
FROM python:3.12-slim
COPY --chmod=0444 deploy/smoke-brain.py /app/server.py
USER 10001:10001
EXPOSE 8080
CMD ["python", "/app/server.py"]
