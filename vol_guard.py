"""ボラ・テールの kill-switch(2026-07-29 案A)。

## なぜ「分位」ではなく「倍率」なのか
1脚farm(hedge_leg_enabled:false)に残る唯一の構造リスクは **裸秒の価格損**。
その損益分岐は絶対値で決まっている:

    abort が taker に並ぶ分岐点 4.75bps  vs  実測の裸秒価格損 -0.835〜-1.381bps

= 平常時で **約5.7倍**の余裕。裸秒の価格損はおおむねボラに比例するので、
「今のボラが平常の何倍か」がそのまま**余裕の食い潰し率**になる。

分位(p99等)は「珍しさ」しか測らない。静穏相場では平常の1.5倍でも p99 に入りうるし、
荒れた週には 4倍でも p99 に届かない。**測るべきは珍しさではなく倍率**。

## 方向シグナルではない
レンジ判定・方向予測は作らない(方向は 0/144・0/18・0/9 で確定ネガ)。
これは**片側の停止スイッチ**だけで、サイズもスプレッドも hold も動かさない。
ボラは我々の調査で唯一「全対照を通過した」量(アクティブOI→荒れる)である一方、
その方向成分は 0/18 だった。だから**大きさにだけ賭ける**使い方に限定する。

## fail-open(重要)
candle 取得失敗・履歴不足では **止めない**。データ断で farm が黙って止まるのは
「動いて見えて止まっている」= 証拠金天井(2026-07-29)と同じ最悪の失敗形。
止める判断は必ずログに残し、status.json からも見えるようにする。

## ヒステリシス
enter_ratio > exit_ratio を**構築時に強制**する。到達不能な閾値でデッドロックした
前例(MTMハルト)があるため、等号・逆転は SystemExit で殺す。
"""
from __future__ import annotations

import math
import statistics
import time
from typing import Optional

# perpl candles は 1回のGETで最大1024本。
_MAX_BARS = 1024

# 裸秒価格損の実測回帰係数(2026-07-30・完走1,245サイクル)。_evaluate の注記を参照。
# 再較正したら**必ず日付と n を書き換える**。ここが古いと閾値の意味が静かにずれる。
_SLOPE_BPS = 0.958
_INTERCEPT_BPS = 0.231
_BREAKEVEN_BPS = 4.75          # abort が taker に並ぶ点。倍率換算で 4.72x


