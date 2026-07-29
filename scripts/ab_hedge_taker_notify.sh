#!/usr/bin/env bash
# perpl ETH taker フォールバック A/B の結果を、**節目でだけ** Discord へ通知する(2026-07-29)。
#
# ★セッション内のバックグラウンド待機は何度も外部要因で停止したので cron に逃がす
#   (join offset A/B で同じ結論に至った。scripts/ab_join_offset_notify.sh と同型)。
# ★通知は状態遷移時のみ。毎回送ると無視されるようになる。
set -u
APP="$HOME/apps/xvenue-hedge"
PY="$HOME/apps/hyperliquid-bot/.venv/bin/python"
STATE="/tmp/xvenue_ab_hedge_taker_verdict"

cd "$APP" || exit 0
OUT="$("$PY" scripts/ab_hedge_taker.py 2>&1)" || exit 0

# 有効 n(証拠金天井の区間を除いたもの)。集計スクリプトのヘッダから拾う。
N="$(printf '%s' "$OUT" | sed -n 's/.*A\/B  (n=\([0-9]*\).*/\1/p' | head -1)"
[ -z "$N" ] && exit 0
# 暫定優位がどちらか(片腕しか無いうちは空)
WIN="$(printf '%s' "$OUT" | sed -n 's/^暫定優位: \*\*\(.*\)\*\*.*/\1/p' | head -1)"
# 状態 = 「優位な腕」+「n の 20 刻み」。腕が入れ替わったときと、n が節目を越えたときだけ送る。
KEY="${WIN}:$((N / 20))"

PREV="$(cat "$STATE" 2>/dev/null || echo '')"
[ "$KEY" = "$PREV" ] && exit 0
printf '%s' "$KEY" > "$STATE"

# ★絶対パス。crontab の PATH 行に依存させない(env -i で再現すると 127 になる)。
printf '%s' "$OUT" | "$HOME/.local/bin/discord-notify" -t "xvenue ETH taker A/B (n=$N)" -c blue
