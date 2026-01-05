#!/usr/bin/env bash
set -euo pipefail
# Backward-compatible entrypoint for older docs.
# Prefer: ./install-linux.sh
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/install-linux.sh" "$@"
