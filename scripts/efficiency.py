#!/usr/bin/env python3
"""2戦略の効率値(出来高÷損失)を並べて出す。cron+discord-notifyで定期通知にも使う。

- pair_hedge (perpl SOL/ETH farm): hlbot内。cyclesを現行銘柄(seq_lead=perpl:SOL)で絞る
  (status.jsonの累計はBTC時代混入のため使わない)。
- xvenue-hedge (txflow×perpl BTC): 本repo。実弾cycle(dry_run=false)のみ。

効率= cum_volume ÷ |cum_net| (net<0のとき)。net≥0(黒字)は損失ゼロ=効率無限大として出来高/手数料を参考表示。
"""
import json
from pathlib import Path

HOME = Path.home()
PAIR_HEDGE_CYCLES = HOME / "apps" / "hyperliquid-bot" / "shared" / "pair_hedge_cycles.jsonl"
XVENUE_CYCLES = HOME / "apps" / "xvenue-hedge" / "data" / "cycles.jsonl"


def _agg(rows: list[dict]) -> dict:
    vol = sum(r.get("volume_usd", 0) for r in rows)
    net = sum(r.get("net_usd", 0) for r in rows)
    fees = sum(r.get("fees_usd", 0) for r in rows)
    return {"n": len(rows), "volume": vol, "net": net, "fees": fees,
            "efficiency": (vol / abs(net)) if net < 0 else None,
            "vol_per_fee": (vol / fees) if fees > 0 else None}


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


def _fmt(name: str, venue: str, a: dict) -> str:
    if a["n"] == 0:
        return f"■ {name} ({venue})\n  データ無し"
    if a["efficiency"] is not None:
        eff = f"効率 {a['efficiency']:,.0f}"
    else:
        eff = f"効率=損失ゼロ(net+${a['net']:.3f})・参考 出来高/手数料={a['vol_per_fee']:,.0f}"
    return (f"■ {name} ({venue})\n"
            f"  {a['n']}サイクル 出来高${a['volume']:,.0f} net${a['net']:+.3f} → {eff}")


def build_report() -> str:
    ph = _agg(_read(PAIR_HEDGE_CYCLES, keep=lambda r: r.get("seq_lead") == "perpl:SOL"))
    xv = _agg(_read(XVENUE_CYCLES, keep=lambda r: r.get("dry_run") is False))
    return (_fmt("pair_hedge", "perpl SOL/ETH", ph) + "\n\n"
            + _fmt("xvenue-hedge", "txflow×perpl BTC", xv))


if __name__ == "__main__":
    print(build_report())
