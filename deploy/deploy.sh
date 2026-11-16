#!/bin/bash
# Deploy the label service on the Bluetooth host (run there, from a checkout).
# Settings come from deploy/.env (copy .env.example). IMAGE_TAG defaults to
# the checked-out commit, which CI has built and pushed.
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -f .env ]]; then
    echo "deploy/.env is missing; copy deploy/.env.example and fill it in" >&2
    exit 1
fi
set -a
. ./.env
set +a
: "${LABEL_HOST:?set LABEL_HOST in deploy/.env}"
export LABEL_NODE="${LABEL_NODE:-$(docker info --format '{{.Name}}')}"
export IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse HEAD)}"
export NGINX_CONF_HASH="$(sha256sum nginx.conf | cut -c1-12)"
if [[ ! -s secrets/clients.json ]]; then
    echo "No client tokens yet; add one with: deploy/tokens.py add <client-name>" >&2
fi
docker compose -f compose.yml pull
docker compose -f compose.yml up -d
docker stack deploy -c stack.yml labels-proxy
