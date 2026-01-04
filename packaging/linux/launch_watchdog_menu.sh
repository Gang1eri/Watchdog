#!/usr/bin/env bash
set -euo pipefail

LOG="$HOME/watchdog_menu.log"
echo "----- $(date) -----" >> "$LOG"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/run_watchdog.sh" >> "$LOG" 2>&1
