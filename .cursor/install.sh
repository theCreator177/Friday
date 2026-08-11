#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> JARVIS Cloud Agent install"

if [[ ! -d .venv ]]; then
  echo "Creating Python virtual environment..."
  python3 -m venv .venv
fi

echo "Installing Python dependencies..."
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "Installing Playwright Chromium..."
.venv/bin/playwright install chromium --with-deps

echo "Installing frontend dependencies..."
(cd frontend && npm ci)

if [[ ! -f key.pem || ! -f cert.pem ]]; then
  echo "Generating SSL certificates..."
  openssl req -x509 -newkey rsa:2048 \
    -keyout key.pem -out cert.pem -days 365 -nodes \
    -subj '/CN=localhost'
fi

if [[ ! -f .env ]]; then
  echo "Creating .env from .env.example..."
  cp .env.example .env
fi

echo "==> Install complete"
