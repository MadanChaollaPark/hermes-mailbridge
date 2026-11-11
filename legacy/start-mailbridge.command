#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then
  exec .venv/bin/python -m mailbridge serve
else
  exec python3 -m mailbridge serve
fi
