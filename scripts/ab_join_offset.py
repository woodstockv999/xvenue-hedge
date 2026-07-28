#!/usr/bin/env python3
"""txflow follow の join offset A/B 集計(2026-07-28)。

## なぜ A/B が要るか
txflow の open taker 率は **時間帯で 25〜75% 振れる**(07-28 の時間別実測)。
join=+1tick を投入した直後の n=16 は 43.4%→68.8% と逆に出たが、投入時刻が
もともと 75% の時間帯だった。**前後比較ではこの分散に効果が埋もれる**ので、
サイクルごとに腕を交互に振って同一時間帯で並走させる。

## 集計規約
台帳の `join_offset_ab` 列を**持つ行だけ**を使う。A/B 導入前の行を混ぜると
腕の割り当てが無い行が片方に寄って偏る([[pair-hedge-ab-null-and-broken-baseline]])。

## 判定
taker 率だけで決めない。キュー先頭は逆選択も最初に受けるので、
**taker 率 / 価格 bps / 効率** の3つを揃えて見る。
"""
import json
import math
import statistics as st
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path.home() / "apps" / "hyperliquid-bot"))
from src import xvenue_ledger as X  # noqa: E402

LEDGER = APP / "data" / "cycles.jsonl"


def load():
    rows = []
    for line in LEDGER.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("dry_run") is not False or X.is_skip(r):
            continue
        if "join_offset_ab" not in r:      # ★A/B 行だけ
            continue
        rows.append(r)
    rows.sort(key=lambda r: r["ts"])
    return rows


def tx_taker(r):
    lg = (r.get("legs") or {}).get("txflow:BTC")
    if lg is not None:
        return not lg.get("open_maker")
    return bool((r.get("txflow") or {}).get("taker_follow"))


def arm_stats(rows):
    tv = tn = tf = 0.0
    V = N = 0.0
    tk = 0
    for r in rows:
        tk += bool(tx_taker(r))
        for l in X.iter_legs(r):
            V += X.leg_volume(l)
            N += X.leg_net(l)
            if l["venue"] == "txflow":
                tv += X.leg_volume(l)
                tn += X.leg_net(l)
                tf += l.get("fees_usd") or 0
    n = len(rows)
    return {
        "n": n, "taker": tk / n if n else 0,
        "txf_fee_bps": tf / tv * 1e4 if tv else 0,
        "txf_px_bps": (tn + tf) / tv * 1e4 if tv else 0,
        "eff": V / abs(N) if N < 0 else float("inf"),
        "net_per_cycle": N / n if n else 0,
        "net_sd": st.stdev([sum(X.leg_net(l) for l in X.iter_legs(r)) for r in rows]) if n > 1 else 0,
    }


def main():
    rows = load()
    if not rows:
        print("A/B 行がまだ無い(`join_offset_ab` 列)。投入直後なら数サイクル待つこと。")
        return
    arms = {}
    for r in rows:
        arms.setdefault(int(r["join_offset_ab"]), []).append(r)
    span = (rows[-1]["ts"] - rows[0]["ts"]) / 3600
    print(f"A/B 行 n={len(rows)}  期間 {span:.1f}h\n")
    print(f"{'腕':>14} {'n':>4} {'txf taker':>12} {'txf fee':>9} {'txf価格':>9} {'eff':>7} {'net/cyc':>9}")
    for k in sorted(arms):
        s = arm_stats(arms[k])
        se = math.sqrt(s["taker"] * (1 - s["taker"]) / s["n"]) * 100 if s["n"] else 0
        lab = "touch(0)" if k == 0 else f"+{k}tick"
        print(f"{lab:>14} {s['n']:4} {s['taker']*100:7.1f}±{se:4.1f}% "
              f"{s['txf_fee_bps']:8.2f}b {s['txf_px_bps']:8.2f}b {s['eff']:7.0f} {s['net_per_cycle']:+9.4f}")
    if 0 in arms and 1 in arms:
        a, b = arm_stats(arms[0]), arm_stats(arms[1])
        na, nb = a["n"], b["n"]
        p = (a["taker"] * na + b["taker"] * nb) / (na + nb)
        den = math.sqrt(p * (1 - p) * (1 / na + 1 / nb)) if 0 < p < 1 else 0
        z = (a["taker"] - b["taker"]) / den if den else 0
        print(f"\ntaker率  touch {a['taker']*100:.1f}% vs +1tick {b['taker']*100:.1f}%  z={z:+.2f}"
              f"  → {'有意(|z|>1.96)' if abs(z) > 1.96 else 'まだ判定不能'}")
        # net/cycle の差(こちらが本命。taker率が下がっても価格が悪化すれば無意味)
        se_d = math.sqrt(a["net_sd"] ** 2 / na + b["net_sd"] ** 2 / nb) if na > 1 and nb > 1 else 0
        d = b["net_per_cycle"] - a["net_per_cycle"]
        print(f"net/cycle  +1tick − touch = {d:+.4f} ± {se_d:.4f}"
              f"  → {'有意' if se_d and abs(d) > 1.96 * se_d else 'まだ判定不能'}")
        need = 0
        if se_d and abs(d) > 1e-9:
            need = int((1.96 * se_d / abs(d)) ** 2 * (na + nb) / 2)
        if need > max(na, nb):
            print(f"  現在の効果量なら片腕 ~{need} サイクル必要(現在 {min(na, nb)})")


if __name__ == "__main__":
    main()
