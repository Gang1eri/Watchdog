#!/usr/bin/env bash
set -euo pipefail

echo "[Watchdog] Installing system dependencies (apt)..."
sudo apt update
sudo apt install -y \
  git \
  hackrf \
  python3 python3-venv python3-pip \
  python3-pyqt5 python3-pyqt5.qtmultimedia \
  python3-pyqtgraph

echo "[Watchdog] Creating venv with system site-packages..."
python3 -m venv .venv --system-site-packages
source .venv/bin/activate

echo "[Watchdog] Installing pip dependencies..."
python -m pip install -U pip
if [ -f requirements-pi.txt ]; then
  pip install -r requirements-pi.txt
else
  pip install numpy
fi

echo ""
echo "[Watchdog] Done."
echo "Run:"
echo "  source .venv/bin/activate"
echo "  python3 main.py"
