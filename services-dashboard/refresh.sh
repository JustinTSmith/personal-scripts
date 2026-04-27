#!/usr/bin/env bash
# Refresh the services dashboard and (optionally) open it.
set -euo pipefail
cd "$(dirname "$0")"
exec /opt/homebrew/bin/python3 scan.py "$@"
