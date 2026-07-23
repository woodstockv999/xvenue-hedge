#!/usr/bin/env python3
"""xvenue-hedge: txflow BTC × perpl BTC クロス会場デルタ中立farm。

txflowでBTCをmaker farm(将来pt)しつつ、perplで逆BTCをヘッジ=デルタ中立・両会場で二重farm。
効率値(出来高÷損失)は data/cycles.jsonl の独立台帳で計測する([[txflow-perpl-xhedge-farm]])。

## 構成
- txflow脚(farm): TxflowClient(txflow-bot/src、eth_account署名)。BTC=coin1、maker。
- perpl脚(hedge): PerplExecutor/PerplMarketData(hlbot-sandbox/src、Ed25519署名)。BTC=market1。
  maker(0.9bps)で置き、leg_timeout内に刺さらなければtakerフォールバック(6.9bps)で裸窓を閉じる。
- 保守版クライアントを import 再利用(コピーしない=分岐を作らない)。venvは依存が揃った
  hlbot-sandbox/.venv を使う(ecosystem.config.js の interpreter)。

## 安全
- dry_run(既定): 発注せず両会場の板から約定を模擬。1サイクル完走を確認してから実弾化。
- loss_budget: 台帳の累積net損失が超えたら自己ハルト。
- perplは共有IPのCF 1015レート制限あり=pair_hedgeと帯域競合。poll控えめ。

## perpl:BTCは同一口座(2780、hlbotのperpl口座)だがhlbotの掃除対象外(perpl:BTCはself.symbols外
   →sweep_orphan_stopsはskip、sweep_orphan_positionsは'ambiguous'=触らず通知のみ)。安全。
"""
import importlib.util
import json
import os
import sys
import time
import types
from pathlib import Path

import yaml
from dotenv import load_dotenv

APP = Path(__file__).resolve().parent


