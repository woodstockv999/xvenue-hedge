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

import yaml

HOME = Path.home()
PAIR_HEDGE_CYCLES = HOME / "apps" / "hyperliquid-bot" / "shared" / "pair_hedge_cycles.jsonl"
XVENUE_CYCLES = HOME / "apps" / "xvenue-hedge" / "data" / "cycles.jsonl"
XVENUE_CONFIG = HOME / "apps" / "xvenue-hedge" / "config.yaml"
TXFLOW_BOT_CYCLES = HOME / "apps" / "txflow-bot" / "data" / "cycles.jsonl"

_FEES = (yaml.safe_load(XVENUE_CONFIG.read_text()) or {}).get("fees", {}) if XVENUE_CONFIG.exists() else {}


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
    """xvenueの各サイクルを会場別に【脚別実額】で分解(2026-07-24修正、旧50/50按分を廃止)。
    - perpl脚net = perpl価格PnL - perpl脚fee(taker_hedgeなら6.9・通常maker0.9 + close無料)
    - txflow脚net = txflow価格PnL - txflow脚fee(=記録fees_usd - perpl脚fee)
    - 会場出来高 = 各脚 notional*2(open+close)
    価格PnLは両脚でほぼ相殺するが、脚別feeが非対称(txflow taker中心 vs perpl maker中心)なので
    50/50按分は実態とズレる。実測(2026-07-24)ではtxflow脚が損失の~71%を負担。"""
    pm = _FEES.get("perpl_maker_bps", 0.9)
    pt = _FEES.get("perpl_taker_bps", 6.9)
    pc = _FEES.get("perpl_close_bps", 0.0)
    perpl = {"volume": 0.0, "net": 0.0, "n": 0}
    txflow = {"volume": 0.0, "net": 0.0, "n": 0}
    for r in rows:
        n = r.get("notional_usd", 0)
        pp = r.get("perpl", {}) or {}
        tx = r.get("txflow", {}) or {}
        pp_open_bps = pt if pp.get("taker_hedge") else pm
        pp_fee = n * (pp_open_bps + pc) / 1e4
        tx_fee = max(0.0, r.get("fees_usd", 0) - pp_fee)  # 残りがtxflow脚fee
        pp_net = pp.get("pnl", 0) - pp_fee
        tx_net = tx.get("pnl", 0) - tx_fee
        perpl["volume"] += n * 2; perpl["net"] += pp_net; perpl["n"] += 1
        txflow["volume"] += n * 2; txflow["net"] += tx_net; txflow["n"] += 1
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

    # --- 会場別(xvenueを按分して合流) ---
    # ★txflow-bot(ARB/HBAR)は2026-07-23退役。txflow会場は稼働中のxvenue txflow脚のみ集計する
    #   (退役した過去のARB/HBAR損失は前向き指標から外す。履歴は data/cycles.jsonl に残存)。
    xvv = _xvenue_by_venue(xv_rows)
    perpl_venue = {"n": ph["n"] + xvv["perpl"]["n"],
                   "volume": ph["volume"] + xvv["perpl"]["volume"],
                   "net": ph["net"] + xvv["perpl"]["net"], "fees": ph["fees"]}
    txflow_venue = dict(xvv["txflow"], fees=0.0)

    return (
        "【戦略別】\n"
        + _eff_line("pair_hedge", "perpl SOL/ETH", ph) + "\n"
        + _eff_line("xvenue-hedge", "txflow×perpl BTC", xv) + "\n\n"
        "【会場別】(xvenueは損益を脚別実額で分解=価格PnL+その脚のfee)\n"
        + _eff_line("perpl", "pair_hedge + xvenue perpl脚", perpl_venue) + "\n"
        + _eff_line("txflow", "xvenue txflow脚(txflow-bot退役)", txflow_venue)
    )


if __name__ == "__main__":
    print(build_report())
