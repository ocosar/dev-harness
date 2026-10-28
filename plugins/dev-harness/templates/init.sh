#!/usr/bin/env bash
# dev-harness start-up step — run once per fresh machine (the harness runs it before the first check of a session).
# Install what the work needs, idempotently. Free only: no paid calls, no secrets printed. Changing this file asks
# the owner. /dev-harness:init fills this in for the repo.
set -euo pipefail

# Python projects (uncomment / adapt):
# python3 -c 'import pytest' 2>/dev/null || pip install -q -r requirements.txt -r requirements-dev.txt

# Node projects:
# [ -d node_modules ] || npm ci --silent

echo "init: ready"
