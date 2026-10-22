#!/usr/bin/env bash
# dev-harness check — runs at the end of every agent turn that changed code (Stop hook).
# Must be FREE (no paid APIs), FAST (aim < 2 min) and DETERMINISTIC. Exit non-zero = agent may not finish.
# /dev-harness:init fills this in for the repo; edit freely.
set -euo pipefail
cd "$(dirname "$0")/.."

# Example (Python):
#   python -m pytest -q -x
# Example (Node):
#   npm test --silent
echo "TODO: put this repo's free, fast test command here" >&2
exit 0
