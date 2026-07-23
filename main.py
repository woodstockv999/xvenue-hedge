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
        self.dirty = False  # cycle例外後の未フラット疑い。両会場flat確認まで新規サイクルを止める
        self._pp_bbo_cache = None                       # (monotonic時刻, (bid,ask)) 429緩和のBBO短TTL
        self._pp_bbo_ttl = float(cfg.get("perpl_bbo_ttl_seconds", 2.0))
        self._rl_backoff = 0.0                          # perpl 429時のエスカレート待機(clean cycleで0へ)
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

    def _perpl_bbo(self, force_fresh: bool = False):
        """perpl BBO。get_best_bid_askはキャッシュ無しで毎回REST get_context(CF保護)を叩く=429主因。
        短TTLキャッシュで requote/open/close の連続読みを1回のRESTに畳む(BTC BBOは数秒で不変)。
        force_fresh=True で必ず取り直す(requoteのtouch移動判定など鮮度が要る所)。"""
        now = time.monotonic()
        if not force_fresh and self._pp_bbo_cache is not None:
            ts, val = self._pp_bbo_cache
            if now - ts < self._pp_bbo_ttl:
                return val
        val = self.pp_market.get_best_bid_ask()  # (bid, ask)
        self._pp_bbo_cache = (now, val)
        return val

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
    def _perpl_maker_lead(self, is_buy: bool, size: float):
        """改善①②: perpl maker を先行させ requote しながら perpl_lead_timeout まで刺しにいく。
        戻り(filled, fill_px)。未約定は(False,None)。建玉=正でfill確認(false-negative対策)。
        改善③: fillはpoll_maker_fill主・get_position_sziはrequote/timeout時のみ=perpl call削減。"""
        plt = float(self.cfg["perpl_lead_timeout_seconds"])
        rq = float(self.cfg["requote_interval_seconds"])
        poll = float(self.cfg["poll_interval_seconds"])
        since_ms = int(time.time() * 1000)
        deadline = time.time() + plt
        oid, px, last_place = None, None, 0.0
        while time.time() < deadline:
            if oid is None:
                p_bid, p_ask = self._perpl_bbo()
                px = p_bid if is_buy else p_ask       # maker: buyはbid/sellはaskにjoin
                oid = self.pp_exec.place_maker_resting(is_buy, size, px, reduce_only=False)
                last_place = time.time()
                if oid is None:
                    time.sleep(poll)
                    continue
            if self.pp_exec.poll_maker_fill(oid, since_ms):
                return True, px
            if time.time() - last_place >= rq:       # requote: touchが動いてたら置き直す
                if abs(self.pp_exec.get_position_szi()) >= size * 0.999:  # requote前に建玉=正で確認
                    return True, px
                p_bid, p_ask = self._perpl_bbo()
                new_px = p_bid if is_buy else p_ask
                if new_px != px:
                    try:
                        self.pp_exec.cancel_order(oid)
                    except Exception:
                        pass
                    oid = None
            time.sleep(poll)
        if oid:
            try:
                self.pp_exec.cancel_order(oid)
            except Exception:
                pass
        time.sleep(1)
        if abs(self.pp_exec.get_position_szi()) >= size * 0.5:
            return True, px                          # 建玉あり=約定してた(poll false-negative)
        return False, None

    def _perpl_unwind(self, was_buy: bool, size: float) -> None:
        """perpl脚(was_buy方向で建った)をreduce-onlyで反対に即クローズ(ヘッジ失敗時の裸回避)。
        ★get_position_sziは429でfail-open→0になり安全チェックが崩れるため使わない。
        reduce-onlyは「建玉方向にしか約定しない=flatならno-op/reject」なので、szi読めなくても
        安全に畳みにいける(fail-closed)。"""
        try:
            self.pp_exec.place_order(not was_buy, size, "unwind", reduce_only=True)
            log(f"unwind: perpl reduce-only close(was_buy={was_buy} size={size})")
        except Exception as e:
            log(f"⚠️ perpl unwind失敗={repr(e)[:50]} 手動フラット化要")

    def _startup_reconcile(self) -> None:
        """起動時に両会場のBTCをフラット化(mid-cycle再起動での建玉残存/2倍化を防ぐ)。dry_runは無処理。"""
        if self.dry_run:
            return
        # perpl BTC: 生の建玉(fail-openしない)を読んで方向付きで畳む
        try:
            pos = self.pp_exec._fetch_position()
            if pos is not None:
                sz = _pe.scaled_to_size(int(pos["s"]), self.pp_market.size_decimals)
                if sz > 1e-8:
                    self._perpl_unwind(pos.get("sd") == 1, sz)  # sd==1(long)→sellで畳む
        except Exception as e:
            log(f"⚠️ startup_reconcile perpl建玉確認失敗={repr(e)[:50]}")
        try:  # txflow BTC 注文取消
            for o in (self.tx.get_open_orders(self.tx.main_address) or []):
                if str(o.get("coin", "")).split("-")[0].upper() == self.symbol:
                    self.tx.cancel_order(self.symbol, o.get("oid"))
        except Exception:
            pass
        try:  # txflow BTC 建玉フラット
            pos = self._tx_position()
            if abs(pos) > 1e-8:
                t_bid, t_ask = self._txflow_bbo()
                px = t_bid if pos > 0 else t_ask
                self.tx.place_limit_order(self.symbol, pos < 0, px, abs(pos),
                                          reduce_only=True, tif=self.tx.TIF_IOC)
                log(f"startup_reconcile: txflow {abs(pos)} をフラット化")
        except Exception as e:
            log(f"⚠️ startup_reconcile txflow失敗={repr(e)[:50]}")

    def _venues_flat(self) -> bool:
        """両会場BTCがフラットか(fail-closed: 読めなければFalse=フラット未確認)。"""
        try:
            if self.pp_exec._fetch_position() is not None:
                return False
            if abs(self._tx_position()) > 1e-8:
                return False
            return True
        except Exception:
            return False

    def _reconcile_dirty(self) -> bool:
        """cycle例外後の掃除。両会場をflatten(_startup_reconcileを再利用)し、
        両会場flatを確認できたらTrue。perpl 429等で読めない/畳めない間はFalseを返し、
        呼び側は新規サイクルを止めて次ループで再試行する(裸脚の上に建てない)。"""
        self._startup_reconcile()  # reduce-only中心=idempotent。flatならno-op
        return self._venues_flat()

    def _run_cycle_live(self, dir_buy: bool) -> dict:
        """改善①②③(2026-07-23): perpl maker を先行(patient+requote)、txflow を追従(maker→taker)。
        perpl maker率↑・追従taker落ちしても安いtxflow(4.5)<perpl(6.9)。close時perpl reduce-only。fail-closed。
        (旧: txflow先行→perpl追従はperpl taker落ち76%)。"""
        lt = float(self.cfg["leg_timeout_seconds"])
        poll = float(self.cfg["poll_interval_seconds"])
        t_bid, t_ask = self._txflow_bbo()
        size = round(self.notional / ((t_bid + t_ask) / 2), 5)  # BTC size_decimals=5

        # === OPEN: perpl maker LEAD(patient+requote) ===
        perpl_is_buy = not dir_buy
        pp_filled, pp_open_px = self._perpl_maker_lead(perpl_is_buy, size)
        if not pp_filled:
            return {"skip": "perpl_lead_no_fill"}   # 建玉なし=安全に見送り

        # perpl約定=裸perpl。txflow FOLLOWS(反対=dir_buy)で【即takerヘッジ】(2026-07-23方針)。
        # 旧: post_only maker待ち→timeoutでtaker。makerは0.03bps極薄板で刺さりにくく、待つ間
        # perpl脚が裸のまま(最大leg_timeout)=危険+完走率48%。txflow taker4.5bpsは安く1sで確実にヘッジ。
        tx_is_buy = dir_buy
        t_bid, t_ask = self._txflow_bbo()
        tx_px = t_ask if tx_is_buy else t_bid       # marketable IOC(buy@ask/sell@bid)
        tx_taker = True
        try:
            self.tx.place_limit_order(self.symbol, tx_is_buy, tx_px, size,
                                      reduce_only=False, tif=self.tx.TIF_IOC)
        except Exception as e:
            log(f"⚠️ txflow追従taker失敗({repr(e)[:50]})")
        time.sleep(1)
        pos = self._tx_position()
        if abs(pos) < size * 0.5:                   # ヘッジ不成立→perpl脚unwind(裸回避)
            log("⚠️ txflowヘッジ不成立→perpl脚をunwind(裸回避)")
            self._perpl_unwind(perpl_is_buy, size)
            # ★txflow takerが約定していて建玉読みがラグると txflow が裸残になりうる。
            #   dirtyを立て次ループ先頭で両会場flatten確認(_reconcile_dirty)して塞ぐ。
            self.dirty = True
            return {"skip": "hedge_failed_unwound"}
        tx_fill = {"px": tx_px, "sz": abs(pos),
                   "fee": self.notional * self.fees["txflow_taker_bps"] / 1e4}

        # === HOLD ===
        time.sleep(float(self.cfg["hold_seconds"]))

        # === CLOSE: txflow reduce(maker→taker) + perpl reduce-only ===
        t_bid2, t_ask2 = self._txflow_bbo()
        tx_close_buy = not tx_is_buy
        tx_cpx = t_bid2 if tx_close_buy else t_ask2
        cid = self._tx_place_maker(tx_close_buy, tx_cpx, size, reduce_only=True)
        tx_cfill, dl = None, time.time() + lt
        while cid is not None and time.time() < dl:
            tx_cfill = self._tx_fill(cid, size)
            if tx_cfill:
                break
            time.sleep(poll)
        if not tx_cfill:  # taker強制close
            if isinstance(cid, int):
                try:
                    self.tx.cancel_order(self.symbol, cid)
                except Exception:
                    pass
            pos = self._tx_position()
            if abs(pos) > 1e-8:
                px = t_bid2 if pos > 0 else t_ask2
                self.tx.place_limit_order(self.symbol, pos < 0, px, abs(pos),
                                          reduce_only=True, tif=self.tx.TIF_IOC)
                tx_cfill = {"px": px, "sz": abs(pos), "fee": self.notional * self.fees["txflow_taker_bps"] / 1e4}
            else:
                tx_cfill = {"px": tx_cpx, "sz": size, "fee": 0.0}
        # perpl reduce-only close(reduce≈無料・確実)
        pp_szi = self.pp_exec.get_position_szi()
        p_bid, p_ask = self._perpl_bbo()
        pp_close_px = (p_bid + p_ask) / 2
        if abs(pp_szi) > 1e-8:
            r = self.pp_exec.place_order(pp_szi < 0, abs(pp_szi), "close", reduce_only=True)
            if isinstance(r, dict) and r.get("price"):
                pp_close_px = float(r["price"])

        # === 損益(long=close-open / short=open-close) ===
        tx_o, tx_c = tx_fill["px"], tx_cfill["px"]
        tx_pnl = (tx_c - tx_o) * size if tx_is_buy else (tx_o - tx_c) * size
        pp_pnl = (pp_close_px - pp_open_px) * size if perpl_is_buy else (pp_open_px - pp_close_px) * size
        tx_fee = tx_fill.get("fee", 0) + tx_cfill.get("fee", 0)
        pp_fee = self.notional * (self.fees["perpl_maker_bps"] + self.fees["perpl_close_bps"]) / 1e4  # lead=常にmaker
        fees = tx_fee + pp_fee
        net = tx_pnl + pp_pnl - fees
        return {
            "ts": round(time.time(), 3), "dir_buy": dir_buy, "dry_run": False,
            "size": size, "notional_usd": self.notional,
            "txflow": {"open": tx_o, "close": tx_c, "pnl": round(tx_pnl, 5), "taker_follow": tx_taker},
            "perpl": {"open": round(pp_open_px, 1), "close": round(pp_close_px, 1),
                      "pnl": round(pp_pnl, 5), "taker_hedge": False},
            "fees_usd": round(fees, 6), "volume_usd": round(self.notional * 4, 4), "net_usd": round(net, 6),
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
        self._startup_reconcile()  # 起動時に両会場BTCフラット(再起動での建玉残存防止)
        dir_buy = True
        while self.cfg.get("enabled", True):
            if self.cum_net <= -float(self.cfg["loss_budget_usd"]):
                if not self.halted:
                    self.halted = True
                    log(f"⚠️ loss_budget${self.cfg['loss_budget_usd']}超過(net=${self.cum_net:.3f})=自己ハルト")
                time.sleep(30)
                continue
            # cycle例外後は新規サイクルより先に両会場をフラット化(裸脚の上に建てない)
            if self.dirty and not self.dry_run:
                if self._reconcile_dirty():
                    self.dirty = False
                    log("error後reconcile: 両会場フラット確認。稼働再開")
                else:
                    log("⚠️ error後reconcile未完(両会場flat未確認)=次ループで再試行")
                    time.sleep(self.cfg.get("cooldown_seconds", 10))
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
                self._rl_backoff = 0.0                   # clean cycle=429バックオフ解除
            except _pc.PerplRateLimitError as e:
                # CF 1015はIPレート制限=即再突入するとバンを延ばす。エスカレート待機で叩くのを止める。
                self.dirty = True                        # 途中でperpl脚が建った疑い→次ループでflatten
                base = float(self.cfg.get("rl_backoff_base_seconds", 30))
                cap = float(self.cfg.get("rl_backoff_cap_seconds", 240))
                self._rl_backoff = min(cap, base if self._rl_backoff <= 0 else self._rl_backoff * 2)
                log(f"perpl 429=CFレート制限。{self._rl_backoff:.0f}s バックオフ(叩くのを止める): {repr(e)[:80]}")
                time.sleep(self._rl_backoff)
                continue
            except Exception as e:
                # 例外はcycle途中(perpl脚約定後など)で起きうる=裸脚の疑い。
                # dirtyを立て、次ループ先頭で両会場フラット化してから再開する。
                self.dirty = True
                log(f"cycleエラー(継続→次ループでflatten): {repr(e)[:120]}")
            time.sleep(self.cfg.get("cooldown_seconds", 10))


def main() -> None:
    cfg = yaml.safe_load((APP / "config.yaml").read_text())
    XVenueHedge(cfg).loop()


if __name__ == "__main__":
    main()
