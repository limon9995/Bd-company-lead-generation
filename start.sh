#!/usr/bin/env bash
# BD Lead Generation - one-command start for macOS / Linux (needs Docker running).  ./start.sh
set -euo pipefail
cd "$(dirname "$0")"
echo "== BD Lead Generation =="

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running. Install/start Docker Desktop (https://www.docker.com/products/docker-desktop/) and run this again."
  exit 1
fi

rand() { head -c "$1" /dev/urandom | base64 | tr '+/' '-_' | tr -d '\n'; }
if [ ! -f .env ]; then
  # only on the first run - never overwrite: APP_SECRET_KEY decrypts your saved API keys
  sed -e "s|^APP_SECRET_KEY=.*|APP_SECRET_KEY=$(rand 32)|" \
      -e "s|^SESSION_SECRET=.*|SESSION_SECRET=$(rand 32)|" \
      -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(rand 18)|" .env.example > .env
  chmod 600 .env
  echo "Created .env with new secrets. Keep it private and back it up."
fi

echo "Starting (first time 5-10 minutes: downloads Python, Postgres and Chromium)..."
docker compose up -d --build

printf "Waiting for the admin panel"
for _ in $(seq 1 150); do
  if curl -fs http://localhost:8000/health >/dev/null 2>&1; then ok=1; break; fi
  printf "."; sleep 2
done
echo
[ "${ok:-0}" = 1 ] || { echo "The panel did not start. Run: docker compose logs web"; exit 1; }

if [ ! -f .admin-created ]; then
  read -rp "Admin email for the panel: " email
  echo "Choose a password (10+ characters)."
  docker compose exec web python -m scripts.create_admin "$email" && touch .admin-created
fi

echo "Ready: http://localhost:8000  (only this computer can open it)"
(command -v open >/dev/null && open http://localhost:8000) || (command -v xdg-open >/dev/null && xdg-open http://localhost:8000) || true
echo "Stop later with ./stop.sh. Your data and API keys stay on this computer."
