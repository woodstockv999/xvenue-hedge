#!/usr/bin/env bash
# 全Claudeセッション共通の xvenue-hedge 安全再起動ラッパー(2026-07-25新設)。
#
# 背景: 2026-07-25 のセッションで生の `pm2 restart xvenue-hedge` を3回叩き、**3回すべてが
#   サイクル途中に当たって孤児脚を生んだ**。うち1回は perpl 建玉 szi=0.0024(≒$150)が残り、
#   起動時 reconcile が perpl 429 に阻まれて約80秒間フラット化できず裸だった。
#   hyperliquid-bot には同目的の safe_restart.sh があるのに xvenue には無かった、という
#   単純な抜け(memory xvenue-fee-fabrication-and-halt-2026-07-25)。
#
# xvenue のサイクル構造(main.py _run_cycle_live):
#   perpl maker LEAD(最大45s) → txflow FOLLOW(maker試行6s→taker) → HOLD(hold_seconds)
#   → CLOSE(txflow maker最大40s→taker → perpl reduce-only)
#   **OPEN完了後〜CLOSE完了前は両会場に建玉がある**。ここで落とすと片方だけ残りうる。
#   一方 perpl lead 待ち中(まだ約定なし)は建玉ゼロ=最も安全な瞬間。
#
# このラッパーは:
#   1. flock で全セッションの restart を直列化
#   2. **両会場フラットになる瞬間を待つ**(サイクル間の見送り/cooldown。最大 WAIT_MAX 秒)
#   3. pm2 restart
#   4. 起動後の孤児脚/unwind/reconcile未完を検知したら警告
#
# 使い方: 生の `pm2 restart xvenue-hedge` の代わりにこれを使う。
#   ~/apps/xvenue-hedge/scripts/safe_restart.sh
set -euo pipefail

APP=/home/w00dst0ck/apps/xvenue-hedge
LOG=/home/w00dst0ck/.pm2/logs/xvenue-hedge-out.log
LOCK=/tmp/xvenue_safe_restart.lock
WAIT_MAX=${WAIT_MAX:-240}   # フラット待ちの上限秒。hold+close が一巡する程度(既定4分)

# 1. 直列化。
exec 9>"$LOCK"
if ! flock -w 60 9; then
  echo "[safe_restart] 他セッションの再起動ロックを60秒待っても取得できず。中止。" >&2
  exit 1
fi
echo "[safe_restart] restartロック取得。"

# 2. 建玉が無い瞬間を待つ。
#    ★判定は**ログの進行**で行う: cycle#完了 / 見送り / フラット確認 のいずれかが出た直後は
#      両会場フラット。実口座を直接叩く判定は perpl 429 を煽る(=再起動したい状況ほど読めない)
#      ので使わない。逆に「これから発注する」行が出たらまた建玉フェーズに入るので待ち直す。
flat_now() {
  # 直近1行が「サイクルの切れ目」を示していればフラットとみなす。
  tail -n 1 "$LOG" 2>/dev/null | grep -qE "cycle#[0-9]+ vol=|cycle見送り:|両会場フラット確認|自己ハルト|バックオフ"
}
waited=0
if [ -f "$LOG" ]; then
  while [ "$waited" -lt "$WAIT_MAX" ]; do
    if flat_now; then
      echo "[safe_restart] サイクルの切れ目を検知(両会場フラット)。${waited}s 待機。"
      break
    fi
    sleep 3
    waited=$((waited + 3))
  done
  if [ "$waited" -ge "$WAIT_MAX" ]; then
    echo "[safe_restart] ⚠️ ${WAIT_MAX}s 待ってもフラットな瞬間を掴めず。建玉途中の可能性があるまま再起動する" >&2
    echo "[safe_restart]    (起動時 reconcile が両会場を畳むが、perpl 429 中は数十秒かかる)" >&2
  fi
else
  echo "[safe_restart] ⚠️ ログ($LOG)が無い=フラット判定できず。そのまま再起動する" >&2
fi

# 3. 再起動。
mark=$(wc -l < "$LOG" 2>/dev/null || echo 0)   # 起動後ログだけを見るための基準行
pm2 restart xvenue-hedge
sleep 12                                        # startup_reconcile が一巡する余裕

# 4. 起動後チェック。**この再起動で出た行だけ**を見る(前回分を拾ってオオカミ少年になるのを防ぐ)。
post=$(tail -n +$((mark + 1)) "$LOG" 2>/dev/null || true)
if printf '%s' "$post" | grep -qE "をフラット化|unwind:|reconcile未完|残脚検出"; then
  echo "[safe_restart] ⚠️ 起動時に孤児脚を検知(下記)。フラット化を見届けること。" >&2
  printf '%s\n' "$post" | grep -E "をフラット化|unwind:|reconcile未完|残脚検出" | tail -5 >&2
  echo "[safe_restart]    429で畳めていない場合は 'error後reconcile: 両会場フラット確認' が出るまで監視。" >&2
  exit 2
fi
echo "[safe_restart] ✓ 再起動完了(孤児脚なし)。"
