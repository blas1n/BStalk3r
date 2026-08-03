#!/usr/bin/env bash
# BStalk3r weekly paper-trade report (run by launchd, Mon 09:30 KST).
#
# Measures the live Alpaca paper account (equity curve, fills, round-trips,
# positions/cash) and PUSHES the report to Telegram so it's readable on mobile.
# Also writes a local copy to logs/weekly-report-YYYYMMDD.md.
#
# Needs in .env: ALPACA_API_KEY, ALPACA_SECRET_KEY, TELEGRAM_BOT_TOKEN,
# TELEGRAM_CHAT_ID. Location-independent.
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
mkdir -p logs

[ -f .env ] || { echo "ERROR: .env not found in $PROJECT_DIR" >&2; exit 1; }
# load telegram creds (ALPACA keys are read by the python via pydantic-settings)
set -a; . ./.env; set +a

REPORT_FILE="logs/weekly-report-$(date +%Y%m%d).md"

# 1) measure -> report text (also saved locally)
if ! REPORT="$(uv run python scripts/measure_paper.py 2>report.err)"; then
  REPORT="⚠️ BStalk3r weekly report FAILED: $(tail -3 report.err | tr '\n' ' ')"
fi
rm -f report.err
printf '%s\n' "$REPORT" > "$REPORT_FILE"

# 2) push to Telegram (chunk to Telegram's 4096-char limit)
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  API="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage"
  # split into <=3500-char chunks on line boundaries
  printf '%s\n' "$REPORT" | awk '
    { buf = buf $0 "\n";
      if (length(buf) > 3500) { printf "%s\x1e", buf; buf="" } }
    END { if (length(buf)) printf "%s", buf }' | while IFS= read -r -d $'\x1e' chunk || [ -n "$chunk" ]; do
    curl -s -X POST "$API" \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
      --data-urlencode "text=${chunk}" \
      --data-urlencode "parse_mode=Markdown" \
      --data-urlencode "disable_web_page_preview=true" >/dev/null || true
  done
  echo "pushed report to Telegram chat ${TELEGRAM_CHAT_ID}"
else
  echo "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — report only in $REPORT_FILE" >&2
fi

echo "report: $PROJECT_DIR/$REPORT_FILE"
