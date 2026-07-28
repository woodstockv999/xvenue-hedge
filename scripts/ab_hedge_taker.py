#!/usr/bin/env python3
"""perpl ETH ヘッジ脚の taker フォールバック A/B 集計(2026-07-29)。

## 何を決める A/B か
txflow 退役でヘッジ対象が **残差$40 → lead全額$190** になった。旧来の
「taker で埋めても $0.023 だから必ず完成させろ」という根拠が $0.132 に膨らみ、
前提が崩れた。ところが maker 一本槍に戻すと ETH が埋まらず `2leg_eth_abort` が連発し、
出来高が $765→$383 と半減する。

    taker ON : 手数料 +$0.115/cycle・出来高フル・デルタ中立で hold に入る
    taker OFF: 手数料は安い・出来高は半分・**lead $190 を裸で持つ方向賭け**になる

abort の期待値は手数料でなく価格側に出る([[pair-hedge-loss-anatomy-2026-07-22]]
「損失の 71% が価格・そのうち 71% が abort 由来」)。**手数料と価格の綱引きなので
机上では決まらない** → 周回パリティで振って実測する。

## 判定指標は「効率値」(join offset の A/B とは違う)
join offset のときは主判定を **taker 率(二項)** にした。両腕とも同じ完走サイクルを
生み、違いは手数料だけだったので、分散最小の推定量が率そのものだったため。

今回は違う。**腕によってサイクルの中身(出来高)が変わる**ので、率も手数料/cycle も
「1サイクルあたり」で比べると意味を成さない。効率値 = Σvol ÷ |Σnet| がそのまま
目的関数なので、これを直接比べる。

## 集計規約
台帳の `hedge_taker_ab` 列を**持つ行だけ**を使う。A/B 導入前の行や、A/B 打ち切り後の
固定運用の行を混ぜると片腕に寄って静かに偏る([[pair-hedge-ab-null-and-broken-baseline]])。

★ net の符号に注意。効率値は損失側でしか定義されないので、Σnet ≥ 0 の腕は
  「この窓では黒字 = 効率値は無限大」と表示して数値比較から外す。
"""
import json
import math
import statistics as st
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
CYCLES = APP / "data" / "cycles.jsonl"


# ★証拠金天井にぶつかっていた行を落とす閾値(2026-07-29)。
#   A/B 開始直後は lead $190 で、perpl 2脚の必要証拠金 2×190/3 = $126.7 が equity $127.4 を
#   実質超えており、**taker 腕の発注が全部 sr=44 で拒否されていた**。つまりその区間の
#   「taker ON」は maker のみと同じ動作しかしておらず、混ぜると腕の差が薄まる
#   ([[xvenue-margin-ceiling-silent-2026-07-29]])。`_margin_cap` 適用後は lead ≈ $143。
#   ★落とした件数は必ず表示する。黙って捨てると「全部見た」に見える。
_MARGIN_CAP_LEAD_MAX = 160.0


def load(hours: float | None) -> tuple[list[dict], int]:
    rows = []
    for line in CYCLES.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("dry_run"):
            continue
        if "hedge_taker_ab" not in r:      # A/B 割り当てのある行だけ
            continue
        rows.append(r)
    n_all = len(rows)
    rows = [r for r in rows
            if float(r.get("lead_notional_usd") or 0) <= _MARGIN_CAP_LEAD_MAX]
    dropped = n_all - len(rows)
    if hours and rows:
        newest = max(r.get("ts", 0) for r in rows)
        rows = [r for r in rows if r.get("ts", 0) >= newest - hours * 3600]
    return rows, dropped


def arm_stats(rows: list[dict]) -> dict:
    vol = sum(r.get("volume_usd", 0.0) for r in rows)
    net = sum(r.get("net_usd", 0.0) for r in rows)
    fee = sum(r.get("fees_usd", 0.0) for r in rows)
    n = len(rows)
    aborts = sum(1 for r in rows if "abort" in str(r.get("mode", "")))
    nets = [r.get("net_usd", 0.0) for r in rows]
    sd = st.stdev(nets) if n > 1 else 0.0
    return {
        "n": n, "vol": vol, "net": net, "fee": fee,
        "abort_rate": aborts / n if n else 0.0,
        "vol_per_cycle": vol / n if n else 0.0,
        "net_per_cycle": net / n if n else 0.0,
        "fee_per_cycle": fee / n if n else 0.0,
        "price_per_cycle": (net + fee) / n if n else 0.0,
        "eff": (vol / -net) if net < 0 else None,
        "sd_net": sd,
        "se_net": sd / math.sqrt(n) if n > 1 else 0.0,
    }


def fmt_eff(s: dict) -> str:
    return f"{s['eff']:,.0f}" if s["eff"] else "黒字(=∞)"


def main() -> None:
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else None
    rows, dropped = load(hours)
    if dropped:
        print(f"※ 証拠金天井にぶつかっていた {dropped} 行を除外(lead > "
              f"${_MARGIN_CAP_LEAD_MAX:.0f} = taker 腕の発注が拒否されていた区間)")
    if not rows:
        print("A/B 行が無い(config の perpl_hedge_taker_fallback_ab を確認)")
        return
    arms = {}
    for r in rows:
        arms.setdefault(bool(r["hedge_taker_ab"]), []).append(r)

    print(f"# perpl ETH taker フォールバック A/B  (n={len(rows)}"
          + (f" / 直近 {hours}h)" if hours else ")"))
    print()
    print("| 腕 | n | abort率 | vol/cycle | fee/cycle | 価格/cycle | net/cycle | 効率値 |")
    print("|---|---|---|---|---|---|---|---|")
    stats = {}
    for arm in (False, True):
        rs = arms.get(arm)
        if not rs:
            continue
        s = stats[arm] = arm_stats(rs)
        label = "taker ON" if arm else "maker のみ"
        print(f"| {label} | {s['n']} | {s['abort_rate']*100:.0f}% | "
              f"${s['vol_per_cycle']:.0f} | ${s['fee_per_cycle']:.4f} | "
              f"${s['price_per_cycle']:+.4f} | ${s['net_per_cycle']:+.4f} | {fmt_eff(s)} |")
    print()

    if len(stats) < 2:
        print("片腕しか行が無い。両腕たまるまで判定しない。")
        return

    a, b = stats[False], stats[True]
    # 効率値の差は net の差から来るが、net は価格ノイズ支配。必要 n を出しておく。
    d = b["net_per_cycle"] - a["net_per_cycle"]
    pooled = math.sqrt((a["sd_net"] ** 2 + b["sd_net"] ** 2) / 2) or 1e-9
    need = (2 * (1.96 + 0.84) ** 2 * pooled ** 2 / (d ** 2)) if abs(d) > 1e-12 else float("inf")
    print(f"net/cycle 差 = ${d:+.4f} (taker ON − maker のみ)")
    print(f"pooled sd = ${pooled:.4f} → 有意(80%検出力)に必要な片腕 n = "
          + (f"{need:,.0f}" if need < 1e6 else "実質不能"))
    print()
    # ★有意性ではなく損得の非対称で決める([[feedback-measure-the-right-unit-2026-07-28]])
    if a["eff"] and b["eff"]:
        win = "taker ON" if b["eff"] > a["eff"] else "maker のみ"
        print(f"暫定優位: **{win}** (効率 {max(a['eff'], b['eff']):,.0f} vs "
              f"{min(a['eff'], b['eff']):,.0f})")
    print("★有意になるのを待つより、継続費用と誤りの費用の非対称で決めること。"
          "片腕 n≥40 で効率差が 15% 以上つけば採用してよい。")


if __name__ == "__main__":
    main()
