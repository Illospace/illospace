FROM docker:27-cli

RUN apk add --no-cache \
    bash \
    coreutils \
    docker-cli-compose \
    git \
    jq

WORKDIR /repo

COPY deploy/scripts/self-update-daemon.sh /usr/local/bin/illo-self-update-daemon
COPY deploy/scripts/self-update-healthcheck.sh /usr/local/bin/illo-self-update-healthcheck
RUN chmod +x /usr/local/bin/illo-self-update-daemon /usr/local/bin/illo-self-update-healthcheck

CMD ["illo-self-update-daemon"]
