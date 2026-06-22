#!/usr/bin/env bash
# EGC Pulse — local run helper (optional).
# Starts the Python backend on :8787 and opens the dashboard in your browser.
# Cross-platform safe: macOS prefers Chrome, Linux/Windows fall back gracefully,
# and if no opener is found it just prints the URL. No secrets are handled here.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-8787}"
URL="http://localhost:${PORT}"

# First run: create demo/.env from the template (never overwrites an existing one).
if [ ! -f "${HERE}/.env" ] && [ -f "${HERE}/.env.example" ]; then
  cp "${HERE}/.env.example" "${HERE}/.env"
  echo "Created demo/.env from .env.example — add any API keys you have, then re-run."
fi

# Open the browser shortly after the server boots (best-effort; never fails the run).
open_browser() {
  sleep 1.5
  case "$(uname -s)" in
    Darwin)  open -a "Google Chrome" "${URL}" 2>/dev/null || open "${URL}" 2>/dev/null || true ;;
    Linux)   xdg-open "${URL}" >/dev/null 2>&1 || true ;;
    MINGW*|MSYS*|CYGWIN*) start "" "${URL}" >/dev/null 2>&1 || true ;;
    *)       echo "Open ${URL} in your browser." ;;
  esac
}
open_browser &

echo "EGC Pulse → ${URL}  (Ctrl+C to stop)"
exec python3 "${HERE}/pulse_demo.py" serve
