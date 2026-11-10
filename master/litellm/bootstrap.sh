#!/usr/bin/env bash
# Generates secrets, starts LiteLLM, mints a restricted virtual key for Laptop 1.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  umask 077
  cat > .env <<EOF
LITELLM_MASTER_KEY=sk-$(openssl rand -hex 24)
POSTGRES_PASSWORD=$(openssl rand -hex 24)
LITELLM_TAG=
EOF
fi
chmod 600 .env
if [ -z "$(grep "^LITELLM_TAG=." .env || true)" ]; then
  echo "Set LITELLM_TAG in $(pwd)/.env to a verified LiteLLM release tag (check their security advisories), then re-run."; exit 1
fi
set -a; . ./.env; set +a

docker compose up -d
for i in $(seq 1 60); do
  curl -fsS http://127.0.0.1:4000/health/liveliness >/dev/null 2>&1 && break
  [ "$i" = 60 ] && { echo "LiteLLM did not start:"; docker compose logs --tail 50 litellm; exit 1; }
  sleep 2
done

# Revoke keys from earlier runs, then mint one non-admin key, rate-limited (models: [] = all role groups).  Give THIS to Laptop 1, never the master key.
curl -sS -o /dev/null http://127.0.0.1:4000/key/delete -H @- -H "Content-Type: application/json" \
  -d '{"key_aliases":["laptop1-orchestrator"]}' <<<"Authorization: Bearer $LITELLM_MASTER_KEY" || true
curl -fsS http://127.0.0.1:4000/key/generate \
  -H @- -H "Content-Type: application/json" \
  -d '{"models":[],"rpm_limit":30,"max_parallel_requests":2,"key_alias":"laptop1-orchestrator"}' \
  <<<"Authorization: Bearer $LITELLM_MASTER_KEY" \
  | python3 -c 'import sys,json;print("\nVirtual key for Laptop 1:", json.load(sys.stdin)["key"])'

