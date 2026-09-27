#!/usr/bin/env bash
# Deploy or update on the VM (run as the "intraday" user in /opt/intraday).
#   infrastructure/scripts/deploy.sh
# Pulls, checks secrets/paper mode, builds, migrates, restarts, and waits for health.
# The engine always comes up PAUSED-safe: it reconciles before any entry.
set -euo pipefail
cd "$(dirname "$0")/../.."
[ -f .env ] || { echo ".env missing (copy .env.example and fill it in)"; exit 1; }
set -a; . ./.env; set +a
[ "${TRADING_MODE:-}" = "PAPER" ] || { echo "TRADING_MODE must be PAPER"; exit 1; }
case "${ALPACA_BASE_URL%/}" in
  https://paper-api.alpaca.markets) ;;
  *) echo "ALPACA_BASE_URL must be https://paper-api.alpaca.markets"; exit 1 ;;
esac
grep -Eq '=(.*CHANGE_ME|.*PLACEHOLDER)' .env && { echo ".env still contains placeholders"; exit 1; }

git pull --ff-only
docker compose build
docker compose up -d postgres redis
docker compose --profile ops run --rm migrate
docker compose up -d
for i in $(seq 1 30); do
  if docker compose exec -T api python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/api/v1/health/ready',timeout=3)" 2>/dev/null; then
    echo "api ready"; docker compose ps; exit 0
  fi
  sleep 2
done
echo "api did not become ready"; docker compose logs --tail=50 api engine; exit 1
