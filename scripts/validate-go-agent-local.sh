#!/bin/sh
set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
compose_file="$repository_root/infra/local/docker-compose.go.yml"
project_name="prd-agent-go-local"

docker compose -p "$project_name" -f "$compose_file" up --build -d postgres redis
docker compose -p "$project_name" -f "$compose_file" run --build --rm go-migrate
docker compose -p "$project_name" -f "$compose_file" up --build -d python-agent go-api go-maintenance

if ! python3 "$repository_root/scripts/verify_go_agent_flow.py"; then
  docker compose -p "$project_name" -f "$compose_file" ps
  docker compose -p "$project_name" -f "$compose_file" logs --tail 200 go-api go-maintenance python-agent
  exit 1
fi

docker compose -p "$project_name" -f "$compose_file" ps
