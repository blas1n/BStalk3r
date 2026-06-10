#!/usr/bin/env bash
# Install (or refresh) the BStalk3r daily launchd agent for the current user.
#
# Renders the plist template with this checkout's absolute paths and loads it.
# Re-run after moving the checkout. Use --uninstall to remove.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.bstalk3r.daily"
DEST="$HOME/Library/LaunchAgents/${LABEL}.plist"

if [ "${1:-}" = "--uninstall" ]; then
  launchctl unload "$DEST" 2>/dev/null || true
  rm -f "$DEST"
  echo "Uninstalled ${LABEL}."
  exit 0
fi

DAILY_SH="$PROJECT_DIR/scripts/daily.sh"
TEMPLATE="$PROJECT_DIR/deploy/launchd/${LABEL}.plist.template"

[ -f "$TEMPLATE" ] || { echo "Template not found: $TEMPLATE" >&2; exit 1; }
chmod +x "$DAILY_SH"
mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/logs"

sed -e "s#__DAILY_SH__#${DAILY_SH}#g" \
    -e "s#__PROJECT_DIR__#${PROJECT_DIR}#g" \
    "$TEMPLATE" > "$DEST"

# Validate before loading.
plutil -lint "$DEST"

launchctl unload "$DEST" 2>/dev/null || true
launchctl load "$DEST"

echo "Installed ${LABEL} -> ${DEST}"
echo "Runs: Tue–Sat 09:00 local. Job: scan + track in ${PROJECT_DIR}"
echo "Logs: ${PROJECT_DIR}/logs/"
echo
echo "Run once now to verify:  bash ${DAILY_SH}"
echo "Check it's registered:   launchctl list | grep bstalk3r"
echo "Uninstall:               $0 --uninstall"
