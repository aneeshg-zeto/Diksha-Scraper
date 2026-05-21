@echo off
REM ─────────────────────────────────────────────────────────────────────────────
REM  DIKSHA Scraper — Windows launcher
REM  Usage:
REM    run.bat              — full run (discover + download)
REM    run.bat --login      — browser login (run once before first scrape)
REM    run.bat --dry-run    — discover only, no downloads
REM    run.bat --resume     — skip discovery, resume downloads
REM    run.bat --help       — show all options
REM ─────────────────────────────────────────────────────────────────────────────

setlocal

REM Use venv Python directly (most reliable on Windows)
if exist ".venv\Scripts\python.exe" (
    .venv\Scripts\python.exe -m diksha_scraper %*
) else (
    python -m diksha_scraper %*
)

endlocal
