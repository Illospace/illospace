#!/bin/sh
set -eu
if ! docker container inspect illo-test-pg >/dev/null 2>&1; then
  docker run -d --name illo-test-pg \
    -e POSTGRES_USER=illo -e POSTGRES_PASSWORD=illo \
    -p 127.0.0.1:55432:5432 --tmpfs /var/lib/postgresql/data \
    postgres:16 >/dev/null 2>&1 || docker container inspect illo-test-pg >/dev/null
fi
if [ "$(docker inspect --format '{{.State.Running}}' illo-test-pg)" != true ]; then
  docker start illo-test-pg >/dev/null
fi
attempt=0
until docker exec illo-test-pg pg_isready -U illo >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo 'The shared test database did not start.' >&2
    exit 1
  fi
  sleep 1
done
echo 'The shared test database is ready on 127.0.0.1:55432.'
