#!/usr/bin/env bash
set -euo pipefail

# Watchdog Linux installer (DragonOS Pi64, DragonOS x86_64, Ubuntu/Lubuntu, Debian, Raspberry Pi OS)
#
# What it does:
#  - Detects OS base via /etc/os-release and prints it
#  - Installs OS dependencies via apt (PyQt5 + QtMultimedia via apt for compatibility)
#  - Creates a venv that can see apt-installed PyQt5 (--system-site-packages)
#  - Installs pip deps from a Linux requirements file (auto-detect)
#  - Installs a menu-safe desktop launcher that logs failures to ~/watchdog_menu.log
#
# Optional flags:
#   --no-desktop        Don't create the app-menu launcher
#   --desktop-shortcut  Also copy the launcher onto ~/Desktop
#   --with-bladerf      Install bladeRF packages too (best-effort)
#   --keep-venv         Don't delete/recreate .venv if it exists
#
# Notes:
#  - On Ubuntu/Lubuntu, PyQt5 packages are typically in the "universe" repo. This script enables it if missing.
#  - HackRF tools package name varies by distro; we try "hackrf" first, then "hackrf-tools".

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
NO_DESKTOP=0
DESKTOP_SHORTCUT=0
WITH_BLADERF=0
KEEP_VENV=0

for arg in "$@"; do
  case "$arg" in
    --no-desktop) NO_DESKTOP=1 ;;
    --desktop-shortcut) DESKTOP_SHORTCUT=1 ;;
    --with-bladerf) WITH_BLADERF=1 ;;
    --keep-venv) KEEP_VENV=1 ;;
    *) echo "[Watchdog] Unknown option: $arg" >&2; exit 2 ;;
  esac
done

echo "[Watchdog] Repo root: $REPO_ROOT"

