"""台帳シム(`src/xvenue_ledger.py`)の v1/v2 互換テスト。

★このシムが壊れると **perpl 口座ガードから ETH 脚の損益が丸ごと漏れる**。
  3脚化で最も静かに壊れる場所なので、実台帳の実データを fixture にして固定する。
"""
import json
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path.home() / "apps" / "hyperliquid-bot"))
shim = pytest.importorskip("src.xvenue_ledger", reason="hyperliquid-bot が必要")

LEDGER = APP / "data" / "cycles.jsonl"


# --------------------------------------------------------------------------- fixtures
V1_ROW = {
    "ts": 1785106117.4, "symbol": "BTC", "dir_buy": True, "dry_run": False,
    "size": 0.0023, "notional_usd": 150.0,
    "txflow": {"open": 65324.8, "close": 65357.4, "pnl": 0.07498,
               "taker_follow": False, "close_recovered": True},
    "perpl": {"open": 65356.6, "close": 65368.6, "pnl": -0.0276, "taker_hedge": False,
              "size": 0.0023, "notional": 150.3202, "close_recovered": True},
    "fees_usd": 0.058548, "volume_usd": 600.0, "net_usd": -0.011168,
}

V2_ROW_3LEG = {
    "ts": 1785200000.0, "schema": 2, "mode": "3leg", "dry_run": False,
    "symbol": "BTC", "size": 0.0043, "notional_usd": 280.0,
    "legs": {
        "perpl:BTC": {"venue": "perpl", "symbol": "BTC", "role": "lead", "is_buy": False,
                      "size": 0.0043, "open_px": 65000.0, "close_px": 65010.0,
                      "notional": 279.5, "pnl": -0.043, "fees_usd": 0.02516,
                      "open_maker": True, "close_recovered": True},
        "txflow:BTC": {"venue": "txflow", "symbol": "BTC", "role": "follow", "is_buy": True,
                       "size": 0.0023, "open_px": 65001.0, "close_px": 65011.0,
                       "notional": 149.5, "pnl": 0.023, "fees_usd": 0.0673,
                       "open_maker": False, "close_recovered": True},
        "perpl:ETH": {"venue": "perpl", "symbol": "ETH", "role": "hedge", "is_buy": True,
                      "size": 0.067, "open_px": 1957.0, "close_px": 1956.5,
                      "notional": 131.1, "pnl": -0.0335, "fees_usd": 0.0118,
                      "open_maker": True, "close_recovered": True},
    },
    "fees_usd": 0.10426, "volume_usd": 1120.0, "net_usd": -0.15796,
}

SKIP_ROW = {"ts": 1785200001.0, "symbol": "BTC", "dry_run": False,
            "skip_reason": "hedge_failed_unwound", "volume_usd": 0.0,
            "fees_usd": 0.1035, "net_usd": -0.1035}


# --------------------------------------------------------------------------- tests
def test_schema_detection():
    assert shim.schema_version(V1_ROW) == 1
    assert shim.schema_version(V2_ROW_3LEG) == 2


def test_v1_row_yields_two_legs():
    legs = shim.iter_legs(V1_ROW)
    assert len(legs) == 2
    venues = {lg["venue"] for lg in legs}
    assert venues == {"perpl", "txflow"}


def test_v1_leg_fees_split_matches_legacy_formula():
    """v1 は脚別 fee を持たないので推定する。合計が元の fees_usd を超えないこと。"""
    legs = shim.iter_legs(V1_ROW)
    total = sum(lg["fees_usd"] for lg in legs)
    assert total == pytest.approx(V1_ROW["fees_usd"], abs=1e-9)


def test_v2_three_legs_all_returned():
    legs = shim.iter_legs(V2_ROW_3LEG)
    assert len(legs) == 3
    assert {lg["symbol"] for lg in legs} == {"BTC", "ETH"}


def test_perpl_legs_includes_eth():
    """★本シムの存在理由。ETH 脚を落とさないこと。"""
    pl = shim.perpl_legs(V2_ROW_3LEG)
    assert len(pl) == 2, "perpl 脚が2本(BTC lead + ETH hedge)取れていない"
    assert {lg["symbol"] for lg in pl} == {"BTC", "ETH"}


def test_perpl_net_sums_both_legs():
    """perpl 口座の net = BTC 脚 + ETH 脚。片方だけだとガードが過小評価する。"""
    got = sum(shim.leg_net(lg) for lg in shim.perpl_legs(V2_ROW_3LEG))
    want = (-0.043 - 0.02516) + (-0.0335 - 0.0118)
    assert got == pytest.approx(want, abs=1e-9)


def test_v1_perpl_net_excludes_txflow():
    """v1 でも txflow の損益が perpl 側に混ざらないこと(C-3 の主旨)。"""
    pl = shim.perpl_legs(V1_ROW)
    assert len(pl) == 1
    assert pl[0]["pnl"] == pytest.approx(-0.0276)


def test_skip_row_has_no_legs():
    assert shim.iter_legs(SKIP_ROW) == []
    assert shim.perpl_legs(SKIP_ROW) == []
    assert shim.is_skip(SKIP_ROW) is True


def test_leg_volume_uses_actual_fills():
    leg = V2_ROW_3LEG["legs"]["perpl:BTC"]
    want = 65000.0 * 0.0043 + 65010.0 * 0.0043
    assert shim.leg_volume(leg) == pytest.approx(want)


def test_leg_volume_falls_back_to_notional():
    leg = {"venue": "perpl", "symbol": "ETH", "size": 0.0, "notional": 100.0}
    assert shim.leg_volume(leg) == pytest.approx(200.0)


@pytest.mark.skipif(not LEDGER.exists(), reason="本番台帳が無い")
def test_real_ledger_rows_all_parse():
    """★実データ回帰: 本番台帳の全行がシムを通ること(壊れ行で例外を出さない)。"""
    rows = []
    for line in LEDGER.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    assert rows, "台帳が空"
    for r in rows:
        legs = shim.iter_legs(r)
        assert isinstance(legs, list)
        if not shim.is_skip(r) and r.get("dry_run") is False:
            assert legs, f"実弾行から脚が取れない: ts={r.get('ts')}"
            for lg in legs:
                assert lg.get("venue") in ("perpl", "txflow")
                assert isinstance(shim.leg_net(lg), float)


@pytest.mark.skipif(not LEDGER.exists(), reason="本番台帳が無い")
def test_real_ledger_perpl_net_is_smaller_than_total_loss():
    """perpl 脚の net は行全体の net より**必ず小さい損失**(txflow ぶんを含まないので)。"""
    tot_perpl = tot_row = 0.0
    for line in LEDGER.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("dry_run") is not False or shim.is_skip(r):
            continue
        tot_perpl += sum(shim.leg_net(lg) for lg in shim.perpl_legs(r))
        tot_row += float(r.get("net_usd") or 0.0)
    assert tot_row < 0 and tot_perpl < 0
    assert tot_perpl > tot_row, "perpl 脚だけの損失が行全体を超えている(txflow が混入)"
