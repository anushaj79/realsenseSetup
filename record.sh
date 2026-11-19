#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Record everything from a RealSense (D435i / D455 / ...) on macOS.
#
#   ./record.sh                       record until Ctrl+C
#   ./record.sh --preview             ... with a live preview window
#   ./record.sh --seconds 90          stop automatically after 90 s
#   ./record.sh --help                all options
#
# macOS's built-in camera driver grabs the RealSense, and librealsense can only
# take it over as root -- so this asks for your password (sudo). Files are
# handed back to you afterwards so nothing is left owned by root.
# ---------------------------------------------------------------------------
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$ROOT/.venv/bin/python"

[ -x "$VENV_PY" ] || { echo "Run ./setup.sh first."; exit 1; }
mkdir -p "$ROOT/recordings"

if [ "$(id -u)" -ne 0 ]; then
    echo "[record] macOS only lets root open the RealSense -- sudo will ask for your password."
    ME="$(id -un)"
    sudo PYTHONDONTWRITEBYTECODE=1 "$VENV_PY" "$ROOT/record.py" --owner "$ME" "$@"
    RC=$?
    sudo chown -R "$ME" "$ROOT/recordings" 2>/dev/null
    exit $RC
fi
exec "$VENV_PY" "$ROOT/record.py" "$@"
