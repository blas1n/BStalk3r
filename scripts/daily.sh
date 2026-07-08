#!/usr/bin/env bash
# BStalk3r daily accumulation job (run by launchd, or manually).
#
# Accumulates the day's runner universe and backfills forward outcomes:
#   scan              -> persist the latest session's screened runners (Polygon EOD)
#   track             -> compute forward returns for runners now old enough
#   accumulate-shorts -> record would-be SHORT outcomes + Alpaca shortable status
#                        (forward OOS dataset; the runners aren't shortable, so we
#                         simulate rather than paper-trade)
#
# Location-independent: derives the project dir from this script's path, so it
# works from `main` or any worktree. Logs to logs/daily-YYYYMMDD.log.
set -euo pipefail

# launchd runs with a minimal PATH — make sure uv/homebrew are reachable.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

mkdir -p logs
LOG="logs/daily-$(date +%Y%m%d).log"

{
  echo "=========================================================="
  echo "=== BStalk3r daily :: $(date '+%Y-%m-%d %H:%M:%S %Z') ==="
  echo "project: $PROJECT_DIR"

  if [ ! -f .env ]; then
    echo "ERROR: .env not found in $PROJECT_DIR — aborting." >&2
    exit 1
  fi

  echo "--- scan (accumulate screened runners) ---"
  uv run bstalk3r scan

  echo "--- track (backfill forward outcomes) ---"
  uv run bstalk3r track

  echo "--- accumulate-shorts (forward SHORT dataset, auto-resolve session) ---"
  # Non-fatal: short accumulation must never block scan/track. lag 2 gives free-
  # tier minute aggregates time to land before we target a session.
  uv run bstalk3r accumulate-shorts --lag-days 2 || echo "accumulate-shorts failed (non-fatal)"

  echo "=== done :: $(date '+%H:%M:%S') ==="
} >> "$LOG" 2>&1
