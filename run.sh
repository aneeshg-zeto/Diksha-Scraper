#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  DIKSHA Scraper — Linux/macOS launcher
#  Usage:
#    ./run.sh              — full run (discover + download)
#    ./run.sh --login      — browser login (run once before first scrape)
#    ./run.sh --dry-run    — discover only, no downloads
#    ./run.sh --resume     — skip discovery, resume downloads
#    ./run.sh --help       — show all options
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# Activate virtual environment if present
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
elif [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
fi

python -m diksha_scraper "$@"
