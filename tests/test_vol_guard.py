"""vol_guard(案A: ボラ・テールの片側kill-switch)のテスト。

★実クライアントは触らない。perpl の共有レートバケットをテストで消費してはいけない
  (pytest が本番バケットを枯渇させた実例あり)。candle は全てフェイクで与える。
"""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vol_guard import VolGuard  # noqa: E402


def _closes(n_base: int = 1000, base_sigma: float = 0.0004,
            tail_mult: float = 1.0, tail_len: int = 12) -> list[float]:
    """交互に ±sigma のリターンを積んだ終値列を作る。

    交互符号なので任意窓の pstdev はちょうど sigma になる = 期待値が手計算できる。
    末尾 tail_len 本だけ sigma を tail_mult 倍にして「直近だけ荒れた」状況を作る。
    """
    rets = []
    for i in range(n_base):
        rets.append(base_sigma if i % 2 == 0 else -base_sigma)
    for i in range(tail_len):
        s = base_sigma * tail_mult
        rets.append(s if i % 2 == 0 else -s)
    px = 60000.0
    out = [px]
    for r in rets:
        px *= math.exp(r)
        out.append(px)
    return out


class FakeClient:
    def __init__(self, closes=None, exc=None):
        self._closes = closes
        self._exc = exc
        self.calls = 0

    def get_candles(self, market_id, resolution_sec, from_ms, to_ms):
        self.calls += 1
        if self._exc:
            raise self._exc
        return [{"t": i, "c": c} for i, c in enumerate(self._closes)]


def _guard(client, **over):
    cfg = {"enabled": True, "refresh_sec": 0, "min_bars": 288}
    cfg.update(over)
    return VolGuard(client, 1, cfg)


# --------------------------------------------------------------- 構築時の不変条件
def test_到達不能なヒステリシスは構築時に殺す():
    for enter, exit_ in [(3.0, 3.0), (2.0, 3.0), (4.0, 0.0)]:
        with pytest.raises(SystemExit):
            VolGuard(FakeClient([]), 1, {"enter_ratio": enter, "exit_ratio": exit_})


def test_rv_windowは2以上を要求する():
    with pytest.raises(SystemExit):
        VolGuard(FakeClient([]), 1, {"rv_window": 1})


# --------------------------------------------------------------- fail-open
def test_candle取得失敗では止めない():
    g = _guard(FakeClient(exc=RuntimeError("boom")))
    assert g.halt_reason() is None
    assert g.snapshot()["ok"] is False


def test_履歴不足では止めない():
    g = _guard(FakeClient(_closes(n_base=50)))
    assert g.halt_reason() is None
    assert "履歴不足" in g.snapshot()["reason"]


def test_値動きゼロでは止めない():
    g = _guard(FakeClient([60000.0] * 500))
    assert g.halt_reason() is None
    assert g.snapshot()["ok"] is False


# --------------------------------------------------------------- 判定そのもの
def test_平常時は発火しない():
    g = _guard(FakeClient(_closes(tail_mult=1.0)))
    assert g.halt_reason() is None
    s = g.snapshot()
    assert s["ok"] and s["ratio"] == pytest.approx(1.0, abs=0.05)


def test_テールで発火し理由に倍率が出る():
    g = _guard(FakeClient(_closes(tail_mult=6.0)))
    reason = g.halt_reason()
    assert reason is not None and "ボラ・テール" in reason
    assert g.snapshot()["ratio"] == pytest.approx(6.0, abs=0.1)


def test_倍率は価格スケールに依存しない():
    """終値を100倍しても対数リターンは不変=同じ判定になる。"""
    base = _closes(tail_mult=6.0)
    a = _guard(FakeClient(base)).halt_reason()
    b = _guard(FakeClient([c * 100 for c in base])).halt_reason()
    assert (a is None) == (b is None)


# --------------------------------------------------------------- ヒステリシス
def test_enterとexitの間では状態を保つ():
    # 6.0倍で発火 → 3.5倍(exit 3.0 と enter 4.0 の間)では**止まったまま**
    g = _guard(FakeClient(_closes(tail_mult=6.0)))
    assert g.halt_reason() is not None
    g.client = FakeClient(_closes(tail_mult=3.5))
    assert g.halt_reason() is not None, "enterとexitの谷間で勝手に再開してはいけない"
    # exit を割れば再開する(=到達可能であることの確認)
    g.client = FakeClient(_closes(tail_mult=1.0))
    assert g.halt_reason() is None


def test_発火前は谷間でも止めない():
    g = _guard(FakeClient(_closes(tail_mult=3.5)))
    assert g.halt_reason() is None, "enterを超えていないので止まってはいけない"


def test_判定不能に落ちてもヒステリシス状態は維持される():
    g = _guard(FakeClient(_closes(tail_mult=6.0)))
    assert g.halt_reason() is not None
    g.client = FakeClient(exc=RuntimeError("candle断"))
    assert g.halt_reason() is None          # fail-open で通す
    g.client = FakeClient(_closes(tail_mult=3.5))
    assert g.halt_reason() is not None      # 谷間なので active は保たれている


# --------------------------------------------------------------- shadow
def test_shadowは判定するが止めない():
    g = _guard(FakeClient(_closes(tail_mult=6.0)), enabled=False)
    assert g.halt_reason() is None
    s = g.snapshot()
    assert s["active"] is True and s["enabled"] is False
    assert s["ratio"] == pytest.approx(6.0, abs=0.1)


def test_snapshotは分岐点と余裕を載せる():
    g = _guard(FakeClient(_closes(tail_mult=1.0)))
    g.halt_reason()
    s = g.snapshot()
    assert s["breakeven_bps"] == 4.75
    # 実測回帰: 倍率1.0 で -1.188bps → 余裕は約4.0倍
    est = 0.958 * 1.0 + 0.231
    assert s["est_naked_bps"] == pytest.approx(est, rel=0.1)
    assert s["headroom_x"] == pytest.approx(4.75 / est, rel=0.1)


def test_enter_ratioは損益分岐倍率より下にある():
    """★閾値が分岐点を超えていたら、発火した時点で既に手遅れ=スイッチの意味が無い。"""
    g = _guard(FakeClient(_closes(tail_mult=1.0)))
    g.halt_reason()
    s = g.snapshot()
    assert s["breakeven_ratio"] == pytest.approx(4.72, abs=0.05)
    assert g.enter_ratio < s["breakeven_ratio"], "enter_ratio が損益分岐倍率以上では遅すぎる"


# --------------------------------------------------------------- 呼び出し回数
def test_refresh_sec内は再取得しない():
    c = FakeClient(_closes())
    g = _guard(c, refresh_sec=9999)
    g.halt_reason()
    g.halt_reason()
    g.halt_reason()
    assert c.calls == 1, "refresh_sec を無視して毎回叩くと429に近づく"
