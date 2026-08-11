#!/usr/bin/env bash
# Cloud Agent install script: installs backend (Python) and frontend (npm)
# dependencies. Must stay idempotent — it can run repeatedly against cached
# or partially prepared state.
set -euo pipefail

cd "$(dirname "$0")/.."

# Ubuntu marks the system Python as externally managed (PEP 668), so
# user-level installs need --break-system-packages.
python3 -m pip install --user --break-system-packages -r requirements.txt pytest pytest-asyncio

npm ci --prefix frontend
