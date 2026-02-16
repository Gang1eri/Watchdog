#!/usr/bin/env bash
set -euo pipefail

# Watchdog installer for DragonOS / Debian / Raspberry Pi OS
# What it does:
#  - Installs OS dependencies via apt (PyQt5, QtMultimedia, HackRF tools, etc.)
#  - Creates/updates a venv that can see apt-installed PyQt5 (--system-site-packages)
#  - Installs pip deps from the repo's Linux requirements file (auto-detect)
#  - Creates a per-user desktop launcher in ~/.local/share/applications

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

echo "[Watchdog] Repo root: $REPO_ROOT"

echo "[Watchdog] Installing system dependencies (apt)..."
sudo apt update

install_required_candidates() {
  local label="$1"
  shift
  local pkg
  for pkg in "$@"; do
    if apt-cache show "$pkg" >/dev/null 2>&1; then
      echo "[Watchdog] Installing $label: $pkg"
      sudo apt install -y "$pkg"
      return 0
    fi
  done
  echo "[Watchdog] ERROR: No installable package found for: $label"
  echo "[Watchdog] Tried: $*"
  return 1
}

install_optional_candidates() {
  local label="$1"
  shift
  local pkg
  for pkg in "$@"; do
    if apt-cache show "$pkg" >/dev/null 2>&1; then
      echo "[Watchdog] Installing optional package ($label): $pkg"
      sudo apt install -y "$pkg"
      return 0
    fi
  done
  echo "[Watchdog] WARNING: Optional package not found for: $label (tried: $*)"
  return 0
}

# Core app/runtime dependencies.
sudo apt install -y \
  git \
  hackrf \
  python3 python3-venv python3-pip \
  python3-pyqt5 python3-pyqt5.qtmultimedia \
  python3-pyqtgraph \
  libusb-1.0-0

# Soapy runtime + discovery utility (required by backend doctor checks).
install_required_candidates "SoapySDR tools/runtime" \
  soapysdr-tools

# Required Soapy modules for co-equal RTL-SDR + bladeRF backend support.
install_required_candidates "Soapy RTL-SDR module" \
  soapysdr-module-rtlsdr \
  soapysdr-module-rtl-sdr
install_required_candidates "Soapy bladeRF module" \
  soapysdr-module-bladerf

# Optional module for HackRF stream mode via Soapy backend.
install_optional_candidates "Soapy HackRF module (stream mode)" \
  soapysdr-module-hackrf

# Optional vendor utility packages (useful for diagnostics/manual checks).
install_optional_candidates "RTL-SDR tools" rtl-sdr
install_optional_candidates "bladeRF tools" bladerf

cd "$REPO_ROOT"

echo "[Watchdog] Creating/updating venv (.venv) with system site-packages..."
if [[ -d ".venv" ]]; then
  # Recreate every time to avoid confusing PyQt5 import errors.
  rm -rf .venv
fi
python3 -m venv .venv --system-site-packages

# Activate venv
# shellcheck disable=SC1091
source "$REPO_ROOT/.venv/bin/activate"

echo "[Watchdog] Installing Python deps (pip)..."
python -m pip install -U pip

# Auto-detect Linux requirements file (support legacy names too)
REQ_FILE=""
for f in \
  "$REPO_ROOT/requirements-debian.txt" \
  "$REPO_ROOT/requirements-linux.txt" \
  "$REPO_ROOT/requirements-pi.txt" \
  "$REPO_ROOT/requirements-Debian-Pi OS.txt"
do
  if [[ -f "$f" ]]; then
    REQ_FILE="$f"
    break
  fi
done

if [[ -n "$REQ_FILE" ]]; then
  echo "[Watchdog] Using requirements file: $(basename "$REQ_FILE")"
  pip install -r "$REQ_FILE"
else
  echo "[Watchdog] No Linux requirements file found."
  echo "[Watchdog] Installing minimum deps (numpy) so the app can run..."
  pip install numpy
fi

echo "[Watchdog] Ensuring launcher script is executable..."
chmod +x "$REPO_ROOT/packaging/linux/run_watchdog.sh" || true

echo "[Watchdog] Creating desktop launcher (per-user)..."
APP_DIR="$HOME/.local/share/applications"
mkdir -p "$APP_DIR"

# Absolute path to the launcher script (works no matter where the repo is cloned)
RUN_SH="$REPO_ROOT/packaging/linux/run_watchdog.sh"

cat > "$APP_DIR/watchdog.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Watchdog
Comment=Multi-SDR RF tripwire
Exec=$RUN_SH
Terminal=false
Categories=Utility;
EOF

chmod +x "$APP_DIR/watchdog.desktop"

echo ""
echo "[Watchdog] Install complete."
echo "Launch options:"
echo "  1) App menu: search for 'Watchdog'"
echo "  2) Terminal:"
echo "     cd \"$REPO_ROOT\""
echo "     source .venv/bin/activate"
echo "     python3 main.py"
