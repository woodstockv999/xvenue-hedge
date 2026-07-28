#!/usr/bin/env bash
# join offset A/B の結果を、**判定が変わったときだけ** Discord へ通知する(2026-07-28)。
#
# ★セッション内のバックグラウンド待機は何度も外部要因で停止したので、cron に逃がす。
# ★通知は状態遷移時のみ(判定文字列が前回と変わったときだけ送る)。毎回送ると無視されるようになる。
set -u
APP="$HOME/apps/xvenue-hedge"
PY="$HOME/apps/hyperliquid-bot/.venv/bin/python"
STATE="/tmp/xvenue_ab_join_verdict"

cd "$APP" || exit 0
OUT="$("$PY" scripts/ab_join_offset.py 2>&1)" || exit 0

# 判定行(net/cycle のほうが本命)から状態を作る
VERDICT="$(printf '%s' "$OUT" | grep -c '有意$')"
N="$(grep -c 'join_offset_ab' data/cycles.jsonl 2>/dev/null || echo 0)"
# 状態 = 「有意になった判定の数」+「n の桁が変わった」。n は 100 刻みでのみ通知する。
KEY="${VERDICT}:$((N / 100))"

PREV="$(cat "$STATE" 2>/dev/null || echo '')"
[ "$KEY" = "$PREV" ] && exit 0
printf '%s' "$KEY" > "$STATE"

COLOR=blue
[ "$VERDICT" -gt 0 ] && COLOR=green
printf '%s' "$OUT" | discord-notify -t "xvenue join offset A/B (n=$N)" -c "$COLOR"
