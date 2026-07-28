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

## 判定は **手数料** で行う(2026-07-28 訂正)
当初は net/cycle で判定しようとしたが、**価格PnL の sd が手数料効果の 2.4倍**あり、
net で有意にするには 23倍のサンプルが要る(片腕134サイクル)。

そして価格で判定する必要が**そもそも無い**。脚別の「価格 bps」は単独で読めない —
perpl と txflow は同じ BTC の反対売買なので、価格が動けば片方の損が他方の益になる。
実測(n=19): +1tick の腕で txflow 価格PnL -0.4015 に対し perpl +0.2592 で、
**見かけの悪化の 87% をヘッジが吸収**していた。

デルタ中立ペアに「キュー先頭は逆選択を受ける」は当てはまらない。相場の方向は両脚で
相殺され、効くのは **2脚間の基差**だけ。板の先頭に立てば perpl の約定に時間的に近い
ところで埋まるので、基差ドリフトはむしろ減る。払う 1tick(0.0158bps)は節約する
3bps に対して誤差。

→ 主判定 = **taker 率(二項)**。$ 影響は率から決定的に従う(率差 × 3bps × $150)。
   ★「手数料はノイズを含まない」は誤り: 1サイクルの手数料は maker/taker の二値で
     決まるので二項分散をそのまま引き継ぐ(実測 sd 0.0113 で効果量より大きい)。
     分散最小の推定量は**率そのもの**。
   補助 = 手数料/cycle・net/cycle(参考。収束が遅いので単独で否定材料にしない)
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
    fees = [sum(l.get("fees_usd") or 0 for l in X.iter_legs(r)) for r in rows]
    nets = [sum(X.leg_net(l) for l in X.iter_legs(r)) for r in rows]
    return {
        "n": n, "taker": tk / n if n else 0,
        "txf_fee_bps": tf / tv * 1e4 if tv else 0,
        "txf_px_bps": (tn + tf) / tv * 1e4 if tv else 0,
        "eff": V / abs(N) if N < 0 else float("inf"),
        "fee_per_cycle": sum(fees) / n if n else 0,
        "fee_sd": st.stdev(fees) if n > 1 else 0,
        "net_per_cycle": N / n if n else 0,
        "net_sd": st.stdev(nets) if n > 1 else 0,
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
        def report(label, key, sd_key, sign):
            """sign=-1 は「小さいほど良い」(手数料)。必要nも出す。"""
            se_d = math.sqrt(a[sd_key] ** 2 / na + b[sd_key] ** 2 / nb) if na > 1 and nb > 1 else 0
            d = b[key] - a[key]
            sig = bool(se_d) and abs(d) > 1.96 * se_d
            good = "+1tick 有利" if d * sign > 0 else "touch 有利"
            print(f"{label}  +1tick − touch = {d:+.4f} ± {se_d:.4f}"
                  f"  → {'有意: ' + good if sig else 'まだ判定不能'}")
            if not sig and se_d and abs(d) > 1e-9:
                need = int((1.96 * se_d / abs(d)) ** 2 * (na + nb) / 2)
                if need > min(na, nb):
                    print(f"     現在の効果量なら片腕 ~{need} サイクル必要(現在 {min(na, nb)})")

        # ★主判定 = taker 率(二項)。手数料/cycle は maker/taker の二値で決まるので
        #   「手数料はノイズを含まない」は誤り(sd は二項分散をそのまま引き継ぐ)。
        #   **分散最小の推定量は率そのもの**で、$ 影響はそこから決定的に従う。
        dr = a["taker"] - b["taker"]
        FEE_DELTA = 150.0 * 3.0e-4      # txflow taker-maker 差(4.5-1.5bps) × notional
        print(f"\n★taker率  touch {a['taker']*100:.1f}% vs +1tick {b['taker']*100:.1f}%  z={z:+.2f}"
              f"  → {'有意(|z|>1.96)' if abs(z) > 1.96 else 'まだ判定不能'}")
        print(f"   率差 {dr*100:+.1f}pp → 手数料 {dr*FEE_DELTA:+.4f}/cycle "
              f"= {dr*FEE_DELTA*21*24:+.2f}$/日 (21cycle/h 換算)")
        if abs(z) <= 1.96 and abs(dr) > 1e-9:
            pbar = (a["taker"] + b["taker"]) / 2
            need = int(2 * (1.96 + 0.84) ** 2 * pbar * (1 - pbar) / dr ** 2)
            print(f"   この効果量(={abs(dr)*100:.1f}pp)を検出するには片腕 ~{need} サイクル"
                  f"(現在 {min(na, nb)})")
        # ★主判定は手数料。価格PnL は脚間で相殺されるうえ sd が手数料効果の数倍あり、
        #   net で判定しようとすると必要nが二桁増える(docstring 参照)。
        report("  手数料/cycle", "fee_per_cycle", "fee_sd", -1)
        report("  net/cycle  ", "net_per_cycle", "net_sd", -1)
        print("  ※どちらも二項+価格ノイズを引き継ぐので収束が遅い。**主判定は上の taker 率**。")


if __name__ == "__main__":
    main()