# --- OS detection ---
if [[ -f /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  echo "[Watchdog] Detected OS: ${PRETTY_NAME:-unknown} (ID=${ID:-?}, LIKE=${ID_LIKE:-?})"
else
  echo "[Watchdog] WARNING: /etc/os-release not found; assuming Debian/Ubuntu apt system."
  ID=""
  ID_LIKE=""
fi

APT="sudo apt-get"
$APT update >/dev/null || true

apt_install_one() {
  local pkg="$1"
  if dpkg -s "$pkg" >/dev/null 2>&1; then
    return 0
  fi
  if ! $APT install -y "$pkg"; then
    return 1
  fi
}

apt_install_list() {
  local failed=0
  for p in "$@"; do
    if ! apt_install_one "$p"; then
      echo "[Watchdog] WARN: package not available: $p"
      failed=1
    fi
  done
  return "$failed"
}

enable_universe_if_ubuntu() {
  if [[ "${ID:-}" == "ubuntu" ]] || [[ "${ID_LIKE:-}" == *"ubuntu"* ]]; then
    echo "[Watchdog] Ubuntu-like system detected; ensuring 'universe' repo is enabled..."
    apt_install_one software-properties-common || true
    if command -v add-apt-repository >/dev/null 2>&1; then
      sudo add-apt-repository -y universe >/dev/null 2>&1 || true
    fi
    $APT update -y >/dev/null || true
  fi
}

enable_universe_if_ubuntu

echo "[Watchdog] Installing system dependencies (apt)..."
# Core deps:
apt_install_list   git   python3 python3-venv python3-pip   python3-pyqt5 python3-pyqt5.qtmultimedia   python3-pyqtgraph || true

# HackRF tools:
# - If hackrf_sweep already exists (common on DragonOS via /usr/local), don't fight the system packages.
# - Otherwise try distro packages (name varies).
if command -v hackrf_sweep >/dev/null 2>&1; then
  echo "[Watchdog] hackrf_sweep already present ($(command -v hackrf_sweep)); skipping apt HackRF install."
else
  if apt_install_one hackrf; then
    :
  else
    # Some Debian-based distros used to ship hackrf-tools; Ubuntu typically doesn't.
    if apt-cache show hackrf-tools >/dev/null 2>&1; then
      apt_install_one hackrf-tools || true
    fi
    if ! command -v hackrf_sweep >/dev/null 2>&1; then
      echo "[Watchdog] WARN: Could not install HackRF tools (hackrf_sweep not found). HackRF features may not work."
    fi
  fi
fi

# Optional bladeRF packages (best-effort)
if [[ "$WITH_BLADERF" == "1" ]]; then
  echo "[Watchdog] Installing bladeRF packages (best-effort)..."
  apt_install_list bladerf libbladerf2 libbladerf-dev || true
fi

# --- venv ---
cd "$REPO_ROOT"
echo "[Watchdog] Setting up Python venv (.venv)..."
if [[ -d ".venv" && "$KEEP_VENV" == "0" ]]; then
  rm -rf .venv
fi

if [[ ! -d ".venv" ]]; then
  python3 -m venv .venv --system-site-packages
fi

# shellcheck disable=SC1091
source "$REPO_ROOT/.venv/bin/activate"
python -m pip install -U pip

# --- requirements ---
REQ_FILE=""
for f in   "$REPO_ROOT/requirements-linux.txt"   "$REPO_ROOT/requirements-debian.txt"   "$REPO_ROOT/requirements-ubuntu.txt"   "$REPO_ROOT/requirements-pi.txt"   "$REPO_ROOT/requirements.txt"   "$REPO_ROOT/requirements-Debian-Pi OS.txt"
do
  if [[ -f "$f" ]]; then
    REQ_FILE="$f"
    break
  fi
done

if [[ -n "$REQ_FILE" ]]; then
  echo "[Watchdog] Installing pip deps from: $(basename "$REQ_FILE")"
  pip install -r "$REQ_FILE"
else
  echo "[Watchdog] No requirements file found; installing minimum deps (numpy)."
  pip install numpy
fi

# --- launcher scripts ---
mkdir -p "$REPO_ROOT/packaging/linux"

# run_watchdog.sh (portable)
cat > "$REPO_ROOT/packaging/linux/run_watchdog.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"
if [[ -f "$REPO_ROOT/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$REPO_ROOT/.venv/bin/activate"
fi
exec python3 "$REPO_ROOT/main.py"
EOF

# launch_watchdog_menu.sh (menu-safe logging wrapper)
cat > "$REPO_ROOT/packaging/linux/launch_watchdog_menu.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
LOG="$HOME/watchdog_menu.log"
echo "----- $(date) -----" >> "$LOG"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/run_watchdog.sh" >> "$LOG" 2>&1
EOF

chmod +x "$REPO_ROOT/packaging/linux/run_watchdog.sh" "$REPO_ROOT/packaging/linux/launch_watchdog_menu.sh"

# --- desktop entry ---
if [[ "$NO_DESKTOP" == "0" ]]; then
  echo "[Watchdog] Installing app-menu launcher..."
  APP_DIR="$HOME/.local/share/applications"
  mkdir -p "$APP_DIR"

  RUN_SH="$REPO_ROOT/packaging/linux/launch_watchdog_menu.sh"

  cat > "$APP_DIR/watchdog.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Watchdog
Comment=Multi-SDR RF tripwire
Exec=$RUN_SH
Path=$REPO_ROOT
Terminal=false
Categories=Utility;
EOF

  chmod +x "$APP_DIR/watchdog.desktop" || true

  if [[ "$DESKTOP_SHORTCUT" == "1" ]]; then
    mkdir -p "$HOME/Desktop"
    cp "$APP_DIR/watchdog.desktop" "$HOME/Desktop/Watchdog.desktop"
    chmod +x "$HOME/Desktop/Watchdog.desktop" || true
  fi
fi

echo ""
echo "[Watchdog] Install complete."
echo "Run from terminal:"
echo "  cd "$REPO_ROOT""
echo "  source .venv/bin/activate"
echo "  python3 main.py"
if [[ "$NO_DESKTOP" == "0" ]]; then
  echo ""
  echo "Menu launch troubleshooting log:"
  echo "  tail -n 200 ~/watchdog_menu.log"
fi
