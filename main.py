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
        self.coin = "1"  # BTC coin_index (txflow), perpl BTC=market1

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

    # ---- 1サイクル(dry_run: 板から模擬) ----
    def run_cycle(self, dir_buy: bool) -> dict:
        """dir_buy=True: txflow BUY(long) / perpl SELL(short)。deltaは相殺。
        dry_runでは maker が自分側 touch で刺さる best-case を仮定(逆選択はmarkoutで別途評価)。"""
        t_bid, t_ask = self._txflow_bbo()
        p_bid, p_ask = self._perpl_bbo()
        mid = (t_bid + t_ask) / 2
        size = self.notional / mid

        if not self.dry_run:
            raise NotImplementedError(
                "実弾パスは未実装(dry_runで構造検証後に実装)。txflow maker+perpl maker→takerフォールバック")

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
                self._record(rec)
                dir_buy = not dir_buy
            except Exception as e:
                log(f"cycleエラー(継続): {repr(e)[:120]}")
            time.sleep(self.cfg.get("cooldown_seconds", 10))


def main() -> None:
    cfg = yaml.safe_load((APP / "config.yaml").read_text())
    XVenueHedge(cfg).loop()


if __name__ == "__main__":
    main()
