#!/bin/bash
# Starts the librarian service (the Dockerfile's CMD, and what Serve runs):
# the agent's HTTP API on :7680 (orchestrator.py), which loads .env itself.
set -euo pipefail

exec uvicorn orchestrator:app --host 0.0.0.0 --port 7680
