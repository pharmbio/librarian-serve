#!/bin/bash
# Starts the app service (the Dockerfile's CMD, and what Serve runs): the web
# UI and its API on :8080 (main.py), which loads .env itself.
set -euo pipefail

# Trust X-Forwarded-Proto from any proxy in front of the container, so the app
# knows when the browser is on HTTPS and marks the session cookie Secure.
exec uvicorn main:app --host 0.0.0.0 --port 8080 --forwarded-allow-ips "*"
