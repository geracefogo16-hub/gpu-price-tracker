#!/usr/bin/env bash
#
# setup.sh — one-shot installer for the gpus.io GPU price tracker
# ===============================================================
#
# What it does:
#   1. Picks the best available Python (prefers 3.10+, falls back to 3.9).
#   2. Creates a local virtual-env in ./.venv and installs requirements.txt.
#   3. Installs the Playwright Chromium browser (for the scraper's fallback).
#   4. Runs an initial scrape so the dashboard has data immediately.
#   5. Registers a daily macOS cron job (idempotent) that logs to logs/scraper.log.
#
# Usage:
#   ./setup.sh                     # interactive: asks for the scrape time
#   ./setup.sh --time 08:30        # non-interactive scrape time (24h HH:MM)
#   SCRAPE_TIME=22:00 ./setup.sh   # same via env var
#   ./setup.sh --no-cron           # set up env only, skip the cron job
#   ./setup.sh --no-browser        # skip the (large) Chromium download
#   ./setup.sh --help
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
LOG_DIR="$PROJECT_DIR/logs"
SCRAPER_LOG="$LOG_DIR/scraper.log"
CRON_MARKER="# gpus-io-tracker"

SCRAPE_TIME="${SCRAPE_TIME:-}"
# FIXED: local cron is now OPT-IN (--cron or --time), not opt-out.  The daily
# scrape runs in the cloud (GitHub Actions) and commits to the same DB file;
# re-running ./setup.sh with the old default re-registered a local cron job,
# creating dual writes and git-pull conflicts in the launcher.
DO_CRON=0
DO_BROWSER=1
DO_INITIAL_SCRAPE=1

# ---- args -----------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --cron)        DO_CRON=1; shift ;;
    --time)        SCRAPE_TIME="${2:-}"; DO_CRON=1; shift 2 ;;
    --time=*)      SCRAPE_TIME="${1#*=}"; DO_CRON=1; shift ;;
    --no-cron)     DO_CRON=0; shift ;;
    --no-browser)  DO_BROWSER=0; shift ;;
    --no-scrape)   DO_INITIAL_SCRAPE=0; shift ;;
    -h|--help)
      grep '^#' "$0" | grep -v '^#!' | sed 's/^# \{0,1\}//' | head -n 28
      exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; }

# ---- 1. pick a Python interpreter -----------------------------------------
ver_of() { "$1" -c 'import sys;print("%d %d"%sys.version_info[:2])' 2>/dev/null; }

PYTHON=""
PY_FALLBACK=""
for cand in python3.13 python3.12 python3.11 python3.10 python3 python3.9; do
  command -v "$cand" >/dev/null 2>&1 || continue
  read -r MA MI <<<"$(ver_of "$cand")" || continue
  [ -z "${MA:-}" ] && continue
  if [ "$MA" -eq 3 ] && [ "$MI" -ge 10 ]; then PYTHON="$cand"; break; fi
  if [ "$MA" -eq 3 ] && [ "$MI" -eq 9 ] && [ -z "$PY_FALLBACK" ]; then PY_FALLBACK="$cand"; fi
done

if [ -z "$PYTHON" ] && [ -n "$PY_FALLBACK" ]; then
  PYTHON="$PY_FALLBACK"
  warn "Python 3.10+ not found. Falling back to '$PYTHON' ($(ver_of "$PYTHON" | tr ' ' .))."
  warn "The project targets 3.10+ but is written to run on 3.9. To install a newer"
  warn "Python: 'brew install python@3.12'  or download from https://www.python.org/downloads/"
fi
if [ -z "$PYTHON" ]; then
  err "No suitable Python found (need 3.9+). Install Python 3.10+ and re-run."
  exit 1
fi
say "Using Python: $PYTHON ($("$PYTHON" --version 2>&1))"

