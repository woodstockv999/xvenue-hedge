#!/usr/bin/env python3
"""効率値(出来高÷損失)を「戦略別」「会場別」「銘柄別」で出す。cron+discord-notifyで定期通知にも使う。

## 戦略別
- pair_hedge (perpl SOL/ETH farm): cyclesを現行銘柄(seq_lead=perpl:SOL)で絞る
  (status.jsonの累計はBTC時代混入のため使わない)。
- xvenue-hedge (txflow×perpl BTC): 実弾cycle(dry_run=false)のみ。

## 会場別(各DEXの全活動を集約)
- perpl  = pair_hedge(両脚perpl) + xvenue perpl脚
- txflow = txflow-bot(ARB/HBAR等の自前ペア) + xvenue txflow脚
- ★xvenueはクロス会場なので、1サイクルのnetを各会場の出来高比で按分する(両脚同notional=50/50)。
  価格PnLは両脚で相殺するため脚別PnLでなく「出来高按分」で会場コストを均す(ユーザー方針2026-07-23)。

## 銘柄別(どの銘柄が効率を沈めているか)
- xvenue = 1サイクル1銘柄なので `symbol` でそのまま分ける(2026-07-25から記録。旧行はBTC後付け)。
- pair_hedge = 1サイクルにSOL脚とETH脚が同居する。**脚別の価格PnLは分離できない**(ヘッジで相殺
  するのが前提の戦略)ので、会場別と同じ「出来高按分」でnetを脚に配る(ユーザー方針2026-07-23)。
  脚の出来高は `fills[symbol]` の px×size×2(open+close)。片脚abortはその脚だけに出来高が立つので
  abortコストは刺さった脚に寄る=「どちらの脚が損を出しているか」を読む指標になる。
- txflow-bot(ARB/HBAR)は2026-07-23退役のため前向き指標から外す(会場別と同じ扱い)。

効率= 出来高 ÷ |net| (net<0のとき)。net≥0(黒字)は損失ゼロ=効率無限大として出来高/手数料を参考表示。
"""
import json
import sys
from pathlib import Path

import yaml

HOME = Path.home()
sys.path.insert(0, str(HOME / "apps" / "hyperliquid-bot"))
from src import xvenue_ledger as _shim  # noqa: E402  台帳 v1/v2 の差を吸収するシム

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
    """出来高・netを合算。net_keyは戦略で違う(xvenue/pair_hedge=net_usd, txflow-bot=net_pnl_usd)。

    ★`skip_reason` を持つ行は**中断サイクルのコスト計上**(2026-07-27 D-5)。損失には数えるが、
    **サイクル数には数えない**(完走ではないため)。
    ★2026-07-29 から中断行も**出来高を持ちうる**(lead が部分約定して畳んだ往復)。
      「中断行は出来高0」を前提にした分岐を書かないこと。"""
    vol = sum(r.get(vol_key, 0) for r in rows)
    net = sum(r.get(net_key, 0) for r in rows)
    fees = sum(r.get("fees_usd", r.get("fee_usd", 0)) for r in rows)
    n = sum(1 for r in rows if not r.get("skip_reason"))
    return {"n": n, "volume": vol, "net": net, "fees": fees}


def _xvenue_by_venue(rows) -> dict:
    """会場別。**脚は src.xvenue_ledger のシムで正規化する**(2026-07-27)。
    v1(2脚)と v2(`legs` 明示・3脚まで)が台帳に混在するため、ここで形の差を吸収する。
    ★旧実装の「fees_usd から perpl 分を引いた残りが txflow 脚」という残差方式は、
      perpl 脚が2本になると破綻する(ETH の手数料が txflow に付け替わる)ので廃止。"""
    out = {"perpl": {"volume": 0.0, "net": 0.0, "n": 0},
           "txflow": {"volume": 0.0, "net": 0.0, "n": 0}}
    for r in rows:
        legs = _shim.iter_legs(r)
        if not legs:
            # 中断コスト行(D-5)は脚に分解できない。★ただし 2026-07-29 から
            # **出来高を持ちうる**(lead の部分約定→畳み = 実際に建てて実際に畳んだ往復)。
            # 中断は必ず perpl lead 脚なので perpl に寄せる。捨てると会場別が戦略別より
            # 小さく出て、差分の出どころが分からなくなる。
            if r.get("skip_reason"):
                out["perpl"]["volume"] += r.get("volume_usd", 0.0)
                out["perpl"]["net"] += r.get("net_usd", 0.0)
            continue
        seen = set()
        for lg in legs:
            v = lg.get("venue")
            if v not in out:
                continue
            out[v]["volume"] += _shim.leg_volume(lg)
            out[v]["net"] += _shim.leg_net(lg)
            if v not in seen:             # 1サイクルは会場ごとに1カウント
                out[v]["n"] += 1
                seen.add(v)
    return out


