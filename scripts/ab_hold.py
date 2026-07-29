#!/usr/bin/env python3
"""hold(保有時間) A/B の集計 = **markout カーブの実測**(2026-07-29)。

## 何を決めたいか
maker で約定するということは、その瞬間 価格が自分の逆へ動いていたということ。
現在の価格コスト(-0.28〜-1.38bps)は **保有6秒時点**の markout でしかない。
その不利が戻るのか続くのかで、最適な保有時間が決まる:

    戻る → 待つほど改善(今の即クローズが最悪のタイミングで閉じている)
    続く → 待つほど悪化(即クローズが正しい)

## 「有利になったら閉じる・60秒で損切り」を直接実装しない理由
停止則の期待値は **60秒時点の分布**から計算できる。分布を先に測れば実弾で試さずに可否が決まる。
ドリフトの無い系列に対する停止則の期待値はゼロなので、勝ち目があるとすれば
「不利が戻る」場合だけ — それはここで測る平均 markout そのもの。

## 判定指標 = 価格 bps（net ではなく）
手数料は両腕で同一(open maker 0.9bps・close 無料 taker)なので、net で見ると
価格の分散だけが乗って必要 n が跳ね上がる。分散最小の推定量は価格 bps
([[feedback-measure-the-right-unit-2026-07-28]])。

★出来高/時も併記する。60秒腕はサイクル間隔が伸びるので、価格が改善しても
  出来高が落ちれば farm としては負ける。両方見て決めること。
★dir_buy 率のバランスも出す。腕が方向と交絡していたら読まない(乱択にしてあるが確認する)。
"""
import json
import math
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
CYCLES = APP / "data" / "cycles.jsonl"


def main() -> None:
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else None
    rows = []
    for line in CYCLES.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("dry_run") or "hold_seconds_ab" not in r:
            continue
        if "perpl:BTC" not in (r.get("legs") or {}):
            continue
        rows.append(r)
    if not rows:
        print("A/B 行が無い(config の hold_seconds_ab を確認)")
        return
    if hours:
        newest = max(r["ts"] for r in rows)
        rows = [r for r in rows if r["ts"] >= newest - hours * 3600]

    arms = defaultdict(list)
    for r in rows:
        arms[float(r["hold_seconds_ab"])].append(r)
    span_h = (max(r["ts"] for r in rows) - min(r["ts"] for r in rows)) / 3600 or 1e-9

    print(f"# hold A/B  n={len(rows)} / 窓 {span_h*60:.0f}分"
          + (f" / 直近 {hours}h" if hours else ""))
    print()
    print("| 腕 | n | 実保有(中央値) | 価格bps ± se | 出来高/h | dir_buy率 |")
    print("|---|---|---|---|---|---|")
    stats = {}
    for arm in sorted(arms):
        v = arms[arm]
        b = [x["legs"]["perpl:BTC"]["pnl"] / x["legs"]["perpl:BTC"]["notional"] * 1e4
             for x in v]
        m = st.mean(b)
        se = (st.stdev(b) / math.sqrt(len(b))) if len(b) > 1 else 0.0
        held = st.median([x.get("hold_actual_s", 0) for x in v])
        vph = sum(x["volume_usd"] for x in v) / span_h * len(arms)
        nb = sum(1 for x in v if x.get("dir_buy"))
        stats[arm] = (m, se, len(b), st.stdev(b) if len(b) > 1 else 0.0)
        print(f"| {arm:.0f}s | {len(v)} | {held:.0f}s | {m:+.3f} ± {se:.3f} | "
              f"${vph:,.0f} | {nb/len(v)*100:.0f}% |")
    print()
    print("★出来高/h は『この腕が単独で走ったときの推定』(窓内の腕別出来高 × 腕数)。")
    if len(stats) >= 2:
        a, b_ = sorted(stats)
        (ma, sa, na, sda), (mb, sb, nb_, sdb) = stats[a], stats[b_]
        d = mb - ma
        sed = math.sqrt(sa ** 2 + sb ** 2)
        print(f"\n価格 bps の差 = {d:+.3f} ± {sed:.3f} bps ({b_:.0f}s − {a:.0f}s)")
        if sed > 0:
            print(f"  → {abs(d)/sed:.1f} σ")
        pooled = math.sqrt((sda ** 2 + sdb ** 2) / 2) or 1e-9
        for target in (0.3, 0.5, 1.0):
            need = 2 * (1.96 + 0.84) ** 2 * pooled ** 2 / target ** 2
            print(f"  {target:.1f}bps の差を検出するのに必要な片腕 n = {need:,.0f}")
        print("\n★『待つと不利が戻る』が正しいなら d > 0(60s のほうが価格が良い)。"
              "\n  d ≤ 0 なら停止則も同時に否定される — 停止則の負け側は timeout 時の分布そのもの。")


if __name__ == "__main__":
    main()
