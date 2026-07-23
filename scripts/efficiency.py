#!/usr/bin/env python3
"""効率値(出来高÷損失)を「戦略別」と「会場別」で出す。cron+discord-notifyで定期通知にも使う。

## 戦略別
- pair_hedge (perpl SOL/ETH farm): cyclesを現行銘柄(seq_lead=perpl:SOL)で絞る
  (status.jsonの累計はBTC時代混入のため使わない)。
- xvenue-hedge (txflow×perpl BTC): 実弾cycle(dry_run=false)のみ。

## 会場別(各DEXの全活動を集約)
- perpl  = pair_hedge(両脚perpl) + xvenue perpl脚
- txflow = txflow-bot(ARB/HBAR等の自前ペア) + xvenue txflow脚
- ★xvenueはクロス会場なので、1サイクルのnetを各会場の出来高比で按分する(両脚同notional=50/50)。
  価格PnLは両脚で相殺するため脚別PnLでなく「出来高按分」で会場コストを均す(ユーザー方針2026-07-23)。

効率= 出来高 ÷ |net| (net<0のとき)。net≥0(黒字)は損失ゼロ=効率無限大として出来高/手数料を参考表示。
"""
import json
from pathlib import Path

HOME = Path.home()
PAIR_HEDGE_CYCLES = HOME / "apps" / "hyperliquid-bot" / "shared" / "pair_hedge_cycles.jsonl"
XVENUE_CYCLES = HOME / "apps" / "xvenue-hedge" / "data" / "cycles.jsonl"
TXFLOW_BOT_CYCLES = HOME / "apps" / "txflow-bot" / "data" / "cycles.jsonl"


def _read(path: Path, keep=lambda r: True) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if keep(r):
            out.append(r)
    return out


def _acc(rows, vol_key="volume_usd", net_key="net_usd") -> dict:
    """出来高・netを合算。net_keyは戦略で違う(xvenue/pair_hedge=net_usd, txflow-bot=net_pnl_usd)。"""
    vol = sum(r.get(vol_key, 0) for r in rows)
    net = sum(r.get(net_key, 0) for r in rows)
    fees = sum(r.get("fees_usd", r.get("fee_usd", 0)) for r in rows)
    return {"n": len(rows), "volume": vol, "net": net, "fees": fees}


def _xvenue_by_venue(rows) -> dict:
    """xvenueの各サイクルを会場別に按分。両脚同notionalなので vol=50/50, net=50/50。"""
    perpl = {"volume": 0.0, "net": 0.0, "n": 0}
    txflow = {"volume": 0.0, "net": 0.0, "n": 0}
    for r in rows:
        vol = r.get("volume_usd", 0)
        net = r.get("net_usd", 0)
        # 会場出来高 = 各脚 notional*2(open+close)。両脚同額なので total/2 ずつ。
        pv = tv = vol / 2.0
        share = pv / vol if vol else 0.5
        perpl["volume"] += pv; perpl["net"] += net * share; perpl["n"] += 1
        txflow["volume"] += tv; txflow["net"] += net * (1 - share); txflow["n"] += 1
    return {"perpl": perpl, "txflow": txflow}


def _eff_line(name: str, sub: str, a: dict) -> str:
    if a["n"] == 0 and a["volume"] == 0:
        return f"■ {name} ({sub})\n  データ無し"
    net = a["net"]
    vol = a["volume"]
    if net < 0:
        eff = f"効率 {vol / abs(net):,.0f}"
    else:
        fees = a.get("fees", 0)
        vpf = f"・参考 出来高/手数料={vol / fees:,.0f}" if fees > 0 else ""
        eff = f"効率=損失ゼロ(net+${net:.3f}){vpf}"
    ncyc = f"{a['n']}サイクル " if a.get("n") else ""
    return f"■ {name} ({sub})\n  {ncyc}出来高${vol:,.0f} net${net:+.3f} → {eff}"


def build_report() -> str:
    # --- 戦略別 ---
    ph = _acc(_read(PAIR_HEDGE_CYCLES, keep=lambda r: r.get("seq_lead") == "perpl:SOL"))
    xv_rows = _read(XVENUE_CYCLES, keep=lambda r: r.get("dry_run") is False)
    xv = _acc(xv_rows)
    txb = _acc(_read(TXFLOW_BOT_CYCLES), net_key="net_pnl_usd")

    # --- 会場別(xvenueを按分して合流) ---
    xvv = _xvenue_by_venue(xv_rows)
    perpl_venue = {"n": ph["n"] + xvv["perpl"]["n"],
                   "volume": ph["volume"] + xvv["perpl"]["volume"],
                   "net": ph["net"] + xvv["perpl"]["net"], "fees": ph["fees"]}
    txflow_venue = {"n": txb["n"] + xvv["txflow"]["n"],
                    "volume": txb["volume"] + xvv["txflow"]["volume"],
                    "net": txb["net"] + xvv["txflow"]["net"], "fees": txb["fees"]}

    return (
        "【戦略別】\n"
        + _eff_line("pair_hedge", "perpl SOL/ETH", ph) + "\n"
        + _eff_line("xvenue-hedge", "txflow×perpl BTC", xv) + "\n\n"
        "【会場別】(xvenueは損益を出来高で按分)\n"
        + _eff_line("perpl", "pair_hedge + xvenue perpl脚", perpl_venue) + "\n"
        + _eff_line("txflow", "txflow-bot + xvenue txflow脚", txflow_venue)
    )


if __name__ == "__main__":
    print(build_report())