def _xvenue_by_venue_legacy(rows) -> dict:
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
        if r.get("skip_reason"):
            # ★中断行は 2026-07-29 から **出来高を持ちうる**(lead の部分約定→畳み。
            #   実際に建てて実際に畳んだ往復なので会場から見れば出来高)。旧コメントの
            #   「出来高0だから除外」はもう成立しない。中断は必ず perpl lead 脚なので
            #   perpl に寄せる。ここで捨てると会場別が戦略別より小さく出る。
            perpl["volume"] += r.get("volume_usd", 0.0)
            perpl["net"] += r.get("net_usd", 0.0)
            continue
        n = r.get("notional_usd", 0)
        pp = r.get("perpl", {}) or {}
        tx = r.get("txflow", {}) or {}
        ppn = pp.get("notional", n)   # 脚別実額。無ければ全量約定=notionalとみなす(旧行)
        pp_open_bps = pt if pp.get("taker_hedge") else pm
        pp_fee = ppn * (pp_open_bps + pc) / 1e4
        tx_fee = max(0.0, r.get("fees_usd", 0) - pp_fee)  # 残りがtxflow脚fee
        pp_net = pp.get("pnl", 0) - pp_fee
        tx_net = tx.get("pnl", 0) - tx_fee
        perpl["volume"] += ppn * 2; perpl["net"] += pp_net; perpl["n"] += 1
        txflow["volume"] += n * 2; txflow["net"] += tx_net; txflow["n"] += 1
    return {"perpl": perpl, "txflow": txflow}


def _xvenue_by_symbol(rows) -> dict:
    """銘柄別。**脚ベース**で分ける(2026-07-27)。

    旧実装は行の `symbol` で分けていたが、3脚化すると1サイクルが BTC(lead+txflow)と
    ETH(hedge)の**両方に寄与する**ので行単位では分けられない。脚別に実 net/fee を
    持つようになったため按分も不要(pair_hedge 側の按分より正確)。"""
    out = {}
    for r in rows:
        legs = _shim.iter_legs(r)
        if not legs:               # 中断コスト行(D-5)は完走ではない→銘柄別からは除外
            continue
        seen = set()
        for lg in legs:
            sym = lg.get("symbol") or "BTC"
            a = out.setdefault(sym, {"n": 0, "volume": 0.0, "net": 0.0, "fees": 0.0})
            a["volume"] += _shim.leg_volume(lg)
            a["net"] += _shim.leg_net(lg)
            a["fees"] += float(lg.get("fees_usd") or 0.0)
            if sym not in seen:    # 1サイクルは銘柄ごとに1カウント
                a["n"] += 1
                seen.add(sym)
    return out


def _pair_hedge_by_symbol(rows) -> dict:
    """pair_hedge の net を脚別の出来高比で按分する(モジュールdocstringの「銘柄別」参照)。

    脚の出来高 = fills[symbol] の px×size×2(open+close)。fills が空のサイクル(両脚未約定)は
    出来高ゼロなので配りようがなく、そのnetはどの銘柄にも計上しない — 台帳全体の合計とは
    その分ズレる(戦略別の行が正)。"""
    out = {}
    for r in rows:
        legs = {s: (f.get("px", 0) or 0) * (f.get("size", 0) or 0) * 2
                for s, f in (r.get("fills") or {}).items()}
        total = sum(legs.values())
        if total <= 0:
            continue
        for s, vol in legs.items():
            a = out.setdefault(s, {"n": 0, "volume": 0.0, "net": 0.0, "fees": 0.0})
            share = vol / total
            a["n"] += 1
            a["volume"] += vol
            a["net"] += r.get("net_usd", 0.0) * share
            a["fees"] += r.get("fees_usd", 0.0) * share
    return out


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
    ph_rows = _read(PAIR_HEDGE_CYCLES, keep=lambda r: r.get("seq_lead") == "perpl:SOL")
    xvv = _xvenue_by_venue(xv_rows)
    perpl_venue = {"n": ph["n"] + xvv["perpl"]["n"],
                   "volume": ph["volume"] + xvv["perpl"]["volume"],
                   "net": ph["net"] + xvv["perpl"]["net"], "fees": ph["fees"]}
    txflow_venue = dict(xvv["txflow"], fees=0.0)

    # --- 銘柄別 ---
    by_sym = [(f"{s} (xvenue)", "txflow×perpl", a) for s, a in sorted(_xvenue_by_symbol(xv_rows).items())]
    by_sym += [(f"{s.split(':')[-1]} (pair_hedge)", "出来高按分", a)
               for s, a in sorted(_pair_hedge_by_symbol(ph_rows).items())]

    return (
        "【戦略別】\n"
        + _eff_line("pair_hedge", "perpl SOL/ETH", ph) + "\n"
        + _eff_line("xvenue-hedge", "txflow×perpl BTC", xv) + "\n\n"
        "【会場別】(xvenueは損益を脚別実額で分解=価格PnL+その脚のfee)\n"
        + _eff_line("perpl", "pair_hedge + xvenue perpl脚", perpl_venue) + "\n"
        + _eff_line("txflow", "xvenue txflow脚(txflow-bot退役)", txflow_venue) + "\n\n"
        "【銘柄別】(pair_hedgeは脚別PnLが分離できないので出来高按分)\n"
        + "\n".join(_eff_line(name, sub, a) for name, sub, a in by_sym)
    )


if __name__ == "__main__":
    print(build_report())
