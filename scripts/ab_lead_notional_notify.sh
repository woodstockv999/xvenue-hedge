#!/usr/bin/env bash
# lead 名目サイズ A/B の集計を、**節目でだけ** Discord へ通知する(2026-07-29)。
# ★セッション内のバックグラウンド待機は何度も外部要因で停止するので最初から cron に載せる。
set -u
APP="$HOME/apps/xvenue-hedge"
PY="$HOME/apps/hyperliquid-bot/.venv/bin/python"
STATE="/tmp/xvenue_ab_lead_notional_verdict"

cd "$APP" || exit 0
OUT="$("$PY" scripts/ab_lead_notional.py 2>&1)" || exit 0

N="$(printf '%s' "$OUT" | sed -n 's/^# lead 名目 A\/B  n=\([0-9]*\).*/\1/p' | head -1)"
[ -z "$N" ] && exit 0
WIN="$(printf '%s' "$OUT" | sed -n 's/^暫定優位: \*\*\(.*\)\*\*.*/\1/p' | head -1)"
# 優位な腕が入れ替わったとき、または n が 20 刻みの節目を越えたときだけ送る
KEY="${WIN}:$((N / 20))"

PREV="$(cat "$STATE" 2>/dev/null || echo '')"
[ "$KEY" = "$PREV" ] && exit 0
printf '%s' "$KEY" > "$STATE"

# ★絶対パス。crontab の PATH 行に依存させない(env -i で再現すると 127 になる)。
printf '%s' "$OUT" | "$HOME/.local/bin/discord-notify" -t "xvenue lead名目 A/B (n=$N)" -c blue