class VolGuard:
    """perpl の公開 candle から realized vol を測り、平常比が閾値を超えたら新規を止める。

    価格スケールには依存しない(対数リターンの比しか見ない)ので、perpl の
    price_decimals や内部表現が変わっても壊れない。
    """

    def __init__(self, client, market_id: int, cfg: dict, logger=None):
        self.client = client
        self.market_id = int(market_id)
        self.log = logger or (lambda _m: None)

        c = dict(cfg or {})
        self.enabled = bool(c.get("enabled", False))       # false = shadow(判定だけ・止めない)
        self.bar_sec = int(c.get("bar_sec", 300))
        self.rv_window = int(c.get("rv_window", 12))        # 12本 x 300s = 直近1時間
        self.enter_ratio = float(c.get("enter_ratio", 4.0))
        self.exit_ratio = float(c.get("exit_ratio", 3.0))
        self.min_bars = int(c.get("min_bars", 288))         # 1日分未満の履歴では判定しない
        self.refresh_sec = float(c.get("refresh_sec", 300))  # bar と同周期。これ以上細かく引く意味はない

        # ★到達不能なヒステリシスを構築時に殺す(デッドロックの実例あり)
        if not (self.enter_ratio > self.exit_ratio > 0):
            raise SystemExit(
                f"vol_guard: enter_ratio({self.enter_ratio}) > exit_ratio({self.exit_ratio}) > 0 "
                f"でなければヒステリシスが到達不能になる")
        if self.rv_window < 2:
            raise SystemExit(f"vol_guard: rv_window={self.rv_window} は2以上が必要")

        self._active = False          # True = 現在ハルト中(ヒステリシスの状態)
        self._last_fetch = 0.0
        self._snap: dict = {"ok": False, "reason": "未評価"}

    # ------------------------------------------------------------------ 計測
    def _fetch_closes(self) -> list[float]:
        to_ms = int(time.time() * 1000)
        from_ms = to_ms - _MAX_BARS * self.bar_sec * 1000
        bars = self.client.get_candles(self.market_id, self.bar_sec, from_ms, to_ms)
        out = []
        for b in bars:
            try:
                px = float(b["c"])
            except (KeyError, TypeError, ValueError):
                continue
            if px > 0:
                out.append(px)
        return out

    @staticmethod
    def _rolling_rv(closes: list[float], win: int) -> tuple[Optional[float], list[float]]:
        """(直近rv, 窓内の全rv系列) を返す。rv = 対数リターンの標本標準偏差。"""
        rets = []
        for a, b in zip(closes, closes[1:]):
            try:
                rets.append(math.log(b / a))
            except ValueError:
                return None, []
        if len(rets) < win:
            return None, []
        series = []
        for i in range(win, len(rets) + 1):
            series.append(statistics.pstdev(rets[i - win:i]))
        return series[-1], series

    def _evaluate(self) -> dict:
        """1回ぶんの判定。例外は握り潰して fail-open(ok=False)にする。"""
        try:
            closes = self._fetch_closes()
        except Exception as e:
            return {"ok": False, "reason": f"candle取得失敗: {type(e).__name__}: {str(e)[:80]}"}
        if len(closes) < self.min_bars:
            return {"ok": False, "reason": f"履歴不足 {len(closes)}本 < min_bars {self.min_bars}"}

        rv_now, series = self._rolling_rv(closes, self.rv_window)
        if rv_now is None or len(series) < self.min_bars // 2:
            return {"ok": False, "reason": f"rv系列不足 {len(series)}点"}

        # 平常値は**中央値**。平均だと直近のテールが基準そのものを押し上げて自己無効化する。
        rv_base = statistics.median(series)
        if rv_base <= 0:
            return {"ok": False, "reason": "rv_base=0(値動き無し)"}

        ratio = rv_now / rv_base
        # 想定裸秒価格損。★係数は**実測回帰**(2026-07-30):
        #   cycles.jsonl の完走1,245本の価格PnL(名目あたりbps)を、その時刻の rv 倍率へ回帰
        #     価格PnL(bps) = -0.958 x 倍率 - 0.231   (corr -0.231)
        #   倍率1.0 で -1.188bps = memory の実測レンジ(-0.835〜-1.381)のほぼ中央に落ちる。
        #   分岐点 -4.75bps を解くと **倍率 4.72 で損益分岐** → enter_ratio 4.0 はその直下。
        #   ★memory の「余裕5.7倍」は切片を無視した過大評価だった(真値は約4.7倍)。
        est_bps = _SLOPE_BPS * ratio + _INTERCEPT_BPS
        return {
            "ok": True,
            "bars": len(closes),
            "rv_now": rv_now,
            "rv_base": rv_base,
            "ratio": ratio,
            "est_naked_bps": est_bps,
            "breakeven_bps": _BREAKEVEN_BPS,
            "headroom_x": _BREAKEVEN_BPS / est_bps if est_bps > 0 else None,
            # 損益分岐に達する倍率。enter_ratio がこれを超えていたら閾値が無意味になる。
            "breakeven_ratio": (_BREAKEVEN_BPS - _INTERCEPT_BPS) / _SLOPE_BPS,
        }

    # ------------------------------------------------------------------ 公開API
    def snapshot(self) -> dict:
        """status.json へそのまま載せる用。副作用なし(最後の評価結果)。"""
        s = dict(self._snap)
        s["enabled"] = self.enabled
        s["active"] = self._active
        s["enter_ratio"] = self.enter_ratio
        s["exit_ratio"] = self.exit_ratio
        return s

    def halt_reason(self) -> Optional[str]:
        """新規サイクルを止めるべきなら理由文字列、そうでなければ None。

        enabled=False の shadow では**常に None を返す**が、判定とログは実行する。
        """
        now = time.monotonic()
        if now - self._last_fetch >= self.refresh_sec or not self._snap.get("ok"):
            self._last_fetch = now
            prev_ok = self._snap.get("ok")
            self._snap = self._evaluate()
            if not self._snap.get("ok"):
                # fail-open。ただし**状態遷移のときだけ**ログ(毎回吐くとログが埋まる)
                if prev_ok is not False:
                    self.log(f"vol_guard: 判定不能につき通過(fail-open) — {self._snap.get('reason')}")
                # 判定できない間はヒステリシス状態を維持したまま止めない
                return None

            s = self._snap
            was = self._active
            if self._active:
                if s["ratio"] < self.exit_ratio:
                    self._active = False
            else:
                if s["ratio"] > self.enter_ratio:
                    self._active = True
            if was != self._active:
                self.log(
                    f"vol_guard: {'発火' if self._active else '解除'} "
                    f"ratio={s['ratio']:.2f}x (rv_now={s['rv_now']:.6f} / 平常={s['rv_base']:.6f}) "
                    f"想定裸秒損={s['est_naked_bps']:.2f}bps 分岐点4.75bps "
                    f"余裕={s['headroom_x']:.1f}x"
                    + ("" if self.enabled else " ※shadow(停止しない)"))

        if not self._active:
            return None
        if not self.enabled:
            return None      # shadow: 判定は出すが止めない
        s = self._snap
        return (f"ボラ・テール ratio={s['ratio']:.2f}x > {self.enter_ratio}x "
                f"(想定裸秒損{s['est_naked_bps']:.2f}bps vs 分岐点4.75bps)")