# ---- 2. virtual-env + dependencies ----------------------------------------
mkdir -p "$LOG_DIR" "$PROJECT_DIR/data"
if [ ! -x "$VENV_DIR/bin/python" ]; then
  say "Creating virtual-env at .venv"
  "$PYTHON" -m venv "$VENV_DIR"
else
  say "Re-using existing virtual-env at .venv"
fi
VENV_PY="$VENV_DIR/bin/python"

say "Upgrading pip and installing dependencies"
"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet -r "$PROJECT_DIR/requirements.txt"
say "Dependencies installed."

# ---- 3. Playwright Chromium (fallback browser) ----------------------------
if [ "$DO_BROWSER" -eq 1 ]; then
  say "Installing Playwright Chromium (used only as a fallback; ~150 MB)"
  if "$VENV_PY" -m playwright install chromium; then
    say "Chromium installed."
  else
    warn "Chromium install failed — the requests-based scraper still works."
    warn "You can retry later with: .venv/bin/python -m playwright install chromium"
  fi
else
  warn "Skipping Chromium download (--no-browser). Playwright fallback will be unavailable."
fi

# ---- 4. initial scrape -----------------------------------------------------
if [ "$DO_INITIAL_SCRAPE" -eq 1 ]; then
  say "Running an initial scrape to populate the database"
  if "$VENV_PY" "$PROJECT_DIR/scraper.py"; then
    say "Initial scrape complete."
  else
    warn "Initial scrape reported errors (see output above). You can re-run: .venv/bin/python scraper.py"
  fi
fi

# ---- 5. cron job -----------------------------------------------------------
if [ "$DO_CRON" -eq 1 ]; then
  # Resolve the scrape time (arg/env > interactive prompt > default 08:00).
  if [ -z "$SCRAPE_TIME" ]; then
    if [ -t 0 ]; then
      read -r -p "Enter daily scrape time (24h HH:MM) [08:00]: " SCRAPE_TIME
    fi
    SCRAPE_TIME="${SCRAPE_TIME:-08:00}"
  fi
  if ! printf '%s' "$SCRAPE_TIME" | grep -Eq '^([01]?[0-9]|2[0-3]):[0-5][0-9]$'; then
    err "Invalid time '$SCRAPE_TIME' (expected 24h HH:MM, e.g. 08:30). Skipping cron setup."
  else
    HOUR=$((10#${SCRAPE_TIME%%:*}))
    MIN=$((10#${SCRAPE_TIME##*:}))
    CRON_CMD="cd '$PROJECT_DIR' && '$VENV_PY' '$PROJECT_DIR/scraper.py' >> '$SCRAPER_LOG' 2>&1"
    CRON_LINE="$MIN $HOUR * * * $CRON_CMD $CRON_MARKER"

    say "Registering daily cron job at $(printf '%02d:%02d' "$HOUR" "$MIN")"
    # Remove any previous entry for this project, then append the new one.
    EXISTING="$(crontab -l 2>/dev/null | grep -v -F "$CRON_MARKER" || true)"
    printf '%s\n%s\n' "$EXISTING" "$CRON_LINE" | sed '/^$/d' | crontab -
    say "Cron job installed. Current entry:"
    crontab -l | grep -F "$CRON_MARKER" | sed 's/^/    /'
    warn "macOS note: if the job never writes to logs/scraper.log, grant 'Full Disk"
    warn "Access' to /usr/sbin/cron in System Settings > Privacy & Security."
  fi
else
  warn "No local cron registered (scheduling runs in the cloud; use --cron to add one)."
fi

# ---- done ------------------------------------------------------------------
cat <<EOF

$(say "Setup complete ✅")

  Start the dashboard:   ./.venv/bin/python dashboard.py
  Then open:             http://localhost:5050

  Run a scrape manually: ./.venv/bin/python scraper.py
  Scrape log:            $SCRAPER_LOG
  Change the schedule:   ./setup.sh --time HH:MM     (or edit:  crontab -e)

EOF