def _load_as_package(pkg_name: str, pkg_dir: Path, submodules: list[str]) -> dict:
    """pkg_dir 内の .py を pkg_name パッケージ配下として import(ライブコード再利用・コピーしない)。
    txflow-bot と hlbot-sandbox が両方 `src` パッケージ名を使い衝突するため、txflow側を別名で読む。
    submodules は依存順(先に読んだものが後続の相対importで解決される)。"""
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(pkg_dir)]
    sys.modules[pkg_name] = pkg
    out = {}
    for name in submodules:
        spec = importlib.util.spec_from_file_location(f"{pkg_name}.{name}", pkg_dir / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = pkg_name
        sys.modules[f"{pkg_name}.{name}"] = mod
        spec.loader.exec_module(mod)
        out[name] = mod
    return out


# --- txflow: 別名パッケージでライブ import(txflow_client の `from . import txflow_signing` を解決) ---
_tx = _load_as_package("txflowpkg", Path.home() / "apps" / "txflow-bot" / "src",
                       ["txflow_signing", "txflow_client"])
TxflowClient = _tx["txflow_client"].TxflowClient

# --- perpl: hlbot-sandbox の `src` パッケージをそのまま使う(perpl_exchange は from src import 依存) ---
sys.path.insert(0, str(Path.home() / "hlbot-sandbox"))
from src import perpl_client as _pc            # noqa: E402
from src import perpl_exchange as _pe          # noqa: E402
PerplClient = _pc.PerplClient
PerplMarketData = _pe.PerplMarketData
PerplExecutor = _pe.PerplExecutor

CYCLES_PATH = APP / "data" / "cycles.jsonl"
STATUS_PATH = APP / "data" / "status.json"

PERPL_BTC_MCFG = {"market_id": 1, "price_decimals": 1, "size_decimals": 5, "leverage": 3}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class XVenueHedge:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.dry_run = cfg.get("dry_run", True)
        self.notional = float(cfg["notional_usd"])
        self.fees = cfg["fees"]
        self.symbol = cfg.get("symbol", "BTC")  # txflow place/cancel/l2book は symbol名("BTC")を取る(内部でcoin_index)
        self.coin = "1"  # info l2Book 用の BTC coin_index。perpl BTC=market1

        # --- txflow(BTC価格・発注) ---
        load_dotenv(Path.home() / "apps" / "txflow-bot" / ".env")
        tx_key = os.environ.get("TXFLOW_AGENT_PRIVATE_KEY") if not self.dry_run else None
        self.tx = TxflowClient(agent_private_key=tx_key,
                               main_address=os.environ.get("TXFLOW_MAIN_ADDRESS"))

        # --- perpl(BTC価格・ヘッジ発注) ---
        load_dotenv(Path.home() / "apps" / "hyperliquid-bot" / ".env")
        self.pp_client = PerplClient(os.environ["PERPL_API_KEY"], os.environ["PERPL_API_KEY_SECRET"])
        self.pp_market = PerplMarketData("perpl:BTC", self.pp_client, PERPL_BTC_MCFG)
        self.pp_exec = PerplExecutor(self.pp_market, self.pp_client)

        # --- 台帳(累積) ---
        self.cum_volume = 0.0
        self.cum_net = 0.0
        self.cum_fees = 0.0
        self.cycles = 0
        self.halted = False
        self._load_ledger()

    def _load_ledger(self) -> None:
        if not CYCLES_PATH.exists():
            return
        for line in CYCLES_PATH.read_text().splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            self.cum_volume += d.get("volume_usd", 0.0)
            self.cum_net += d.get("net_usd", 0.0)
            self.cum_fees += d.get("fees_usd", 0.0)
            self.cycles += 1

    # ---- 板取得 ----
    def _txflow_bbo(self):
        b = self.tx.info("l2Book", coin=self.coin)
        bids, asks = b["levels"][0], b["levels"][1]
        return float(bids[0]["px"]), float(asks[0]["px"])

    def _perpl_bbo(self):
        return self.pp_market.get_best_bid_ask()  # (bid, ask)

    # ================= 実弾: txflow脚(farm) =================
    def _tx_place_maker(self, is_buy: bool, price: float, size: float, reduce_only: bool):
        """post_only指値を置き、cloid経由でoidを回収して返す(None=載らず)。
        txflowのplace応答はoidを即返さない前例があるためcloid→openOrdersポーリングで同定
        (hedge_bot _place_and_identify を踏襲)。"""
        import uuid
        cloid = str(uuid.uuid4())
        try:
            resp = self.tx.place_limit_order(self.symbol, is_buy, price, size,
                                             reduce_only=reduce_only,
                                             tif=self.tx.TIF_POST_ONLY, cloid=cloid)
        except Exception as e:
            log(f"txflow place例外: {repr(e)[:90]}"); return None
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            log(f"txflow place拒否: {str(resp)[:120]}"); return None
        for _ in range(6):  # cloid一致でoid回収(300ms×6)
            time.sleep(0.3)
            try:
                for o in (self.tx.get_open_orders() or []):
                    if o.get("cloid") == cloid:
                        return int(o["oid"])
            except Exception:
                pass
        # openOrdersに出ない=即約定した可能性→fillsをcloidで確認(false-negative回避)
        return {"cloid": cloid}

    def _tx_fill(self, ident, target: float):
        """oid(int)またはcloid({cloid})に紐づくuserFillsを合算し{px,sz,fee}(達成時)/None。"""
        try:
            fills = self.tx.get_user_fills() or []
        except Exception:
            return None
        if isinstance(ident, dict):
            matched = [f for f in fills if f.get("cloid") == ident["cloid"]]
        else:
            matched = [f for f in fills if f.get("oid") == ident]
        if not matched:
            return None
        sz = sum(float(f["sz"]) for f in matched)
        if sz < target * 0.999:
            return None
        px = sum(float(f["px"]) * float(f["sz"]) for f in matched) / sz
        fee = sum(float(f.get("fee", 0)) for f in matched)
        return {"px": px, "sz": sz, "fee": fee}

    def _tx_position(self) -> float:
        """txflow BTC 符号付き建玉(取引所の正)。失敗は例外(fail-closed用に呼び側で扱う)。"""
        chs = self.tx.get_clearinghouse_state(self.tx.main_address)
        for p in chs.get("assetPositions", []):
            pos = p["position"]
            if str(pos["coin"]).split("-")[0].upper() == "BTC":
                return float(pos.get("szi", 0))
        return 0.0

    def _tx_unwind(self, opened_is_buy: bool, size: float) -> None:
        """txflow脚を建玉(=正)からtaker IOCで即クローズ(ヘッジ失敗時の裸回避)。"""
        try:
            pos = self._tx_position()
        except Exception as e:
            log(f"⚠️ unwind: txflow建玉取得失敗={repr(e)[:50]} 手動確認要"); return
        if abs(pos) < 1e-8:
            return
        t_bid, t_ask = self._txflow_bbo()
        px = t_bid if pos > 0 else t_ask  # long→sell@bid / short→buy@ask (marketable)
        try:
            self.tx.place_limit_order(self.symbol, pos < 0, px, abs(pos),
                                      reduce_only=True, tif=self.tx.TIF_IOC)
            log(f"unwind: txflow {abs(pos)} を taker close")
        except Exception as e:
            log(f"⚠️ unwind失敗={repr(e)[:50]} 手動フラット化要")

    # ---- 1サイクル ----
    def run_cycle(self, dir_buy: bool) -> dict:
        """dir_buy=True: txflow BUY(long) / perpl SELL(short)。deltaは相殺。"""
        if not self.dry_run:
            return self._run_cycle_live(dir_buy)

        t_bid, t_ask = self._txflow_bbo()
        p_bid, p_ask = self._perpl_bbo()
        mid = (t_bid + t_ask) / 2
        size = self.notional / mid
        # --- dry_run: open(maker touch) ---
        if dir_buy:
            tx_open, pp_open = t_bid, p_ask          # txflow BUY@bid / perpl SELL@ask
        else:
            tx_open, pp_open = t_ask, p_bid          # txflow SELL@ask / perpl BUY@bid
        time.sleep(min(self.cfg.get("hold_seconds", 120), 3) if self.dry_run else 0)  # dry_runは短縮

        # --- close(hold後、再取得して maker touch で畳む) ---
        t_bid2, t_ask2 = self._txflow_bbo()
        p_bid2, p_ask2 = self._perpl_bbo()
        if dir_buy:
            tx_close, pp_close = t_ask2, p_bid2      # txflow SELL@ask / perpl BUY@bid
            tx_pnl = (tx_close - tx_open) * size
            pp_pnl = (pp_open - pp_close) * size
        else:
            tx_close, pp_close = t_bid2, p_ask2
            tx_pnl = (tx_open - tx_close) * size
            pp_pnl = (pp_close - pp_open) * size

        # 手数料(notional基準、txflow maker往復 + perpl maker open + close無料)
        tx_fee = self.notional * (self.fees["txflow_maker_bps"] * 2) / 1e4
        pp_fee = self.notional * (self.fees["perpl_maker_bps"] + self.fees["perpl_close_bps"]) / 1e4
        fees = tx_fee + pp_fee
        volume = self.notional * 2 * 2  # 両会場 × open+close
        net = tx_pnl + pp_pnl - fees
        return {
            "ts": round(time.time(), 3), "dir_buy": dir_buy, "dry_run": True,
            "size": size, "notional_usd": self.notional,
            "txflow": {"open": tx_open, "close": tx_close, "pnl": round(tx_pnl, 5)},
            "perpl": {"open": pp_open, "close": pp_close, "pnl": round(pp_pnl, 5)},
            "fees_usd": round(fees, 6), "volume_usd": round(volume, 4), "net_usd": round(net, 6),
        }

    # ================= 実弾サイクル =================
    def _run_cycle_live(self, dir_buy: bool) -> dict:
        """txflow脚(farm)を先行約定させ、即perplでヘッジ(maker→leg_timeoutでtaker)。
        裸窓=txflow約定→perplヘッジ約定。close時はperpl reduce-only。fail-closed。"""
        lt = float(self.cfg["leg_timeout_seconds"])
        t_bid, t_ask = self._txflow_bbo()
        mid = (t_bid + t_ask) / 2
        size = round(self.notional / mid, 5)  # BTC size_decimals=5

        # --- OPEN: txflow lead(farm) ---
        tx_is_buy = dir_buy
        tx_px = t_bid if dir_buy else t_ask
        ident = self._tx_place_maker(tx_is_buy, tx_px, size, reduce_only=False)
        if ident is None:
            return {"skip": "txflow_place_failed"}
        tx_fill, dl = None, time.time() + lt
        while time.time() < dl:
            tx_fill = self._tx_fill(ident, size)
            if tx_fill:
                break
            time.sleep(1)
        if not tx_fill:
            if isinstance(ident, int):
                try:
                    self.tx.cancel_order(self.symbol, ident)
                except Exception:
                    pass
            time.sleep(1)
            pos = self._tx_position()  # 建玉=正(fillポーリングのfalse-negative対策)
            if abs(pos) < size * 0.5:
                return {"skip": "txflow_no_fill"}  # 真に未約定=安全に見送り
            tx_fill = {"px": mid, "sz": abs(pos), "fee": self.notional * self.fees["txflow_maker_bps"] / 1e4}
        # txflow約定=裸BTC。即perplでヘッジ(反対側)。
        p_bid, p_ask = self._perpl_bbo()
        pp_is_buy = not dir_buy
        pp_px = p_bid if pp_is_buy else p_ask     # maker: buyはbid/sellはaskにjoin
        since_ms = int(time.time() * 1000)
        pp_oid = self.pp_exec.place_maker_resting(pp_is_buy, size, pp_px, reduce_only=False)
        pp_filled, pp_taker = False, False
        if pp_oid:
            dl = time.time() + lt
            while time.time() < dl:
                if self.pp_exec.poll_maker_fill(pp_oid, since_ms) or abs(self.pp_exec.get_position_szi()) >= size * 0.999:
                    pp_filled = True
                    break
                time.sleep(1)
        if not pp_filled:  # takerフォールバックで裸窓を閉じる(6.9bps)
            if pp_oid:
                try:
                    self.pp_exec.cancel_order(pp_oid)
                except Exception:
                    pass
            pp_taker_px = None
            try:
                r = self.pp_exec.place_order(pp_is_buy, size, "hedge_taker_fallback", reduce_only=False)
                if isinstance(r, dict) and r.get("price"):
                    pp_taker_px = float(r["price"])
            except Exception as e:
                log(f"⚠️ perplヘッジtaker失敗({repr(e)[:50]})")
            # ヘッジ成立を建玉で確認(=正)。不成立ならtxflow脚を即unwindして裸を解消(fail-closed)
            if abs(self.pp_exec.get_position_szi()) < size * 0.5:
                log("⚠️ perplヘッジ不成立→txflow脚をunwind(裸回避)")
                self._tx_unwind(tx_is_buy, size)
                return {"skip": "hedge_failed_unwound"}
            pp_taker = True
        pp_open_px = (pp_px if pp_filled
                      else (pp_taker_px if pp_taker_px is not None else (p_bid if pp_is_buy else p_ask)))

        # --- HOLD ---
        time.sleep(float(self.cfg["hold_seconds"]))

        # --- CLOSE: txflow reduce(maker→taker) + perpl reduce-only ---
        t_bid2, t_ask2 = self._txflow_bbo()
        tx_close_buy = not tx_is_buy
        tx_cpx = t_bid2 if tx_close_buy else t_ask2
        cid = self._tx_place_maker(tx_close_buy, tx_cpx, size, reduce_only=True)
        tx_cfill, dl = None, time.time() + lt
        while cid is not None and time.time() < dl:
            tx_cfill = self._tx_fill(cid, size)
            if tx_cfill:
                break
            time.sleep(1)
        if not tx_cfill:  # taker強制close
            if isinstance(cid, int):
                try:
                    self.tx.cancel_order(self.symbol, cid)
                except Exception:
                    pass
            pos = self._tx_position()
            if abs(pos) > 1e-8:
                px = t_bid2 if pos > 0 else t_ask2  # marketable IOC
                self.tx.place_limit_order(self.symbol, pos < 0, px, abs(pos),
                                          reduce_only=True, tif=self.tx.TIF_IOC)
                tx_cfill = {"px": px, "sz": abs(pos), "fee": self.notional * self.fees["txflow_taker_bps"] / 1e4}
            else:
                tx_cfill = {"px": tx_cpx, "sz": size, "fee": 0.0}
        # perpl reduce-only close(reduce≈無料・確実)
        pp_szi = self.pp_exec.get_position_szi()
        pp_close_px = (p_bid + p_ask) / 2
        if abs(pp_szi) > 1e-8:
            r = self.pp_exec.place_order(pp_szi < 0, abs(pp_szi), "close", reduce_only=True)
            if isinstance(r, dict) and r.get("price"):
                pp_close_px = float(r["price"])

        # --- 損益(実約定価格ベース) ---
        tx_o, tx_c = tx_fill["px"], tx_cfill["px"]
        # long(buy)は close-open、short(sell)は open-close で利益
        tx_pnl = (tx_c - tx_o) * size if tx_is_buy else (tx_o - tx_c) * size
        pp_pnl = (pp_close_px - pp_open_px) * size if pp_is_buy else (pp_open_px - pp_close_px) * size
        tx_fee = tx_fill.get("fee", 0) + tx_cfill.get("fee", 0)
        pp_fee = self.notional * ((self.fees["perpl_taker_bps"] if pp_taker else self.fees["perpl_maker_bps"])
                                  + self.fees["perpl_close_bps"]) / 1e4
        fees = tx_fee + pp_fee
        volume = self.notional * 2 * 2
        net = tx_pnl + pp_pnl - fees
        return {
            "ts": round(time.time(), 3), "dir_buy": dir_buy, "dry_run": False,
            "size": size, "notional_usd": self.notional,
            "txflow": {"open": tx_o, "close": tx_c, "pnl": round(tx_pnl, 5)},
            "perpl": {"open": round(pp_open_px, 1), "close": round(pp_close_px, 1),
                      "pnl": round(pp_pnl, 5), "taker_hedge": pp_taker},
            "fees_usd": round(fees, 6), "volume_usd": round(volume, 4), "net_usd": round(net, 6),
        }

    def _record(self, rec: dict) -> None:
        with CYCLES_PATH.open("a") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self.cum_volume += rec["volume_usd"]
        self.cum_net += rec["net_usd"]
        self.cum_fees += rec["fees_usd"]
        self.cycles += 1
        eff = (self.cum_volume / abs(self.cum_net)) if self.cum_net < 0 else None
        status = {
            "updated_ts": int(time.time()), "dry_run": self.dry_run, "halted": self.halted,
            "cycles": self.cycles, "cum_volume_usd": round(self.cum_volume, 2),
            "cum_fees_usd": round(self.cum_fees, 4), "cum_net_usd": round(self.cum_net, 4),
            "efficiency": round(eff, 1) if eff else None,
        }
        STATUS_PATH.write_text(json.dumps(status, indent=2))
        log(f"cycle#{self.cycles} vol=${rec['volume_usd']:.0f} net=${rec['net_usd']:+.4f} "
            f"cum_net=${self.cum_net:+.3f} eff={status['efficiency']}")

    def loop(self) -> None:
        log(f"xvenue-hedge 起動: dry_run={self.dry_run} notional=${self.notional} "
            f"(累積 cycles={self.cycles} net=${self.cum_net:+.3f})")
        dir_buy = True
        while self.cfg.get("enabled", True):
            if self.cum_net <= -float(self.cfg["loss_budget_usd"]):
                if not self.halted:
                    self.halted = True
                    log(f"⚠️ loss_budget${self.cfg['loss_budget_usd']}超過(net=${self.cum_net:.3f})=自己ハルト")
                time.sleep(30)
                continue
            try:
                rec = self.run_cycle(dir_buy)
                if rec.get("skip"):
                    log(f"cycle見送り: {rec['skip']}")
                else:
                    self._record(rec)
                    dir_buy = not dir_buy
                    # 初回実弾は1サイクルで停止(canary検証用。確認後にfalseへ)
                    if not self.dry_run and self.cfg.get("canary_once", False):
                        log("canary_once: 1サイクル完了。停止(建玉フラット確認のこと)。")
                        return
            except Exception as e:
                log(f"cycleエラー(継続): {repr(e)[:120]}")
            time.sleep(self.cfg.get("cooldown_seconds", 10))


def main() -> None:
    cfg = yaml.safe_load((APP / "config.yaml").read_text())
    XVenueHedge(cfg).loop()


if __name__ == "__main__":
    main()
