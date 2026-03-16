#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "[setup] Project root: $ROOT_DIR"

if [[ ! -d ".venv" ]]; then
  echo "[setup] Creating virtual environment (.venv)..."
  python -m venv .venv
fi

if [[ -f ".venv/Scripts/activate" ]]; then
  # Git Bash on Windows
  # shellcheck disable=SC1091
  source ".venv/Scripts/activate"
elif [[ -f ".venv/bin/activate" ]]; then
  # Linux/macOS
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
else
  echo "[setup] Could not find venv activation script."
  exit 1
fi

echo "[setup] Installing dependencies..."
python -m pip install --upgrade pip
pip install -r requirements.txt

echo "[setup] Installing Playwright Chromium..."
playwright install chromium

if [[ ! -f ".env" && -f ".env.example" ]]; then
  cp ".env.example" ".env"
  echo "[setup] Created .env from .env.example (fill your API keys)."
fi

echo "[setup] Done. Activate venv and run PersonaMeet."
