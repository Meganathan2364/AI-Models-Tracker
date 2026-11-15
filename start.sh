#!/bin/sh
set -eu

project_root="${MODEL_ATLAS_ROOT:-$(CDPATH= cd "$(dirname "$0")" && pwd)}"
cd "$project_root"

if [ ! -f data/catalog.json ]; then
  python -m backend.jobs all
fi

exec uvicorn backend.app:app --host 0.0.0.0 --port "${PORT:-8000}"
