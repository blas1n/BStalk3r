#!/usr/bin/env bash
# BStalk3r RSI-2 mean-reversion rebalance (run by launchd near the US close).
#
# Runs `bstalk3r mr-trade` once: build the liquid universe's recent closes
# (grouped cache) + today's prices (Alpaca snapshots), reconcile open positions,
# decide SELL/BUY, and route through the execution engine.
#
# SAFETY: honours DRY_RUN. With DRY_RUN=true (default) it only *logs* what it
# would trade — no orders are placed. The founder flips DRY_RUN=false in .env to
# place real paper orders.
#
# Location-independent: derives the project dir from this script's path.
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

mkdir -p logs
LOG="logs/trade-$(date +%Y%m%d).log"

{
  echo "=========================================================="
  echo "=== BStalk3r mr-trade :: $(date '+%Y-%m-%d %H:%M:%S %Z') ==="
  echo "project: $PROJECT_DIR"

  if [ ! -f .env ]; then
    echo "ERROR: .env not found in $PROJECT_DIR — aborting." >&2
    exit 1
  fi

  echo "--- mr-trade (RSI-2 rebalance; DRY_RUN honoured) ---"
  uv run bstalk3r mr-trade

  echo "=== done :: $(date '+%H:%M:%S') ==="
} >> "$LOG" 2>&1
