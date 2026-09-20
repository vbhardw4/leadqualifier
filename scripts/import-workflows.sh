#!/usr/bin/env bash
# Import the versioned n8n workflows with stable IDs.
# Run after `docker compose up -d`. The compose stack already runs this once via
# the n8n-import service; use this script to re-import after editing the JSON.
set -euo pipefail
cd "$(dirname "$0")/.."

# Copy workflow files into the n8n container and import with stable IDs.
docker compose cp n8n/workflows/leadqualifier-errors.json n8n:/tmp/lq-errors.json
docker compose cp n8n/workflows/leadqualifier.json n8n:/tmp/lq-main.json
docker compose exec -T n8n n8n import:workflow --input=/tmp/lq-errors.json --id=c97ca211-736d-463f-9d3d-d20df97f7db0
docker compose exec -T n8n n8n import:workflow --input=/tmp/lq-main.json --id=ae2e6ae0-f547-4637-9770-72646cf4adaa

echo "done. Activate both workflows in the n8n UI (http://localhost:5678):"
echo "  1. 'LeadQualifier - Error Handler'  -> toggle Active"
echo "  2. 'LeadQualifier'                 -> toggle Active"
echo "The main workflow's settings already point at the error workflow by stable ID."
