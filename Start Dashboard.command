#!/bin/bash
#
# Start Dashboard.command — double-click this in Finder to launch the dashboard.
#
# It always uses the project's own virtual-env (./.venv), so it does not matter
# what "python" means in your shell (Anaconda, Homebrew, etc.). It also opens
# your browser automatically. Keep the window open while you use the dashboard;
# press Ctrl+C (or just close the window) to stop it.
#
cd "$(dirname "$0")" || exit 1

VENV_PY="./.venv/bin/python"
PORT=5050

if [ ! -x "$VENV_PY" ]; then
  echo "❌ Virtual-env not found at .venv"
  echo "   Run ./setup.sh first to install everything, then try again."
  echo ""
  read -r -p "Press Return to close this window." _
  exit 1
fi

# Pull the latest cloud-collected data before showing it (non-fatal if offline).
# --ff-only guarantees no merge commits/conflicts; if it can't fast-forward it
# simply skips and shows whatever data is already on this Mac.
if git rev-parse --git-dir >/dev/null 2>&1; then
  echo "🔄 Syncing the latest prices from GitHub…"
  if git pull --ff-only --no-edit origin main >/dev/null 2>&1; then
    echo "   ✓ data up to date"
  else
    echo "   (couldn't sync just now — showing the data already on this Mac)"
  fi
  echo ""
fi

echo "⚡ Starting the GPU Price Tracker dashboard…"
echo "   A browser tab will open at http://localhost:${PORT}"
echo "   Keep this window open. Press Ctrl+C (or close the window) to stop."
echo ""

# Open the browser a moment after the server comes up.
( sleep 2 && open "http://localhost:${PORT}" >/dev/null 2>&1 ) &

exec "$VENV_PY" dashboard.py --port "$PORT"
