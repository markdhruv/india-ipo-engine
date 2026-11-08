#!/usr/bin/env bash
# One-click start for macOS / Linux. Creates the venv on first run.
set -e
cd "$(dirname "$0")"
PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  echo "First run - setting up..."
  python3 -m venv .venv
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet -r requirements.txt
fi
exec "$PY" run.py app "$@"
