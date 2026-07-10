#!/usr/bin/env bash
# Install (or refresh) the BStalk3r launchd agents for the current user.
#
#   com.bstalk3r.daily  -> scan + track + accumulate-shorts (Tue–Sat 09:00 local)
#   com.bstalk3r.trade  -> RSI-2 mr-trade rebalance (Tue–Sat 04:30 local, before US close)
#
# Renders each plist template with this checkout's absolute paths and loads it.
# Re-run after moving the checkout. Use --uninstall to remove both.
#
# SAFETY: mr-trade honours DRY_RUN (default true = logs only, no orders). Flip
# DRY_RUN=false in .env when ready for real paper orders.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"

DAILY_LABEL="com.bstalk3r.daily"
TRADE_LABEL="com.bstalk3r.trade"

if [ "${1:-}" = "--uninstall" ]; then
  for label in "$DAILY_LABEL" "$TRADE_LABEL"; do
    dest="$AGENTS/${label}.plist"
    launchctl unload "$dest" 2>/dev/null || true
    rm -f "$dest"
    echo "Uninstalled ${label}."
  done
  exit 0
fi

mkdir -p "$AGENTS" "$PROJECT_DIR/logs"

install_agent() {
  local label="$1" script="$2" placeholder="$3"
  local template="$PROJECT_DIR/deploy/launchd/${label}.plist.template"
  local dest="$AGENTS/${label}.plist"
  [ -f "$template" ] || { echo "Template not found: $template" >&2; exit 1; }
  chmod +x "$script"
  sed -e "s#${placeholder}#${script}#g" \
      -e "s#__PROJECT_DIR__#${PROJECT_DIR}#g" \
      "$template" > "$dest"
  plutil -lint "$dest"
  launchctl unload "$dest" 2>/dev/null || true
  launchctl load "$dest"
  echo "Installed ${label} -> ${dest}"
}

install_agent "$DAILY_LABEL" "$PROJECT_DIR/scripts/daily.sh" "__DAILY_SH__"
install_agent "$TRADE_LABEL" "$PROJECT_DIR/scripts/trade.sh" "__TRADE_SH__"

echo
echo "daily: Tue–Sat 09:00 local (scan + track + accumulate-shorts)"
echo "trade: Tue–Sat 04:30 local (KST) = 15:30/14:30 ET, before US close (mr-trade; DRY_RUN honoured)"
echo "Logs:  ${PROJECT_DIR}/logs/"
echo
echo "Run once now to verify:  bash ${PROJECT_DIR}/scripts/trade.sh"
echo "Registered:              launchctl list | grep bstalk3r"
echo "Uninstall both:          $0 --uninstall"
echo
echo "NOTE: mr-trade is DRY_RUN by default (logs only). Set DRY_RUN=false in"
echo "      .env to place real paper orders — and tune the trade hour for DST."
