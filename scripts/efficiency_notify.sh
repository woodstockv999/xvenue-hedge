#!/usr/bin/env bash
# 2戦略の効率値をDiscordへ定期通知(cron 2hおき)。手動でも python3 scripts/efficiency.py で取れる。
set -uo pipefail
BODY=$(python3 /home/w00dst0ck/apps/xvenue-hedge/scripts/efficiency.py 2>&1)
discord-notify -t "効率値: pair_hedge vs xvenue-hedge" -c blue "$BODY" 2>/dev/null || true
