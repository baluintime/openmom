#!/usr/bin/env bash
# Start the NIFTY Intraday Options Scalper (Upstox)
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
if [ ! -f .env ]; then
  echo "No .env found — copying .env.example. Fill in your Upstox API credentials."
  cp .env.example .env
fi
exec .venv/bin/python -m app.main
