#!/usr/bin/env python3
"""xvenue-hedge: txflow BTC × perpl BTC クロス会場デルタ中立farm。

txflowでBTCをmaker farm(将来pt)しつつ、perplで逆BTCをヘッジ=デルタ中立・両会場で二重farm。
効率値(出来高÷損失)は data/cycles.jsonl の独立台帳で計測する([[txflow-perpl-xhedge-farm]])。

## 構成
- txflow脚(farm): TxflowClient(txflow-bot/src、eth_account署名)。BTC=coin1、maker。
- perpl脚(hedge): PerplExecutor/PerplMarketData(apps/hyperliquid-bot/src、Ed25519署名)。BTC=market1。
  maker(0.9bps)で置き、leg_timeout内に刺さらなければtakerフォールバック(6.9bps)で裸窓を閉じる。
- 保守版クライアントを import 再利用(コピーしない=分岐を作らない)。venvは依存が揃った
  apps/hyperliquid-bot/.venv を使う(ecosystem.config.js の interpreter)。
  ★perpl層の参照先は 2026-07-26 に hlbot-sandbox から apps/hyperliquid-bot へ移した
    (フォーク統合。下の sys.path.insert のコメント参照)。

## 安全
- dry_run(既定): 発注せず両会場の板から約定を模擬。1サイクル完走を確認してから実弾化。
- loss_budget: 台帳の累積net損失が超えたら自己ハルト。
- perplは共有IPのCF 1015レート制限あり=pair_hedgeと帯域競合。poll控えめ。

## 本botのperpl脚は同一口座(2780、hlbotのperpl口座)だがhlbotの掃除対象外(取扱銘柄はself.symbols外
   →sweep_orphan_stopsはskip、sweep_orphan_positionsは'ambiguous'=触らず通知のみ)。HYPEもpair_hedge(SOL/ETH)管理外で同様に安全。
"""
import importlib.util
import json
import os
import random
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

APP = Path(__file__).resolve().parent


def _load_as_package(pkg_name: str, pkg_dir: Path, submodules: list[str]) -> dict:
    """pkg_dir 内の .py を pkg_name パッケージ配下として import(ライブコード再利用・コピーしない)。
    txflow-bot と hyperliquid-bot が両方 `src` パッケージ名を使い衝突するため、txflow側を別名で読む。
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

# --- perpl: apps/hyperliquid-bot の `src` パッケージをそのまま使う(perpl_exchange は from src import 依存) ---
# ★2026-07-26 に hlbot-sandbox から移行した。理由: perpl層が2系統にフォークしており、
#   hlbot-sandbox 側(mtime 07-13)には **fills ページング打ち切り判定のバグが残っていた**
#   (429=CF 1015 の量的主因。5,000-6,700ページ取得/時)。修正は apps/hyperliquid-bot 側に
#   しか入っておらず、xvenue はバグ持ちの古いスナップショットを使い続けていた。
#   API面は apps/hyperliquid-bot 側が厳密な上位集合(774行→1521行)で、署名も
#   PerplMarketData に binance_symbol(デフォルト付き)が増えるだけ=呼び出し側は無変更。
sys.path.insert(0, str(Path.home() / "apps" / "hyperliquid-bot"))
from src import perpl_client as _pc            # noqa: E402
from src import perpl_exchange as _pe          # noqa: E402
from src import perpl_account_guard as _pag    # noqa: E402  口座レベルの合算ガード(両セル共有)
PerplClient = _pc.PerplClient
PerplMarketData = _pe.PerplMarketData
PerplExecutor = _pe.PerplExecutor

CYCLES_PATH = APP / "data" / "cycles.jsonl"
STATUS_PATH = APP / "data" / "status.json"

# perpl の market 定義。**config の symbol から引く**(2026-07-27)。
# ★旧コードは market_id=1(BTC)のハードコードで、config.yaml の symbol を変えても追随せず
#   「txflow=新銘柄 / perpl=BTC」という致命的ミスマッチを生む地雷だった(監査 B-4)。
#   2026-07-24 に HYPE→BTC を戻したときは手で書き換えていて、忘れれば別銘柄をヘッジする。
# 案④確定ネガ(2026-07-24): HYPE/ZEC は perpl 側ワイド spread の価格損が txflow maker 節約に
#   必ず負ける(会場間流動性の反転)→BTC が最適。定義は残すが通常は BTC を使う。
_PERPL_MARKETS = {
    "BTC":  {"market_id": 1,  "price_decimals": 1, "size_decimals": 5, "leverage": 3},
    "ETH":  {"market_id": 20, "price_decimals": 2, "size_decimals": 3, "leverage": 3},
    "HYPE": {"market_id": 40, "price_decimals": 4, "size_decimals": 2, "leverage": 3},
    "ZEC":  {"market_id": 50, "price_decimals": 2, "size_decimals": 4, "leverage": 3},
}

class _PerplLeg:
    """perpl の1市場ぶんの実行面(market_id/精度/板/executor/BBOキャッシュ)を1個にまとめる。

    ★このクラスの唯一の存在理由は「**グローバルを持たないこと**」(2026-07-27)。
      旧実装は `PERPL_MCFG` というモジュールグローバルの単一 dict を __init__ で書き換える
      方式で、2市場を同時に扱った瞬間に「最後に書き換えた側の market_id/精度」が両脚に効く。
      2026-07-24 の「銘柄ハードコードで裸建玉を量産」と**同型の罠**なので、差し替え式ではなく
      脚ごとに持つ形にする。BBOキャッシュを脚別にするのも同じ理由 — 単一のままだと
      ETH の発注に BTC の BBO が乗る。

    ★PerplAccountFeed はここに持たない。全market横断のストリームで getter が market_id
      フィルタ済みなので、client 単位で1本を共有する(WS上限 ~5/IP)。"""

    def __init__(self, symbol: str, mcfg: dict, client, bbo_ttl: float):
        self.symbol = symbol.upper()
        self.name = f"perpl:{self.symbol}"
        self.mcfg = dict(mcfg)
        self.market_id = int(self.mcfg["market_id"])
        self.price_decimals = int(self.mcfg["price_decimals"])
        self.size_decimals = int(self.mcfg["size_decimals"])
        self.market = PerplMarketData(self.name, client, self.mcfg)
        self.exec = PerplExecutor(self.market, client)
        self.book = _pc.PerplBookFeed(client.ws_url, self.market_id, self.price_decimals)
        self.book.start()
        self.bbo_ttl = bbo_ttl
        self.bbo_cache = None      # (monotonic時刻, (bid, ask))。**脚別**であることが重要

    def __repr__(self) -> str:
        return f"<_PerplLeg {self.name} mid={self.market_id}>"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class XVenueHedge:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.dry_run = cfg.get("dry_run", True)
        self.notional = float(cfg["notional_usd"])
        self.fees = cfg["fees"]
        self.symbol = cfg.get("symbol", "HYPE")  # txflow place/cancel/l2book は symbol名を取る(内部でcoin_index)
        # ★perpl market を symbol から差し替える(2026-07-27 B-4)。ここが無いと config の symbol を
        #   変えても perpl 側は BTC のままで、txflow=新銘柄 / perpl=BTC のミスマッチになる。
        _m = _PERPL_MARKETS.get(self.symbol.upper())
        if _m is None:
            raise SystemExit(f"perpl market 未定義: {self.symbol}（_PERPL_MARKETS に追加すること）")

        # --- txflow(BTC価格・発注) ---
        load_dotenv(Path.home() / "apps" / "txflow-bot" / ".env")
        tx_key = os.environ.get("TXFLOW_AGENT_PRIVATE_KEY") if not self.dry_run else None
        self.tx = TxflowClient(agent_private_key=tx_key,
                               main_address=os.environ.get("TXFLOW_MAIN_ADDRESS"))
        self.coin = self.tx.coin_index(self.symbol)  # info l2Book 用の coin_index(HYPE=44)
        _tx_sd = self.tx._symbol_meta[self.symbol.upper()]["size_decimals"]
        # 両会場のsize_decimalsは異なる(HYPE: txflow=1/perpl=2)。粗い方に丸めれば両脚同量=裸デルタ回避。
        self._size_round = min(_tx_sd, int(_m["size_decimals"]))
        # txflow の価格刻み(follow の join_offset で使う)。price_decimals はクライアント側でキャッシュ。
        self._px_round = self.tx.price_decimals(self.symbol)
        self._tx_tick = self.tx.price_tick(self.symbol)

        # --- perpl(**脚ごとに1組**。グローバルは持たない。_PerplLeg の docstring 参照) ---
        load_dotenv(Path.home() / "apps" / "hyperliquid-bot" / ".env")
        self.pp_client = PerplClient(os.environ["PERPL_API_KEY"], os.environ["PERPL_API_KEY_SECRET"])
        _bbo_ttl = float(cfg.get("perpl_bbo_ttl_seconds", 2.0))
        # A(2026-07-24): BBOを常駐板WS(公開market-data)から取り、REST get_context(CF 1015誘発)を減らす。
        # 取れない/古いときは None→従来のREST短TTLキャッシュにフォールバック(WSは最適化・正ではない)。
        self.lead_leg = _PerplLeg(self.symbol, _m, self.pp_client, _bbo_ttl)
        self.legs = {self.lead_leg.symbol: self.lead_leg}
        # ヘッジ脚(perpl ETH)。**既定は構築しない** — WS/接続を焼かないため。
        # lead を txflow より大きく建てた残差を、この脚で相殺する(3脚化=C5)。
        self.hedge_leg: Optional[_PerplLeg] = None
        if cfg.get("hedge_leg_enabled", False):
            _hsym = str(cfg.get("hedge_symbol", "ETH")).upper()
            _hm = _PERPL_MARKETS.get(_hsym)
            if _hm is None:
                raise SystemExit(f"perpl hedge market 未定義: {_hsym}（_PERPL_MARKETS に追加）")
            if _hsym == self.lead_leg.symbol:
                raise SystemExit(f"hedge_symbol が lead と同一: {_hsym}（別market にすること）")
            self.hedge_leg = _PerplLeg(_hsym, _hm, self.pp_client, _bbo_ttl)
            self.legs[_hsym] = self.hedge_leg
            self._sr_hedge = self.hedge_leg.size_decimals
        # A2(2026-07-24): 建玉読みを常駐認証WS(PerplAccountFeed)から取り、CF 1015の主因である
        # 「1操作1接続」の認証WSハンドシェイクを減らす。取れない/古いときは None→従来のREST/WS短命経路。
        # ★2026-07-27: 直接生成していたため **認証WSが2本張られていた**。PerplExecutor が
        #   内部で client.shared_account_feed() を呼んで別インスタンスを作るため。
        #   perpl の WS 上限は ~5/IP なので1本ぶんの無駄が 429 を近づけていた。
        #   account ストリームは全market横断で getter が market_id フィルタ済み＝共有して正しい。
        self.pp_account = self.pp_client.shared_account_feed()
        self.pp_account.start()

        # --- 台帳(累積) ---
        self.cum_volume = 0.0
        self.cum_net = 0.0
        self.cum_fees = 0.0
        self.cycles = 0
        # A/B の腕を振る RNG。★決定論的な割り当ては「気づいていない周期」と同期しうる
        #   (2026-07-29: attempts%2 で振ったら dir_buy と交絡して約定率が逆に出た)。
        #   テストから差し替えられるよう属性で持つ。
        self._rng = random.Random()
        self.halted = False
        self.halted_reason = ""
        self.dirty = False  # cycle例外後の未フラット疑い。両会場flat確認まで新規サイクルを止める
        # dirty に入った時刻(status.json 経由で外側に継続時間を見せる用)。dirty=True の代入は
        # 7箇所に散っているので、そこは触らず **loop() 側で一元管理**する。
        self.dirty_since: Optional[float] = None
        self._rl_backoff = 0.0                          # perpl 429時のエスカレート待機(clean cycleで0へ)
        self._skip_streak = 0                           # 連続見送り数。詰まったまま叩き続ける自己増幅429を止める
        self._tx_eq_cache = (0.0, None)                 # (monotonic, txflow accountValue) 60s TTL
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
            if not d.get("skip_reason"):        # 中断行(D-5)は完走サイクルではない
                self.cycles += 1

    # ---- 板取得 ----
    def _txflow_bbo(self):
        b = self.tx.info("l2Book", coin=self.coin)
        bids, asks = b["levels"][0], b["levels"][1]
        return float(bids[0]["px"]), float(asks[0]["px"])

    def _pp_szi(self, leg: "_PerplLeg") -> float:
        """perpl の符号付き建玉(脚指定)。常駐口座WS(handshake不要)を優先、取れなければ従来の
        get_position_szi(1操作1接続)へフォールバック。両者とも失敗は0.0扱い(get_position_sziと同契約)。
        ★fail-open(429で0.0)なので『建玉ゼロ確認してから発注』の判断には使わない(それは_fetch_position)。"""
        p = self.pp_account.get_position(leg.market_id, leg.price_decimals, leg.size_decimals)
        if p is not None:
            return p.get("szi", 0.0)
        return leg.exec.get_position_szi()

    def _pp_szi_strict(self, leg: "_PerplLeg") -> Optional[float]:
        """符号付き建玉。**読めなければ None**(fail-open しない)。0.0 は『確認できたフラット』。

        _pp_szi は 429 等で 0.0 を返す契約なので「建玉が無い」の判断には使えない。close 判定に
        使うと、建玉が残っているのに close を飛ばし、さらに約定価格として BBO 中値(架空値)を
        台帳に書く二重障害になる(2026-07-27 N-2)。"""
        p = self.pp_account.get_position(leg.market_id, leg.price_decimals, leg.size_decimals)
        if p is not None:
            return p.get("szi", 0.0)
        try:
            pos = leg.exec._fetch_position()          # fail-closed(読めなければ例外)
        except Exception:
            return None
        if pos is None:
            return 0.0
        sz = _pe.scaled_to_size(int(pos["s"]), leg.market.size_decimals)
        return sz if pos.get("sd") == 1 else -sz

    def _pp_feed_filled(self, leg: "_PerplLeg") -> Optional[float]:
        """常駐口座WSから見た perpl 建玉の絶対サイズ。**handshake不要**。
        feedが切断/陳腐化/スナップショット未達なら None(=判定不能、呼び側は従来経路へ)。

        エントリーガードが「建玉ゼロを確認してから置く」不変条件を作っているので、resting保有中に
        建玉があればそれはこのrestingの約定量そのもの(perpl_exchange._fill_from_position と同じ論法)。"""
        p = self.pp_account.get_position(leg.market_id, leg.price_decimals, leg.size_decimals)
        return None if p is None else abs(p.get("szi") or 0.0)

    def _pp_entry_blocked(self, leg: "_PerplLeg") -> bool:
        """常駐WSだけで『エントリーを置けない』と断定できるか。**handshake不要**。

        ★"置ける"の判断には使わない。feedは差分配信なので置いた直後の注文が未反映な窓があり、
        不在の確認に使うと fail-open(二重建玉)になる。ここは**"置けない"方向にだけ**効かせる
        非対称な使い方で、クリーン/判定不能なら False を返して従来どおり place_maker_resting の
        ガード(取引所の正)に最終判断を委ねる。2026-07-25 の xvenue 429 は 277件がこのガードの
        空振り(=どうせ弾かれる状況でhandshakeを焼いていた)だった。"""
        oids = self.pp_account.get_live_oids(leg.market_id)
        if oids:
            # ★feed の陳腐化に対する自力復帰(2026-07-27 実測の実害から追加)。
            #   常駐WS feed が「取り消された/約定した oid」を消し損ねると、ここが永久に True を
            #   返して全サイクルを見送る。実測: 同一 oid が **49連続** で検出され約3時間停止。
            #   取引所に問い合わせたら **指値ゼロ・建玉フラット**で、板には何も無かった。
            #   2026-07-25 の 6.5時間デッドロックと同じ「稼働中の復帰経路が無い」問題で、
            #   従来は再起動でしか直らなかった。
            #   見送りが続いたら feed を信じず取引所で裏を取り、(a)空なら feed 陳腐化として続行、
            #   (b)本当に残っていればその場で取消す。取得失敗時は従来どおり見送る(fail-closed)。
            if self._skip_streak >= int(self.cfg.get("feed_distrust_after_skips", 3)):
                try:
                    live = leg.exec.list_open_maker_orders()
                except Exception:
                    live = None
                if live is not None and not live:
                    log(f"{leg.name} feed は指値生存({sorted(oids)})と言うが取引所は空"
                        f" → feed 陳腐化とみなして続行")
                    return False
                if live:
                    log(f"{leg.name} 孤児 resting を検出 {live} → その場で取消す(稼働中の復帰)")
                    for _s, oid in live:
                        self._pp_cancel_verified(leg, int(oid))
                    return True
            log(f"{leg.name} 板に自分の指値が生存(feed) {sorted(oids)} → handshakeせず見送り")
            return True
        szi = self._pp_feed_filled(leg)
        if szi is not None and szi > 1e-8:
            log(f"{leg.name} 建玉が残っている(feed) szi={szi} → handshakeせず見送り")
            return True
        return False

    def _tx_marketable_px(self, is_buy: bool, bid: float, ask: float) -> float:
        """txflow taker IOC用の【確実約定価格】。touchちょうどだとBBO読取〜発注の間に板が
        動くとIOCが刺さらない(2026-07-23 hedge_fail実測の真因)。クロス方向にバッファを足す。
        IOCは板の最良値で約定する→バッファは約定保証のみで、板が動かなければtouch約定=コスト増なし。
        client.quantize_priceがtick丸めするのでtick非整合でも安全。"""
        b = float(self.cfg.get("taker_cross_bps", 8.0)) / 1e4
        return ask * (1 + b) if is_buy else bid * (1 - b)

    def _perpl_bbo(self, leg: "_PerplLeg", force_fresh: bool = False):
        """perpl BBO。get_best_bid_askはキャッシュ無しで毎回REST get_context(CF保護)を叩く=429主因。
        短TTLキャッシュで requote/open/close の連続読みを1回のRESTに畳む(BTC BBOは数秒で不変)。
        force_fresh=True で必ず取り直す(requoteのtouch移動判定など鮮度が要る所)。"""
        # ① 常駐板WS(REST不要・CF 1015を誘発しない)。取れれば常にライブなのでキャッシュ不要。
        bb = leg.book.get_best_bid_ask()
        if bb is not None:
            return bb
        # ② フォールバック: REST get_context(短TTLキャッシュ)。WSが未起動/古い/切断中のとき。
        now = time.monotonic()
        # ★キャッシュは**脚別**(leg.bbo_cache)。単一にすると ETH の発注に BTC の BBO が乗る。
        if not force_fresh and leg.bbo_cache is not None:
            ts, val = leg.bbo_cache
            if now - ts < leg.bbo_ttl:
                return val
        val = leg.market.get_best_bid_ask()  # (bid, ask)
        leg.bbo_cache = (now, val)
        return val

    # ================= 実弾: txflow脚(farm) =================
    def _tx_join_px(self, is_buy: bool, bid: float, ask: float, offset_ticks: int) -> float:
        """txflow maker の join 価格。offset=0 は touch に並ぶ(従来)。>0 で板の**内側**へ寄せる。

        ## なぜ内側に置くのか(2026-07-28)
        txflow BTC のスプレッドは実測 **15/15 で常に 2tick(0.032bps)固定**= MM が張り付いている。
        touch に並ぶと列の最後尾で、bid 側には 0.09〜4.5 BTC が先に並んでいる。自分のサイズは
        0.0024 BTC なので、その全部が捌けるまで刺さらない → 6秒窓で埋まらず **47% が taker 落ち**。
        1tick 内側に置くとスプレッドが 1tick になり、**誰も内側に入れない無競争の最良気配**に
        なる(内側に入るには反対側の touch を越えるしかない)。対向のフローに最初に当たる。

            コスト  1tick = 0.0158bps
            節約    taker 回避 = 3.0bps(txflow taker 4.5 - maker 1.5)
            → 190倍 有利

        ★必ず反対側の touch を越えないようクランプする。越えると post_only が拒否され、
          呼び側は taker に落ちる = 直そうとした当のものを悪化させる。"""
        if offset_ticks <= 0:
            return bid if is_buy else ask
        t, d = self._tx_tick, self._px_round
        if is_buy:                      # ask-1tick を上限に、bid から内側へ
            return min(round(bid + offset_ticks * t, d), round(ask - t, d))
        return max(round(ask - offset_ticks * t, d), round(bid + t, d))

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

    def _tx_resolve_oid(self, ident) -> Optional[int]:
        """ident(int または {"cloid":...})から板上の oid を引く。板に無ければ None。
        cloid でしか同定できていない指値を取消すために必要(2026-07-27)。"""
        if isinstance(ident, int):
            return ident
        if isinstance(ident, dict) and ident.get("cloid"):
            try:
                for o in (self.tx.get_open_orders() or []):
                    if o.get("cloid") == ident["cloid"]:
                        return int(o["oid"])
            except Exception:
                return None
        return None

    def _tx_order_alive(self, oid) -> bool:
        """ident がまだ txflow の板に生存しているか。**取消の空振りを検知する**ために使う
        (2026-07-25)。取得失敗は True(=生存扱い)を返す — 読めないときに「消えた」と判断すると
        同じ脚を二重発注して建玉2倍化を招くので、fail-closed 側に倒す。

        ★2026-07-27: 非int を一律 False(=消えた)で返しており docstring と矛盾していた。これが
          建玉2倍化の実経路。_tx_place_maker は openOrders に1.8秒出てこないと {"cloid":...} を
          返すので、requote 分岐で cancel_order(dict) が例外→握り潰し→ここが False→「板から
          消えた」と誤判定して再発注、で2本が同時に生きる(07-26 23:18 / 07-27 05:12 に実測)。
          openOrders は cloid を持つ(_tx_place_maker が同定に使っている)ので cloid でも照合する。"""
        try:
            orders = self.tx.get_open_orders() or []
        except Exception:
            return True                       # 読めない=生存扱い(fail-closed)
        if isinstance(oid, int):
            return any(int(o.get("oid", -1)) == oid for o in orders)
        if isinstance(oid, dict) and oid.get("cloid"):
            return any(o.get("cloid") == oid["cloid"] for o in orders)
        return True                           # 判定不能=生存扱い(fail-closed)

    def _tx_position(self) -> float:
        """txflow の対象銘柄の符号付き建玉(取引所の正)。失敗は例外(fail-closed用に呼び側で扱う)。
        ★銘柄はself.symbol依存(2026-07-24: BTCハードコードがHYPE切替で常に0.0を返し裸txflowを量産した)。"""
        chs = self.tx.get_clearinghouse_state(self.tx.main_address)
        sym = self.symbol.upper()
        for p in chs.get("assetPositions", []):
            pos = p["position"]
            if str(pos["coin"]).split("-")[0].upper() == sym:
                return float(pos.get("szi", 0))
        return 0.0

    def _tx_equity(self, ttl: float = 60.0) -> Optional[float]:
        """txflow 口座の accountValue(USD)。ttl 秒キャッシュ。読めなければ直近値(無ければ None)。

        ★2026-07-27追加: それまで main.py に balance 参照が1つも無く、$26/日 燃やしている口座の
          残高を誰も見ていなかった。証拠金の床(notional/leverage)に当たると発注が通らなくなる。"""
        now = time.monotonic()
        ts, val = self._tx_eq_cache
        if val is not None and now - ts < ttl:
            return val
        try:
            chs = self.tx.get_clearinghouse_state(self.tx.main_address)
            v = float((chs.get("marginSummary") or {}).get("accountValue"))
        except Exception:
            return val                        # 読めない=直近値のまま(読めないことでハルトさせない)
        self._tx_eq_cache = (now, v)
        return v

    def _tx_unwind(self, opened_is_buy: bool, size: float) -> None:
        """txflow脚を建玉(=正)からtaker IOCで即クローズ(ヘッジ失敗時の裸回避)。"""
        try:
            pos = self._tx_position()
        except Exception as e:
            log(f"⚠️ unwind: txflow建玉取得失敗={repr(e)[:50]} 手動確認要"); return
        if abs(pos) < 1e-8:
            return
        t_bid, t_ask = self._txflow_bbo()
        px = self._tx_marketable_px(pos < 0, t_bid, t_ask)  # long→sell/short→buy、確実約定バッファ付き
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
        p_bid, p_ask = self._perpl_bbo(self.lead_leg)
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
        p_bid2, p_ask2 = self._perpl_bbo(self.lead_leg)
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
            "ts": round(time.time(), 3), "symbol": self.symbol, "dir_buy": dir_buy, "dry_run": True,
            "size": size, "notional_usd": self.notional,
            "txflow": {"open": tx_open, "close": tx_close, "pnl": round(tx_pnl, 5)},
            "perpl": {"open": pp_open, "close": pp_close, "pnl": round(pp_pnl, 5)},
            "fees_usd": round(fees, 6), "volume_usd": round(volume, 4), "net_usd": round(net, 6),
        }

    # ================= 実弾サイクル =================
    def _pp_cancel_verified(self, leg: "_PerplLeg", oid: int, tries: int = 3) -> bool:
        """perpl の resting 取消を『板から消えた』まで確認する。消えたらTrue。

        PerplExecutor.cancel_order は失敗しても例外を投げない(内部でログして戻る)ため、
        投げっぱなしだと取消できなかった resting が板に残る。孤児 resting が1本でも残ると
        place_maker_resting のガード(_entry_preconditions_ok)が以降**全サイクル**で発注を
        拒否し、xvenue-hedge には掃除役がいない(hlbot の sweep_orphan_stops は
        `coin not in self.symbols` で perpl:BTC を skip する)ため永久に復帰しない。
        2026-07-25 に 6.5時間サイクルゼロで停止した実害の対策。"""
        for _ in range(tries):
            try:
                leg.exec.cancel_order(oid)
            except Exception:
                pass
            try:
                live = leg.exec.list_open_maker_orders()
            except Exception:
                live = None
            if live is not None and all(int(o) != int(oid) for _, o in live):
                return True
            time.sleep(2)
        log(f"⚠️ {leg.name} resting oid={oid} を取消せない(板に残存)=次サイクルはガードでskipされる")
        return False

    def _pp_partial_abort(self, leg: "_PerplLeg", oid, is_buy: bool, filled_sz: float) -> None:
        """perpl lead の**部分約定は『未約定』として扱う**(残 resting を取消し約定ぶんを畳む)。

        PerplExecutor.place_order と同じ契約: 部分約定を『約定』として返すと、呼び側は全量
        (size)でヘッジ・損益計上するのに取引所には端数しか無い状態になる。実際 2026-07-25 の
        cycle#125 は 0.00027/0.0023(11.7%)しか約定していないのに全量約定として扱われ、
        ヘッジの88%が裸・台帳も過大計上・残 resting が孤児化して bot が停止した。

        戻り = unwind の約定dict/None(台帳計上用。`_perpl_unwind` の docstring 参照)。"""
        self._pp_cancel_verified(leg, oid)
        r = None
        if filled_sz > 1e-8:
            r = self._perpl_unwind(leg, is_buy, filled_sz)
        self.dirty = True  # 畳めたか確認できていない→次ループ先頭で両会場flatを確認する
        return r

    def _stash_lead_abort(self, open_px: float, sz: float, is_buy: bool, unwind: Optional[dict]) -> None:
        """lead の部分約定→畳みを**台帳に持ち帰るために self に積む**(2026-07-29)。

        ★これが無いと、部分約定した往復は open の maker 手数料も価格差も**台帳に1行も
          残らない**(見送りは行を書かないため)。2026-07-28 に ETH ヘッジ脚で塞いだ
          「unwind の戻り値を捨てるとコストがブラックホールになる」と**完全に同じ穴**が
          lead 側に残っていた([[xvenue-eff9000-is-txflow-dilution-2026-07-28]])。
          実測(1脚期3時間): 7件・建てた名目 $339 = 出来高 $678 が不可視だった。

        ★戻り値のタプル(filled, px, got_sz)を増やして運ばないのは、あの契約を変えると
          呼び側が黙って壊れるため(test_maker_lead_returns_three_tuple)。self に置いて
          `_run_cycle_live` の見送りパスで回収する。"""
        if sz <= 1e-8 or not open_px:
            return
        notional = float(open_px) * float(sz)
        fee = notional * self.fees["perpl_maker_bps"] / 1e4      # close は無料 taker
        pnl = 0.0
        if isinstance(unwind, dict):
            cpx = float(unwind.get("price") or unwind.get("px") or 0.0)
            if cpx > 0:
                # is_buy=True で建てた=ロング。畳みは売り。
                pnl = (cpx - open_px) * sz if is_buy else (open_px - cpx) * sz
                notional += cpx * sz                              # 出来高は open+close
            else:
                notional *= 2                                     # close 価格不明=open で近似
        else:
            notional *= 2
        self._lead_abort = {"volume_usd": notional, "fees_usd": fee, "pnl": pnl}

    def _perpl_maker_lead(self, leg: "_PerplLeg", is_buy: bool, size: float,
                          timeout_s: Optional[float] = None, keep_partial: bool = False):
        """改善①②: perpl maker を先行させ requote しながら perpl_lead_timeout まで刺しにいく。
        戻り(filled, fill_px, got_sz)。未約定は(False,None,0.0)。建玉=正でfill確認(false-negative対策)。
        改善④(2026-07-25): 約定検知と発注可否の先読みを**常駐口座WS**に寄せ、CF 1015の主因である
        「1操作1接続」の認証WSハンドシェイクを削る。feedが古い/切断なら従来経路へフォールバック。

        keep_partial=True(ヘッジ脚専用): 部分約定を**畳まずに残したまま**(False, px, 実約定)で返す。
          呼び側(`_perpl_hedge_follow`)が残りを taker で埋めて脚を完成させるため。
          ★lead 脚では絶対に True にしないこと — lead の部分建玉はヘッジ相手がおらず丸ごと裸に
            なる(既定 False のまま = 2026-07-25 の全量約定契約を維持)。"""
        # ★ETH ヘッジ脚もこの関数を使う(専用実装を書かない)。取消×約定レース防御・部分約定
        #   grace・timeout時の部分建玉回収がここに全部入っており、書き直せば 2026-07-25 の
        #   事故が確実に再発する。違いは timeout と部分約定の扱いだけなので引数で受ける。
        plt = float(self.cfg["perpl_lead_timeout_seconds"] if timeout_s is None else timeout_s)
        rq = float(self.cfg["requote_interval_seconds"])
        poll = float(self.cfg["poll_interval_seconds"])
        grace = float(self.cfg.get("partial_grace_seconds", 5.0))
        since_ms = int(time.time() * 1000)
        deadline = time.time() + plt
        oid, px, last_place, partial_since = None, None, 0.0, 0.0
        place_fail_logged = False        # 発注失敗ログは1試行につき1本だけ
        while time.time() < deadline:
            if oid is None:
                if self._pp_entry_blocked(leg):      # 常駐WSで先に見切る(handshakeを焼かない)
                    return False, None, 0.0
                p_bid, p_ask = self._perpl_bbo(leg)
                px = p_bid if is_buy else p_ask       # maker: buyはbid/sellはaskにjoin
                oid = leg.exec.place_maker_resting(is_buy, size, px, reduce_only=False)
                last_place, partial_since = time.time(), 0.0
                if oid is None:
                    # ★黙って再試行しないこと(2026-07-29)。ここは「発注できなかった」経路で、
                    #   ログが一切無かったため **サイズ $245 が 0/35 という結果だけが見え、
                    #   板が薄いのか発注が拒否されているのか区別できなかった**。
                    #   部分約定すら出ない 0% は板の厚みでは説明できない = 指値が乗っていない。
                    #   今朝の sr=44(証拠金不足で全拒否・症状は出来高半減だけ)と同型の盲点。
                    #   毎ループ出すと煩いので、1回の試行につき最初の1本だけ出す。
                    if not place_fail_logged:
                        log(f"⚠️ {leg.name} 指値を置けなかった(size={size} px={px}) "
                            f"→ {plt:.0f}s まで再試行。連続するなら証拠金/サイズ上限を疑う")
                        place_fail_logged = True
                        # ★失敗が続くときだけ口座を1回읽む。2026-07-29 はここが見えず、
                        #   「余力 $66.26 vs 必要 $66.67」を突き止めるのに11時間かかった。
                        #   失敗時限定なので通常運転では叩かない(429を作らない)。
                        self._log_margin_state(leg, size, px)
                    time.sleep(poll)
                    continue
            # 約定検知は常駐口座WS(handshake不要)を主に。取れない/古いときだけ従来の
            # poll_maker_fill(REST fills + 建玉フォールバックで1操作1接続)へ落とす。
            fsz = self._pp_feed_filled(leg)
            if fsz is None:
                f = leg.exec.poll_maker_fill(oid, since_ms)
                fsz = float(f.get("sz") or 0.0) if f else 0.0
            if fsz > 1e-8:
                if fsz >= size * 0.999:
                    return True, px, size
                # 部分約定。常駐WSは約定を即時に見るので「まだ埋まりかけ」を掴むことがある
                # (REST fillsのラグが今まで結果的にこれを均していた)。grace秒待って埋まらなければ見送る。
                if not partial_since:
                    partial_since = time.time()
                    log(f"{leg.name} lead 部分約定 {fsz}/{size} → {grace:.0f}s 様子見")
                elif time.time() - partial_since >= grace:
                    if keep_partial:
                        log(f"{leg.name} 部分約定のまま {fsz}/{size} → 残restingを取消し"
                            f"**建玉は残して**呼び側へ返す(taker で残りを埋める)")
                        self._pp_cancel_verified(leg, oid)
                        return False, px, fsz
                    log(f"{leg.name} lead 部分約定のまま {fsz}/{size} → 残restingを取消し畳んで見送り")
                    self._stash_lead_abort(px, fsz, is_buy,
                                           self._pp_partial_abort(leg, oid, is_buy, fsz))
                    return False, None, 0.0
            if time.time() - last_place >= rq:       # requote: touchが動いてたら置き直す
                szi = abs(self._pp_szi(leg))
                if szi >= size * 0.999:              # requote前に建玉=正で確認
                    return True, px, size
                if szi > 1e-8:      # 部分約定中は置き直さない(残りが孤児化する)。
                    time.sleep(poll)  # 見送るか待つかの判断は上のgrace付き分岐に一本化する
                    continue
                p_bid, p_ask = self._perpl_bbo(leg)
                new_px = p_bid if is_buy else p_ask
                if new_px != px:
                    if not self._pp_cancel_verified(leg, oid):
                        return False, None, 0.0      # 取消未確認で置き直すと建玉が2倍になる
                    oid = None
            time.sleep(poll)
        if oid:
            self._pp_cancel_verified(leg, oid)
        time.sleep(1)
        # ★keep_partial 経路では **strict** で読む。_pp_szi は 429 で fail-open(0.0)なので、
        #   部分建玉があるのに「0約定」と返すと呼び側が taker で全量を上塗りして過剰ヘッジになる。
        #   読めないときは got=None(判定不能)を返し、呼び側に taker を打たせない。
        if keep_partial:
            szi_s = self._pp_szi_strict(leg)
            if szi_s is None:
                log(f"⚠️ {leg.name} timeout時に建玉を読めない → 判定不能で返す(taker で上塗りしない)")
                self.dirty = True
                return False, px, None
            szi = abs(szi_s)
        else:
            szi = abs(self._pp_szi(leg))
        if szi >= size * 0.999:
            return True, px, size                    # 建玉あり=約定してた(poll false-negative)
        if szi > 1e-8:                               # timeout時の部分約定=ヘッジ相手のいない裸脚
            if keep_partial:
                log(f"{leg.name} timeout時に部分建玉 {szi}/{size} → 建玉は残して呼び側へ返す")
                return False, px, szi
            log(f"{leg.name} lead timeout時に部分建玉 {szi}/{size} → 畳んで見送り")
            self._stash_lead_abort(px, szi, is_buy, self._perpl_unwind(leg, is_buy, szi))
            self.dirty = True
        return False, None, 0.0

    def _perpl_unwind(self, leg: "_PerplLeg", was_buy: bool, size: float) -> Optional[dict]:
        """perpl脚(was_buy方向で建った)をreduce-onlyで反対に即クローズ(ヘッジ失敗時の裸回避)。
        ★get_position_sziは429でfail-open→0になり安全チェックが崩れるため使わない。
        reduce-onlyは「建玉方向にしか約定しない=flatならno-op/reject」なので、szi読めなくても
        安全に畳みにいける(fail-closed)。

        戻り = 約定dict(px等)/None。**呼び側が台帳に載せるため**に返す(2026-07-28)。
        従来は戻り値が無く、ヘッジ脚が部分約定→unwind したサイクルのコストが
        **台帳にも口座ガードにも1ドルも現れていなかった**(全サイクルの40%が該当)。"""
        try:
            r = leg.exec.place_order(not was_buy, size, "unwind", reduce_only=True)
            log(f"unwind: {leg.name} reduce-only close(was_buy={was_buy} size={size})")
            return r if isinstance(r, dict) else None
        except Exception as e:
            log(f"⚠️ {leg.name} unwind失敗={repr(e)[:50]} 手動フラット化要")
            return None

    def _perpl_close_leg(self, leg: "_PerplLeg", open_is_buy: bool, expect_sz: float):
        """perpl 1脚を reduce-only で畳み、(約定価格, 回収できたか) を返す(2026-07-27 C5 で抽出)。

        ★_pp_szi は fail-open(429で0.0)。これを「建玉なし」と読むと close を丸ごと飛ばした
          うえで約定価格に BBO 中値(架空値)を書く二重障害になる(N-2)。方向は open 時点で
          確定しているので、読めないときも reduce_only で畳みにいく
          (reduce_only は建玉方向にしか約定しない=flat なら no-op なので安全)。"""
        szi = self._pp_szi_strict(leg)
        b, a = self._perpl_bbo(leg)
        px, ok = None, True
        if szi is None:
            log(f"⚠️ {leg.name} close: 建玉を読めず → open方向から reduce_only で畳む(fail-closed)")
            close_sz = expect_sz
        else:
            close_sz = abs(szi) if abs(szi) > 1e-8 else 0.0
        if close_sz > 1e-8:
            close_is_buy = (szi < 0) if szi is not None else (not open_is_buy)
            r = leg.exec.place_order(close_is_buy, close_sz, "close", reduce_only=True)
            if isinstance(r, dict) and r.get("price"):
                px = float(r["price"])
        else:
            log(f"⚠️ {leg.name} close: open約定後に建玉が消えている(確認済みflat)")
            self.dirty = True
        if px is None:
            # 約定価格を取れない。BBO 中値は**架空値**なので、捏造せず旗を立てて記録に残す。
            px, ok = (b + a) / 2, False
            self.dirty = True
        return px, ok

    def _startup_reconcile(self) -> None:
        """起動時に両会場の対象銘柄をフラット化(mid-cycle再起動での建玉残存/2倍化を防ぐ)。dry_runは無処理。"""
        if self.dry_run:
            return
        # ★perpl BTC: **建玉を畳む前に**残存指値を取消す(2026-07-26追加)。
        #   ここが抜けていたため、_pp_cancel_verified が3回失敗して resting が板に残ると
        #   _entry_preconditions_ok(fail-closed)が以降の全エントリーを拒否し、**再起動しても
        #   _startup_reconcile がその指値を触らないので状態が引き継がれ、永久にサイクル0**
        #   になっていた。回復は「指値がいつか約定/失効する」か手動取消のみ=恒久デッドロック。
        #   txflow 側は最初から get_open_orders→cancel_order を回しており、perpl 側だけの抜け。
        #   順序が重要: 先に建玉を畳むと、残った指値が直後に約定して建玉が復活しうる。
        # ★2026-07-27: **全 perpl 脚**をループする。ヘッジ脚(ETH)を足したとき、pair_hedge 退役後の
        #   perpl:ETH は hlbot の symbols から外れるので sweep_orphan_stops は skip、
        #   sweep_orphan_positions は ambiguous で通知のみ = **この関数が ETH の唯一の掃除役**になる。
        #   ここが ETH をカバーしないと 2026-07-25 の 6.5時間デッドロック(孤児 resting →
        #   _entry_preconditions_ok が全エントリー拒否 → 再起動しても掃除されない)が ETH で再現する。
        for leg in self.legs.values():
            try:
                live = leg.exec.list_open_maker_orders()
                if live is None:  # 取得失敗(429等)。fail-closed で「消せた」とみなさない
                    log(f"⚠️ startup_reconcile {leg.name} 残存指値を読めず(429?)=取消を見送る")
                else:
                    for _sym, oid in live:
                        if not self._pp_cancel_verified(leg, int(oid)):
                            log(f"⚠️ startup_reconcile {leg.name} oid={oid} を取消せない"
                                f"=以降のエントリーがガードで拒否される(手動取消要)")
            except Exception as e:
                log(f"⚠️ startup_reconcile {leg.name} 指値取消失敗={repr(e)[:50]}")
            # 生の建玉(fail-openしない)を読んで方向付きで畳む
            try:
                pos = leg.exec._fetch_position()
                if pos is not None:
                    sz = _pe.scaled_to_size(int(pos["s"]), leg.market.size_decimals)
                    if sz > 1e-8:
                        self._perpl_unwind(leg, pos.get("sd") == 1, sz)  # sd==1(long)→sellで畳む
            except Exception as e:
                log(f"⚠️ startup_reconcile {leg.name} 建玉確認失敗={repr(e)[:50]}")
        self._sweep_retired_perpl_positions()
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
                px = self._tx_marketable_px(pos < 0, t_bid, t_ask)  # 確実約定バッファ付き
                self.tx.place_limit_order(self.symbol, pos < 0, px, abs(pos),
                                          reduce_only=True, tif=self.tx.TIF_IOC)
                log(f"startup_reconcile: txflow {abs(pos)} をフラット化")
        except Exception as e:
            log(f"⚠️ startup_reconcile txflow失敗={repr(e)[:50]}")

    def _log_margin_state(self, leg: "_PerplLeg", size: float, px: Optional[float]) -> None:
        """発注できなかったときに **なぜか** を1行で残す(2026-07-29)。fail-open。

        ★`_margin_cap` は equity から名目を導くが、発注を止めるのは **余力(balance)** =
          equity − 他の建玉が握っている証拠金。孤児建玉が1本あるだけで前提が崩れる。
          その差は口座を読まないと分からないので、失敗時だけ読む。"""
        try:
            snap = leg.exec._client.get_snapshot()
            bal = float(snap.get("balance") or 0.0)
            need = float(px or 0.0) * size / float(leg.mcfg.get("leverage", 3) or 3)
            pos = {k: v.get("s") for k, v in (snap.get("positions") or {}).items()}
            log(f"   余力 ${bal:,.2f} / 必要証拠金 ${need:,.2f} "
                f"({'不足' if bal < need else '足りている'}) / 建玉 {pos or 'なし'}")
        except Exception:
            pass

    def _sweep_retired_perpl_positions(self) -> None:
        """**もう構築していない** perpl 脚に建玉が残っていたら畳む(2026-07-29)。

        ## なぜ要るか — 実際に踏んだ形
        `_startup_reconcile` は `self.legs` をループするが、`hedge_leg_enabled: false` に
        すると ETH 脚は**そもそも構築されない**。上のコメントは「この関数が ETH の唯一の
        掃除役」と書いているが、それは脚が生きている間だけ成立する。**脚を退役させた
        瞬間に掃除役ごと消える**。

        実害(2026-07-29): ヘッジ脚を切った再起動で ETH ショート 0.070 が取り残され、
        証拠金 $44.56 を握ったまま **11時間半** 放置された。余力が $66.26 まで削られ、
        BTC lead $200 の必要証拠金 $66.67 を **41セント** 下回って発注が通らなくなり、
        見送り11連続・240秒バックオフで実質停止していた。
        症状として見えたのは「約定率が低い」だけで、原因を示すものは何も出ていない。

        ★[[xvenue-partial-fill-deadlock-2026-07-25]] の「ZEC 孤児建玉19時間裸(銘柄変更で
          恒久孤児化)」と同型。**設定で脚を減らす変更は、その脚の建玉の始末を伴う。**

        口座スナップショット1回で全 market の建玉を見る(脚を構築しないので板WSも張らない)。
        fail-open: 読めなければ何もしない(掃除できないことを理由に稼働を止めない)。"""
        if self.dry_run:
            return
        live_ids = {lg.market_id for lg in self.legs.values()}
        by_id = {int(m["market_id"]): (s, m) for s, m in _PERPL_MARKETS.items()}
        try:
            snap = self.lead_leg.exec._client.get_snapshot()
            positions = snap.get("positions") or {}
        except Exception as e:
            log(f"⚠️ 退役脚の建玉スイープ: 口座スナップショットを読めず({repr(e)[:60]})=見送り")
            return
        for mid_s, pos in positions.items():
            mid = int(mid_s)
            if mid in live_ids:
                continue                      # 生きている脚は上のループが見る
            if mid not in by_id:
                log(f"⚠️ 未知の perpl market_id={mid} に建玉がある(この bot の管理外)。手動確認要")
                continue
            sym, mcfg = by_id[mid]
            try:
                sz = _pe.scaled_to_size(int(pos["s"]), int(mcfg["size_decimals"]))
                if sz <= 1e-8:
                    continue
                md = _pe.PerplMarketData(f"perpl:{sym}", self.lead_leg.exec._client, dict(mcfg))
                ex = _pe.PerplExecutor(md, self.lead_leg.exec._client)
                was_buy = pos.get("sd") == 1                      # 1=Long → 売りで畳む
                r = ex.place_order(not was_buy, sz, "retired_leg_cleanup", reduce_only=True)
                px = (r or {}).get("price")
                log(f"⚠️ 退役した perpl:{sym} の孤児建玉 {sz} を畳んだ(px={px})。"
                    f"証拠金を握り続けて lead の発注を止める原因になる")
            except Exception as e:
                log(f"⚠️ 退役脚 perpl:{sym} の建玉を畳めず({repr(e)[:60]})。手動フラット化要")

    def _venues_flat(self) -> bool:
        """両会場の対象銘柄がフラットか(fail-closed: 読めなければFalse=フラット未確認)。
        ★全 perpl 脚を見る(2026-07-27)。まず常駐WSで読み、判定不能な脚だけ REST に落とす
        (素直に脚数ぶん _fetch_position を叩くと REST コストが脚数倍になる)。"""
        try:
            for leg in self.legs.values():
                szi = self._pp_feed_filled(leg)      # 常駐WS(API コストゼロ)
                if szi is None:                      # 判定不能 → fail-closed な REST へ
                    if leg.exec._fetch_position() is not None:
                        return False
                elif szi > 1e-8:
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

    def _margin_cap(self, lead_notional: float, tx_notional: float) -> float:
        """perpl 口座の証拠金で lead 名目を頭打ちにする(2026-07-29)。

        ## なぜ要るか — 2026-07-29 に実際に踏んだ形
        txflow を退役させた結果、ETH のヘッジ対象が残差$40 から **lead 全額$190** になった。
        perpl は leverage 3x なので、hold 中は lead と hedge の両方の証拠金が同時に要る:

            必要証拠金 = (lead + hedge) / 3 = 2 × 190 / 3 = $126.7   vs equity $127.4

        余裕 $0.7。**ETH の発注が全部 sr=44 で拒否され、follow が 100% 不成立**になった。
        観測できたのは「出来高が $765→$383 に半減」と「効率が変わらない」だけで、
        原因を示すものは 60字で切られたログしか無かった。★口座が痩せると発注が黙って
        拒否されるだけなので、**名目は equity から導け。固定値で書くな**。

        ## 上限の出し方 — **同時に持つ perpl 脚の数で式が変わる**
        perpl が hold 中に同時に持つのは lead と hedge(≈ lead - tx)。

            ヘッジ脚あり  equity×使用率 ≥ (lead + lead - tx)/lev → lead ≤ (eq×lev×使用率 + tx)/2
            ヘッジ脚なし  equity×使用率 ≥ lead/lev              → lead ≤  eq×lev×使用率

        ★2脚の式を1脚構成に当てたまま名目を上げると、上限が**半分**のまま張り付いて
          「上げたのに出来高が増えない」になる。脚の数を変えたら式も変えること
          ([[xvenue-margin-ceiling-silent-2026-07-29]] と同じ「脚を変えたら証拠金を再計算」)。

        使用率は既定 0.75 — 残りは手数料・含み損・マーク変動の緩衝。
        fail-open: equity が読めない/古いときは config の値をそのまま使う
        (読めないことを理由に出来高farmを止めない = equity_floor_reason と同じ方針)。"""
        util = float(self.cfg.get("perpl_margin_utilization", 0.75) or 0)
        if util <= 0:
            return lead_notional
        eq = _pag.current_equity_usd()
        if eq is None or eq <= 0:
            return lead_notional
        # ★`market.leverage` は存在しない(PerplMarketData が持つのは leverage_hundredths)。
        #   _PERPL_MARKETS の生 config を持つ mcfg が正。
        lev = float(self.lead_leg.mcfg.get("leverage", 0) or 0)
        if lev <= 0:
            return lead_notional
        budget = eq * lev * util
        cap = ((budget + tx_notional) / 2.0) if self.hedge_leg is not None else budget
        if cap >= lead_notional:
            return lead_notional
        if cap != getattr(self, "_last_margin_cap", None):
            log(f"⚠️ 証拠金でlead名目を制限: ${lead_notional:.0f} → ${cap:.0f} "
                f"(perpl equity ${eq:.2f} × lev {lev:.0f} × 使用率 {util:.0%})")
            self._last_margin_cap = cap
        return cap

    def _plan_sizes(self, t_bid: float, t_ask: float) -> dict:
        """1サイクルの脚別サイズを決める(2026-07-27 C5)。

        3脚構成:
            perpl BTC  +size_lead     txflow BTC -size_tx   → BTC 残差 = lead - tx
            perpl ETH  -size_eth                            → 残差を ETH で中立化

        ★ETH サイズは config の名目($130 等)から直接引かない。**丸め後の BTC 残差**から出す。
          size_decimals が会場・銘柄で違う(txflow BTC=4 / perpl BTC=5 / perpl ETH=3)ため、
          名目から引くと丸め後の実残差とズレて、その差がそのまま裸デルタになる。

        hedge_leg_enabled=false、または lead==txflow(残差0)のときは ETH 脚を持たない
        = **現行2脚と厳密に等価**。カナリアのレバーは lead_notional_usd そのもの。"""
        btc_mid = (t_bid + t_ask) / 2
        # ★txflow 脚を切る(2026-07-29): txflow の証拠金が床($55)を割って自己ハルトしたため、
        #   ユーザー判断で txflow を退役。残るのは **perpl BTC + perpl ETH** = 2026-07-22 に
        #   効率 8,978 を出した構成そのもの。txflow 名目を 0 にすると残差 = lead 全額となり、
        #   ETH が lead を丸ごとヘッジする(下の resid_sz 計算がそのまま成立する)。
        tx_notional = float(self.notional) if self.cfg.get("txflow_leg_enabled", True) else 0.0
        # ★サイズ A/B(2026-07-29)。$139(2脚)→$200(1脚) で lead 約定率が 57%→32% に落ちた。
        #   サイズを上げるほど出来高が増えるという前提が成立していない = **最適サイズが
        #   現行の内側にある可能性**がある。ただし $139 と $200 は構造(ETH脚の有無)も違うので
        #   弾性の推定に使えない。同一構造・同一時間帯で並走させないと最適点は出ない。
        #   ★腕は `self.attempts`(見送り込み)で振る。`self.cycles` で振ると偏る(上の注記参照)。
        _lab = self.cfg.get("lead_notional_ab") or []
        if _lab:
            # ★**乱択で振る**(2026-07-29 に決定論を捨てた)。当初 attempts % len で交互に
            #   振ったところ、方向と交絡した:
            #       $170 → perpl SELL 72% / $200 → perpl SELL 37%
            #   腕は**試行ごと**に交代するのに `dir_buy` は**完走時にだけ**反転するので、
            #   進み方の違う2つのカウンタが相関した。トレンド中は板の片側だけよく埋まるため、
            #   これは約定率に直撃する(実際 $170 41% vs $200 64% と、前の窓と逆に出た)。
            #   決定論的な割り当ては「自分が気づいていない周期」と必ず同期しうる。
            #   乱択なら未知の周期に対しても直交する。
            lead_notional = float(self._rng.choice(_lab))
            self._ln_ab = lead_notional
            # ★腕の組も残す(2026-07-29)。腕を差し替えた前後の行を混ぜると、**残した腕にだけ
            #   相手不在の時間の行が乗る**(実際 [170,230]→[170,200] で $170 が該当した)。
            #   集計は「最新の組の行だけ」を使う。
            # ★タグに**割り当て方式**も入れる(2026-07-29)。腕の組が同じでも方式が変われば
            #   前の窓のデータは使えない(交互→乱択で dir_buy との交絡が消えたため)。
            #   組だけをタグにしていると、交絡済みの行が静かに混ざる。
            self._ln_ab_arms = "/".join(f"{float(a):.0f}" for a in _lab) + "@rnd"
        else:
            lead_notional = float(self.cfg.get("lead_notional_usd", tx_notional) or tx_notional)
            self._ln_ab = None                   # A/B 停止中 = 腕の割り当てではない
        if lead_notional < tx_notional:
            lead_notional = tx_notional          # lead < txflow は設計外(残差が負になる)
        lead_notional = self._margin_cap(lead_notional, tx_notional)
        size_lead = round(lead_notional / btc_mid, self._size_round)
        size_tx = round(tx_notional / btc_mid, self._size_round)
        out = {"btc_mid": btc_mid, "size_lead": size_lead, "size_tx": size_tx,
               "size_eth": 0.0, "resid_usd": 0.0, "eth_mid": None}
        if self.hedge_leg is None:
            return out
        resid_sz = round(size_lead - size_tx, self._size_round)
        if resid_sz <= 0:
            return out                            # 残差なし=2脚と等価
        resid_usd = resid_sz * btc_mid
        e_bid, e_ask = self._perpl_bbo(self.hedge_leg)
        eth_mid = (e_bid + e_ask) / 2
        hr = float(self.cfg.get("hedge_ratio", 1.0) or 1.0)
        size_eth = round(resid_usd * hr / eth_mid, self._sr_hedge)
        out.update(size_eth=size_eth, resid_usd=resid_usd, eth_mid=eth_mid)
        return out

    def _perpl_hedge_follow(self, is_buy: bool, size: float):
        """perpl ETH ヘッジ脚の follow。maker 試行 → 残りを taker で埋めて**必ず完成させる**。
        戻り (filled, open_px, got_sz, took, abort_fill)。

        ★独自の発注ループを書かないこと。`_perpl_maker_lead` には取消×約定レース防御・
          部分約定 grace・timeout 時の部分建玉回収が全部入っており、書き直せば
          2026-07-25 の事故(建玉2倍化・6.5時間停止)が確実に再発する。違いは timeout と
          keep_partial だけ。

        ## なぜ taker フォールバックが要るか(2026-07-28)
        maker 一本槍だと **ETH follow が 40%(49/122)不成立**で、その全部が hold に入らず
        即クローズ = 狙っている OI が4割の周回で丸ごと消えていた。内訳は
        「0.001/0.02(=5%)の部分約定を grace 5秒で見切って畳む」が 23、素の timeout が 26。

        コスト差は maker 0.9bps → taker 6.9bps の 6bps。ヘッジ対象は lead-txflow の**残差
        $40 だけ**なので 1サイクルあたり **$0.023**。一方でこれが埋まれば ETH 脚ぶんの
        出来高(~$78/cycle)と hold 全長の OI が戻る。**残差 $40 を maker で粘る価値は無い**
        (節約できるのは 6bps×$40 = 2.3セント、失うのは1サイクルぶんの hold)。"""
        if self.hedge_leg is None or size <= 0:
            return False, None, 0.0, False, None
        leg = self.hedge_leg
        t = float(self.cfg.get("perpl_hedge_timeout_seconds", 120))
        # ★A/B(2026-07-29): txflow 退役でヘッジ対象が残差$40→lead全額$190 になり、上の
        #   「$0.023 だから taker で埋めろ」の前提が崩れた($0.132)。しかし maker 一本槍に
        #   戻したら **ETH が埋まらず 2leg_eth_abort が連発**した(出来高が半減)。
        #   abort は手数料こそ安いが lead $190 を裸で持つ方向賭けになる
        #   ([[pair-hedge-loss-anatomy-2026-07-22]]「損失の71%が価格・その71%がabort由来」)。
        #   どちらが得かは**手数料と価格の綱引きなので机上では決まらない** → 周回パリティで振る。
        #   集計は `hedge_taker_ab` 列を持つ行だけ。A/B 停止中は列を書かない(join offset と同規約)。
        _tab = self.cfg.get("perpl_hedge_taker_fallback_ab") or []
        if _tab:
            use_taker = bool(_tab[self.cycles % len(_tab)])
            self._tk_ab = use_taker
        else:
            use_taker = bool(self.cfg.get("perpl_hedge_taker_fallback", True))
            self._tk_ab = None
        # ★keep_partial は **常に True**(2026-07-29)。use_taker=False のときも部分建玉を
        #   自分で畳んでコストを台帳に持ち帰るため。`_perpl_maker_lead` 内の
        #   `_pp_partial_abort` に任せると unwind の約定を捨ててしまい、2026-07-28 に塞いだ
        #   「abort コストが台帳にも口座ガードにも1ドルも出ない」穴が再発する。
        ok, px, got = self._perpl_maker_lead(leg, is_buy, size, timeout_s=t, keep_partial=True)
        if ok:
            return True, px, size, False, None
        if not use_taker:
            # maker 一本槍(= 2026-07-22 に効率 8,978 を出した pair_hedge と同じ経済)。
            # 部分建玉は畳み、そのコストを abort dict で返す。
            if got and got > 1e-8:
                ab = self._perpl_unwind(leg, is_buy, got)
                self.dirty = True
                return False, px, got, False, {"px": px, "sz": got, "unwind": ab}
            return False, None, 0.0, False, None
        if got is None:      # 建玉が読めない(429等)。taker で上塗りすると過剰ヘッジになる
            log(f"⚠️ {leg.name} hedge: maker 後の建玉が判定不能 → taker を打たない(fail-closed)")
            self.dirty = True
            return False, None, 0.0, False, None
        remain = leg.market.round_size(size - got) if got > 1e-8 else size
        if remain <= 1e-8:                       # 丸めで残りが消えた=makerぶんで完成扱い
            return (got > 1e-8), px, got, False, None
        try:
            r = leg.exec.place_order(is_buy, remain, "hedge_taker", reduce_only=False)
        except Exception as e:
            # taker も埋まらない。maker 部分建玉が裸で残るので必ず畳む(fail-closed)。
            # ★切り詰めない(2026-07-29)。60字だと `perpl order rq=17…` で rq の数字に食われ、
            #   拒否理由(証拠金不足なのか sr コードなのか)が**一切読めなかった**。
            #   例外の全文を出すこと — 診断できないログはログでない。
            log(f"⚠️ {leg.name} taker フォールバック失敗={e}")
            ab = self._perpl_unwind(leg, is_buy, got) if got > 1e-8 else None
            self.dirty = True
            return False, px, got, False, {"px": px, "sz": got, "unwind": ab}
        tpx = float(r.get("price") or r.get("px") or 0.0)
        if tpx <= 0:
            tpx = px if px else 0.0
        total = got + remain
        # open 価格は maker ぶんと taker ぶんの**約定加重平均**。片方の価格で全量を計上すると
        # 損益が捏造になる(2026-07-25 の close 価格捏造と同じ穴)。
        blend = ((px * got + tpx * remain) / total) if (px and got > 1e-8) else tpx
        log(f"{leg.name} hedge: maker {got}/{size} → taker {remain} 追加(px={tpx}) 完成")
        return True, blend, total, True, None

    def _run_cycle_live(self, dir_buy: bool) -> dict:
        """改善①②③(2026-07-23): perpl maker を先行(patient+requote)、txflow を追従(maker→taker)。
        perpl maker率↑・追従taker落ちしても安いtxflow(4.5)<perpl(6.9)。close時perpl reduce-only。fail-closed。
        (旧: txflow先行→perpl追従はperpl taker落ち76%)。

        ★2026-07-27 C5: 3脚化。perpl BTC を txflow より大きく建て、残差を perpl ETH で相殺する。
          脚順は **逐次**(perpl BTC lead → txflow → perpl ETH)。並行発注はしない —
          txflow follow は ~8秒 / perpl ETH follow は中央値50秒でレイテンシが桁違いなので
          並行化しても全体の6%しか縮まらず、perpl のトークンバケットが実質の直列化装置になる。
          ETH が不成立なら **全畳み**(3脚とも即クローズ)。hold には入らない。"""
        # ★試行カウンタ(2026-07-29)。A/B の腕は **`self.cycles` で振ってはいけない** —
        #   あれは完走したサイクルしか加算しないので、約定しにくい腕ほど同じ腕を連続で
        #   引き続け、試行回数が腕間で偏る(サイズ A/B では「大きい腕ほど試行が増える」)。
        #   見送りも含めて数える別カウンタで振ること。
        self.attempts = getattr(self, "attempts", 0) + 1
        lt = float(self.cfg["leg_timeout_seconds"])
        poll = float(self.cfg["poll_interval_seconds"])
        t_bid, t_ask = self._txflow_bbo()
        plan = self._plan_sizes(t_bid, t_ask)
        size = plan["size_tx"]                    # txflow 脚のサイズ(以降の txflow ロジックは不変)
        size_lead = plan["size_lead"]             # perpl BTC lead のサイズ(2脚時は size と同じ)

        # === OPEN: perpl maker LEAD(patient+requote) ===
        perpl_is_buy = not dir_buy
        pp_filled, pp_open_px, _ = self._perpl_maker_lead(self.lead_leg, perpl_is_buy, size_lead)
        # ★建玉が立った時刻を残す(2026-07-29)。台帳は `ts`(サイクル終了)しか持っておらず、
        #   保有時間を出すのに毎回 perpl の fills を叩く必要があった(=429 の種)。
        #   約定検知の時刻なので実約定とは poll 間隔ぶんズレるが、秒オーダーの比較には足りる。
        pp_open_ts = time.time()
        if not pp_filled:
            # ★見送り行にも腕を残す。約定率は 完走/(完走+見送り) なので、**見送りを腕別に
            #   数えられないと分母が作れない**(台帳には完走しか載らない)。
            _lab = getattr(self, "_ln_ab", None)
            if _lab is not None:
                log(f"cycle見送り(ab=${_lab:.0f} arms={getattr(self, '_ln_ab_arms', '')})")
            # ★部分約定→畳みが起きていたら、その往復のコストと出来高を持ち帰る。
            #   捨てると台帳にも account_guard にも1ドルも出ない(見送りは行を書かない)。
            ab = getattr(self, "_lead_abort", None)
            self._lead_abort = None
            if ab:
                return {"skip": "perpl_lead_partial_unwound", **ab}
            return {"skip": "perpl_lead_no_fill"}   # 建玉なし=安全に見送り

        # perpl約定=裸perpl。txflow FOLLOWSでヘッジ。【hybrid: 短時間maker試行→taker】(2026-07-24)。
        # perpl裸窓中にtxflow makerを最大 follow_maker_try_seconds 試す(刺されば1.5bps=taker4.5より3bps安)。
        # 刺さらなければtakerで確実に裸窓を閉じる。裸窓は最大try秒(~3s)に延びる代償で手数料を削る。
        tx_is_buy = dir_buy
        htry = float(self.cfg.get("follow_maker_try_seconds", 3.0))
        frq = float(self.cfg.get("follow_maker_requote_seconds", 1.5))
        # ★txflow 退役(2026-07-29): size==0 なら txflow に一切触らない。以降の txflow ブロックは
        #   すべて `if tx_on:` で括る。size 0 で発注すると reject/例外になるので**必ずガードする**。
        tx_on = size > 1e-12
        if tx_on:
            t_bid, t_ask = self._txflow_bbo()
            tx_fill, tx_taker, mpx = None, False, (t_bid if tx_is_buy else t_ask)
        else:
            # 以降の損益/出来高計算がそのまま通るように**中立な0約定**を入れる
            # (px=0・sz=0 なので pnl も volume も 0 に落ちる)。
            tx_fill = {"px": 0.0, "sz": 0.0, "fee": 0.0}
            tx_taker, mpx = False, 0.0

        # --- ① 短時間 maker試行(post_only + requote。建玉で裏取り) ---
        # ★取消×約定レース防御(2026-07-25。pair_hedge が 2026-07-12 の事故で入れた多層防御の移植):
        #   旧コードは cancel を try/except:pass で撃ちっぱなしにし、**空振りでも ident を捨てて
        #   次の reduce_only=False を置いていた**。両方刺さると建玉2倍=片方が裸デルタになる
        #   (07-25 17:36 に `startup_reconcile: txflow 0.0046 をフラット化`=2×size で実観測)。
        #   不変条件を2つ課す: (a)生きた指値を同時に2本持たない (b)過去identの約定も必ず回収する。
        # ★tx_on=False のとき hdl=0 で maker ループが1度も回らない。後続の `if not tx_fill:`
        #   (残resting取消 / taker フォールバック)は tx_fill に 0約定 dict が入っているので
        #   truthy = 自動的にスキップされる。**ブロックを丸ごとインデントし直さないための構造**。
        ident, last_place = None, 0.0
        hdl = (time.time() + htry) if tx_on else 0.0
        open_idents = []          # このサイクルでopenに使った全ident(取消空振り分の回収用)
        cancel_pending = False    # 取消を送ったが板から消えたことを未確認=**再発注してはいけない**
        # ★join offset(2026-07-28): 板の内側に置く(_tx_join_px の docstring 参照)。
        #   post_only に拒否されたら **その場で touch(offset 0)へ落として粘る** — いきなり
        #   taker に落とすと、直そうとしている taker 率を自分で上げてしまう。
        # ★A/B(2026-07-28): txflow の taker 率は**時間帯で 25〜75% も振れる**(07-28 実測の
        #   時間別)。前後比較ではこの分散に効果が埋もれる — 実際 join=+1tick の投入直後 n=16 は
        #   43.4%→68.8% と逆に出たが、投入時刻がもともと 75% の時間帯だった。
        #   **同一時間帯で交互に振る**ことでのみ切り分けられる。集計は `join_offset_ab` 列を持つ
        #   行だけで行うこと([[pair-hedge-ab-null-and-broken-baseline]] と同じ規約)。
        # ★ヘッジ脚の A/B 腕は毎周回リセットする。`_perpl_hedge_follow` が呼ばれない
        #   周回(ヘッジ脚無効など)で前周回の腕が残ると、台帳に嘘の割り当てが載る。
        self._tk_ab = None
        _ab = self.cfg.get("follow_join_offset_ab") or []
        if _ab:
            joff = int(_ab[self.cycles % len(_ab)])
            # 台帳に残す腕(途中で touch へ落ちても「割り当て」を記録する)。
            # ★A/B **稼働中の行だけ**に付けること。固定運用に戻したあとも書き続けると、
            #   採用した腕だけが増えて他方が凍り、`join_offset_ab` で集計する
            #   scripts/ab_join_offset.py が**静かに偏る**(A/B 打ち切り後の実害)。
            joff_used = joff
        else:
            joff = int(self.cfg.get("follow_join_offset_ticks", 0) or 0)
            joff_used = None      # A/B 停止中 = 腕の割り当てではない
        while time.time() < hdl:
            if ident is None and not cancel_pending:
                t_bid, t_ask = self._txflow_bbo()
                mpx = self._tx_join_px(tx_is_buy, t_bid, t_ask, joff)
                ident = self._tx_place_maker(tx_is_buy, mpx, size, reduce_only=False)
                last_place = time.time()
                if ident is None:
                    if joff > 0:                         # 内側が拒否された→touchで置き直す
                        log(f"txflow open: join+{joff}tick が post_only 拒否 → touch へ落として継続")
                        joff = 0
                        continue
                    break                                # touch でも拒否→takerへ
                open_idents.append(ident)
            # 約定回収は**現行identだけでなく過去identも**見る(取消が空振りして古い方が刺さる)。
            for cand in reversed(open_idents):
                fl = self._tx_fill(cand, size)
                if fl:
                    tx_fill = fl
                    break
            if tx_fill:
                break
            try:
                # ★0.5 では半分しか刺さっていなくても「全量約定」として size を計上していた
                #   (2026-07-27 N-1修正)。perpl は size 全量をヘッジ済みなので、差分がそのまま
                #   hold 中ずっと裸デルタになる。perpl 側の判定(0.999)と揃える。未達なら下の
                #   taker フォールバックが remaining だけを埋める。
                pos_m = abs(self._tx_position())
                if pos_m >= size * 0.999:                # fills遅延→建玉で裏取り
                    tx_fill = {"px": mpx, "sz": pos_m,
                               "fee": self.notional * self.fees["txflow_maker_bps"] / 1e4}
                    ident = None; break
            except Exception:
                pass
            if ident is not None and not cancel_pending and time.time() - last_place >= frq:
                # ★**touch が動いたときだけ**置き直す(2026-07-28)。
                #   旧コードは経過時間だけで無条件に cancel→再発注していた。価格が動いていなくても
                #   1.5秒ごとに板から降りて列の最後尾に並び直す = キュー優先度を捨て続ける。
                #   6秒窓なら4回並び直すので実質どの瞬間も最後尾で、maker が刺さらず taker に
                #   落ちる。perpl lead 側(_perpl_maker_lead)は `if new_px != px:` を持っているのに
                #   txflow 側の2ループだけ欠落していた。実測 taker落ち 49.6%・損失の70%が手数料。
                nb_r, na_r = self._txflow_bbo()
                # ★join offset 込みで比較する。自分が最良気配になっている間は BBO が自分の値を
                #   返すので new_mpx == mpx となり、並び直さずキュー先頭を保てる。
                new_mpx = self._tx_join_px(tx_is_buy, nb_r, na_r, joff)
                if new_mpx == mpx:
                    last_place = time.time()      # touch 不動 → 並び直さずそのまま待つ
                    time.sleep(0.5)
                    continue
                # ★ident が cloid dict(openOrders に1.8s出てこなかった)のときは oid が無く
                #   cancel_order を呼べない。旧コードは例外を握り潰したまま再発注へ進み、
                #   板に載っていた場合に2本同時生存=建玉2倍になった(2026-07-27修正)。
                oid_c = self._tx_resolve_oid(ident)
                if oid_c is None:
                    log("txflow open: identのoid未解決(cloid未反映)=requoteせず約定回収に委ねる")
                else:
                    try:                                 # touch移動→置き直し(まず取消を送る)
                        self.tx.cancel_order(self.symbol, oid_c)
                    except Exception:
                        pass
                cancel_pending = True                    # 消えたと**確認できるまで**次を置かない
            if cancel_pending:
                if self._tx_order_alive(ident):
                    log(f"txflow open: 取消未成立(oid={ident})=再発注せず次tickで再確認")
                else:
                    # 板から消えた。**約定していないことを確認してから**置き直す
                    # (消えた理由が「約定」なら再発注はそのまま建玉2倍になる)。
                    fl_gone = self._tx_fill(ident, size)
                    if fl_gone:
                        tx_fill = fl_gone
                        break
                    ident, cancel_pending = None, False   # 未約定で消えた=安全に置き直せる
            time.sleep(0.5)

        # --- ② maker不成立 → taker で確実ヘッジ(裸窓を閉じる) ---
        if not tx_fill:
            # 残resting指値を取消し、**消えたことを確認**してから残量を測る(生きたままだと
            # taker と同時に刺さって2倍化する)。
            if isinstance(ident, int):
                try:
                    self.tx.cancel_order(self.symbol, ident)
                except Exception:
                    pass
                for _ in range(6):                       # 取消反映を待つ(空振りなら生存し続ける)
                    if not self._tx_order_alive(ident):
                        break
                    time.sleep(0.5)
                else:
                    log(f"⚠️ txflow open: 取消未成立のまま(oid={ident})。残量計算は建玉で行う")
            # 取消の直前に刺さっていた可能性を必ず回収する(過去ident全部)。
            for cand in reversed(open_idents):
                fl = self._tx_fill(cand, size)
                if fl:
                    tx_fill = fl
                    break
        if not tx_fill:
            # ★全量IOCではなく**残量だけ**を撃つ。旧コードは既約定分の上に size を積んでいた
            #   (maker が刺さっていたのに fills 未反映だと 2×size になる)。
            pos0 = 0.0
            try:
                pos0 = abs(self._tx_position())
            except Exception:
                pass
            remaining = round(size - pos0, self._size_round)
            t_bid, t_ask = self._txflow_bbo()
            tx_touch = t_ask if tx_is_buy else t_bid
            if remaining <= size * 0.05:                 # 既に埋まっている=takerを撃たない
                log(f"txflow open: 既に建玉{pos0}(≒size)=taker不要。maker約定として計上")
                tx_fill = {"px": mpx, "sz": pos0,
                           "fee": self.notional * self.fees["txflow_maker_bps"] / 1e4}
            else:
                tx_px = self._tx_marketable_px(tx_is_buy, t_bid, t_ask)  # 板移動許容バッファ付き
                tx_taker = True
                try:
                    self.tx.place_limit_order(self.symbol, tx_is_buy, tx_px, remaining,
                                              reduce_only=False, tif=self.tx.TIF_IOC)
                except Exception as e:
                    log(f"⚠️ txflow追従taker失敗({repr(e)[:50]})")
                pos = 0.0
                for _ in range(6):                      # 建玉反映ラグ吸収(偽hedge_fail防止)
                    time.sleep(0.5)
                    try:
                        pos = self._tx_position()
                    except Exception:
                        continue
                    if abs(pos) >= size * 0.999:
                        break
                # ★部分約定なら残量をもう一度だけ taker で埋める(2026-07-27 N-1)。旧コードは
                #   size*0.5 で「ヘッジ成立」と見なしており、残り最大50%が hold 中ずっと裸だった。
                if 1e-8 < abs(pos) < size * 0.999:
                    rem2 = round(size - abs(pos), self._size_round)
                    if rem2 > 0:
                        log(f"txflow追従taker 部分約定 {abs(pos)}/{size} → 残{rem2}を再taker")
                        try:
                            t_b2, t_a2 = self._txflow_bbo()
                            self.tx.place_limit_order(
                                self.symbol, tx_is_buy,
                                self._tx_marketable_px(tx_is_buy, t_b2, t_a2),
                                rem2, reduce_only=False, tif=self.tx.TIF_IOC)
                        except Exception as e:
                            log(f"⚠️ txflow追従taker再送失敗({repr(e)[:50]})")
                        for _ in range(6):
                            time.sleep(0.5)
                            try:
                                pos = self._tx_position()
                            except Exception:
                                continue
                            if abs(pos) >= size * 0.999:
                                break
                if abs(pos) < size * 0.999:             # ヘッジ不成立→両脚を畳む(裸回避)
                    log(f"⚠️ txflowヘッジ不成立({abs(pos)}/{size})→両脚をunwind(裸回避)")
                    if abs(pos) > 1e-8:
                        self._tx_unwind(tx_is_buy, abs(pos))   # txflow の部分建玉も残さない
                    self._perpl_unwind(self.lead_leg, perpl_is_buy, size)
                    self.dirty = True                    # 裸残の疑い→次ループでflatten確認
                    # ★中断コストを台帳に持ち帰る(2026-07-27 D-5)。perpl unwind は reduce-only
                    #   taker、txflow の畳みも taker。旧コードは skip をそのまま返しており、
                    #   これらが cum_net に入らなかった=account_guard も同じだけ過小評価していた。
                    ab_fee = self.notional * self.fees["perpl_taker_bps"] / 1e4
                    if abs(pos) > 1e-8:
                        ab_fee += self.notional * (abs(pos) / size) * self.fees["txflow_taker_bps"] / 1e4
                    return {"skip": "hedge_failed_unwound", "abort_fees_usd": ab_fee}
                # maker約定分(pos0)と taker約定分(残量)の混成なので手数料も按分する。
                mk = self.notional * (pos0 / size) * self.fees["txflow_maker_bps"] / 1e4
                tk = self.notional * (min(remaining, abs(pos)) / size) * self.fees["txflow_taker_bps"] / 1e4
                tx_fill = {"px": tx_touch, "sz": abs(pos), "fee": mk + tk}
        # ★超過建玉ガード: 上の防御を抜けても size を超えていたら**その場で削る**
        #   (perpl脚は size しかヘッジしていないので、超過分はそのまま裸デルタになる)。
        try:
            pos_chk = self._tx_position() if tx_on else 0.0
        except Exception:
            pos_chk = 0.0
        excess = round(abs(pos_chk) - size, self._size_round)
        if tx_on and excess > size * 0.05:
            log(f"⚠️ txflow open: 建玉超過 {abs(pos_chk)} > size {size} → 超過{excess}をreduce-onlyで削る")
            try:
                t_b, t_a = self._txflow_bbo()
                self.tx.place_limit_order(self.symbol, pos_chk < 0,
                                          self._tx_marketable_px(pos_chk < 0, t_b, t_a),
                                          excess, reduce_only=True, tif=self.tx.TIF_IOC)
            except Exception as e:
                log(f"⚠️ txflow超過削り失敗({repr(e)[:50]})=次ループでflatten")
                self.dirty = True

        # === LEG3: perpl ETH follow(逐次。並行発注はしない) ===
        # ★hold タイマーを**ここより先に**確定させる。ETH follow は中央値50秒かかるので、
        #   これを hold の外に置くと1サイクルが 6分→7.5分に伸びて throughput が -20%。
        #   ETH は hold 時間の内側で刺しにいく。
        # ★hold A/B(2026-07-29)。「maker で約定した=価格が自分の逆へ動いた瞬間」なので、
        #   その不利が**戻るのか続くのか**で最適な保有時間が決まる。現在の価格コスト
        #   -0.28〜-1.38bps は保有 6秒(実測 中央値)時点の markout でしかない。
        #   ★これは「発注を待つ」のではなく「**建った建玉をいつ閉じるか**」なので、
        #     07-28 に効率を落とした follow_maker_try 延長とは性質が違う(裸窓は増えるが
        #     それは待ち時間そのもので、約定を待つ空振り時間ではない)。
        #   ★腕は乱択。決定論だと dir_buy(完走時にしか反転しない)と交絡する。
        _hab = self.cfg.get("hold_seconds_ab") or []
        if _hab:
            hold_s = float(self._rng.choice(_hab))
            self._hold_ab = hold_s
        else:
            hold_s = float(self.cfg["hold_seconds"])
            self._hold_ab = None
        hold_end = time.time() + hold_s
        eth_is_buy = not perpl_is_buy      # lead の残差(lead-txflow)を打ち消す向き
        size_eth = float(plan["size_eth"])
        eth_filled, eth_open_px, mode = False, None, "2leg"
        eth_taker, eth_abort = False, None
        if size_eth > 0:
            eth_filled, eth_open_px, got_eth, eth_taker, eth_abort = \
                self._perpl_hedge_follow(eth_is_buy, size_eth)
            if eth_filled:
                size_eth = got_eth          # ★実約定サイズで以降を回す(名目で計上すると台帳が汚れる)
                mode = "3leg_taker" if eth_taker else "3leg"
            else:
                # ★全畳み: hold に入らず即クローズして次サイクルへ(2026-07-27 決着)。
                #   「$150↔$150 の完全ヘッジが成立しているのだから hold すべき」は誤り —
                #   ヘッジ保持中は出来高を1ドルも生まず、1サイクルの出来高も手数料も
                #   縮小保持と**完全に同一**(open 280 + close 280 = open 280 + reduce 130 + close 150)。
                #   違いは hold 秒だけなので、低OI枝($300/s < 全体平均$317/s)を早く畳んで
                #   高OI枝($560/s)の抽選を回す方が NET・OI とも上。
                #   ※taker フォールバック導入後(2026-07-28)、ここに来るのは taker も失敗した
                #     ときだけ = 例外的経路。到達したら eth_abort にコストが入っている。
                mode = "2leg_eth_abort"
                log(f"perpl:ETH follow 不成立({size_eth}) → 全畳み(hold に入らず即クローズ)")
                hold_end = time.time()

        # === HOLD(全脚の超過建玉を定期監視) ===
        # ★open直後の超過ガードは1回しか走らず、取消×約定レースで2本目が数秒遅れて刺さると
        #   見逃していた(全ログで「建玉超過」の発火0件)。超過分は hold 中ずっと裸デルタになる。
        #   hold を延ばす方針(OI/保有時間を積む)では露出時間がそのまま伸びるため、hold 中も
        #   定期的に確認して削る(2026-07-27)。
        # ★3脚化で **perpl BTC lead も監視対象**にする。lead が2倍化すると lead ぶん丸ごと
        #   裸になる(txflow と ETH は元のサイズしかヘッジしていない)。2026-07-12 事故の直系。
        chk_s = float(self.cfg.get("hold_position_check_seconds", 30))
        _watch = [(None, size)] if tx_on else []      # (leg, 期待サイズ)。None=txflow
        _watch.append((self.lead_leg, size_lead))
        if eth_filled:
            _watch.append((self.hedge_leg, size_eth))
        while True:
            remain_h = hold_end - time.time()
            if remain_h <= 0:
                break
            time.sleep(min(chk_s, remain_h))
            if time.time() >= hold_end:
                break
            for _leg, _want in _watch:
                if _leg is None:                     # --- txflow ---
                    try:
                        pos_h = self._tx_position()
                    except Exception:
                        continue
                    ex_h = round(abs(pos_h) - _want, self._size_round)
                    if ex_h > _want * 0.05:
                        log(f"⚠️ hold中に txflow 建玉超過 {abs(pos_h)} > size {_want} → 超過{ex_h}を削る")
                        try:
                            t_bh, t_ah = self._txflow_bbo()
                            self.tx.place_limit_order(self.symbol, pos_h < 0,
                                                      self._tx_marketable_px(pos_h < 0, t_bh, t_ah),
                                                      ex_h, reduce_only=True, tif=self.tx.TIF_IOC)
                        except Exception as e:
                            log(f"⚠️ hold中の超過削り失敗({repr(e)[:50]})=次ループでflatten")
                            self.dirty = True
                    continue
                # --- perpl 各脚(常駐WS。API コストゼロ。判定不能なら今tickはスキップ) ---
                szi_h = self._pp_feed_filled(_leg)
                if szi_h is None:
                    continue
                ex_p = round(szi_h - _want, _leg.size_decimals)
                if ex_p > _want * 0.05:
                    log(f"⚠️ hold中に {_leg.name} 建玉超過 {szi_h} > size {_want} → 超過{ex_p}を削る")
                    try:
                        _is_buy = perpl_is_buy if _leg is self.lead_leg else eth_is_buy
                        _leg.exec.place_order(not _is_buy, ex_p, "trim", reduce_only=True)
                    except Exception as e:
                        log(f"⚠️ hold中の {_leg.name} 超過削り失敗({repr(e)[:50]})=次ループでflatten")
                        self.dirty = True

        # === CLOSE: txflow reduce(maker→taker) + perpl reduce-only ===
        tx_close_buy = not tx_is_buy
        crq = float(self.cfg.get("close_requote_seconds", 5))
        # ★txflow 退役時(tx_on=False)は板も引かない。dl=0 で close ループも回らず、
        #   tx_cfill に 0約定を入れて後続の `if not tx_cfill:` を全部スキップさせる。
        if tx_on:
            t_bid2, t_ask2 = self._txflow_bbo()
            tx_cpx = t_bid2 if tx_close_buy else t_ask2
            tx_cfill = None
        else:
            t_bid2 = t_ask2 = tx_cpx = 0.0
            tx_cfill = {"px": 0.0, "sz": 0.0, "fee": 0.0, "recovered": True}
        cid, last_place = None, 0.0
        dl = (time.time() + lt) if tx_on else 0.0
        # ★このサイクルでcloseに使った全ident(requoteで捨てた分も含む)。取消×約定レースで
        #   「取消したつもりの指値が約定していた」場合に実約定を回収するために必要
        #   (2026-07-25: 保持していなかったため19%のサイクルでclose価格を捏造していた)。
        close_idents = []
        while time.time() < dl:                        # closeもrequote(touch追随)でmaker取りこぼし改善
            if cid is None:
                t_bid2, t_ask2 = self._txflow_bbo()
                tx_cpx = t_bid2 if tx_close_buy else t_ask2
                cid = self._tx_place_maker(tx_close_buy, tx_cpx, size, reduce_only=True)
                last_place = time.time()
                if cid is None:                        # reduce_only拒否(既flat等)→taker/確認へ
                    break
                close_idents.append(cid)
            tx_cfill = self._tx_fill(cid, size)
            if tx_cfill:
                break
            if time.time() - last_place >= crq:        # touch移動→置き直し(maker約定率↑=taker落ち減)
                # ★open 側と同じく **touch が動いたときだけ**(2026-07-28)。コメントは元から
                #   「touch移動→」と書いてあったが、実装は経過時間だけの無条件 requote だった。
                nb_c, na_c = self._txflow_bbo()
                new_cpx = nb_c if tx_close_buy else na_c
                if new_cpx == tx_cpx:
                    last_place = time.time()      # 動いていない → 並び直さない
                else:
                    if isinstance(cid, int):
                        try:
                            self.tx.cancel_order(self.symbol, cid)
                        except Exception:
                            pass
                    cid = None
            time.sleep(poll)
        if not tx_cfill:  # taker強制close
            if isinstance(cid, int):
                try:
                    self.tx.cancel_order(self.symbol, cid)
                except Exception:
                    pass
            pos = self._tx_position()
            if abs(pos) > 1e-8:
                px = self._tx_marketable_px(pos < 0, t_bid2, t_ask2)  # long→sell/short→buy、確実約定
                touch = t_bid2 if pos > 0 else t_ask2
                self.tx.place_limit_order(self.symbol, pos < 0, px, abs(pos),
                                          reduce_only=True, tif=self.tx.TIF_IOC)
                tx_cfill = {"px": touch, "sz": abs(pos), "fee": self.notional * self.fees["txflow_taker_bps"] / 1e4}
            else:
                # 既にフラット = 置いた reduce-only maker のどれかが約定していた(取消×約定レース。
                # 「reduce_only拒否」ログの正体はこれ)。**実約定をfillsから回収する** — 以前は
                # px=現在touch / fee=0 を捏造しており、close価格の誤りとmaker手数料1.5bpsの
                # 計上漏れで net が甘く出ていた(実測: 全219サイクルの19%=42本が該当)。
                for ident in reversed(close_idents):
                    fl = self._tx_fill(ident, size)
                    if fl:
                        tx_cfill = fl
                        break
                if not tx_cfill:
                    # fills が追いつかない/identが取れなかった。**捏造せず maker 手数料を計上**し、
                    # 価格は最後のtouch近似のまま「回収失敗」を記録に残す(黙って甘い数字にしない)。
                    log("⚠️ txflow close: 既flatだが実約定を回収できず(px=touch近似・maker手数料で計上)")
                    tx_cfill = {"px": tx_cpx, "sz": size,
                                "fee": self.notional * self.fees["txflow_maker_bps"] / 1e4,
                                "recovered": False}
        # perpl reduce-only close(reduce≈無料・確実)
        # ★_pp_szi は fail-open(429で0.0)。これを「建玉なし」と読むと close を丸ごと飛ばした
        #   うえで pp_close_px に BBO 中値(架空値)を書く二重障害になっていた(2026-07-27 N-2)。
        #   方向は open 時点で確定しているので、読めないときも reduce_only で畳みにいく
        #   (reduce_only は建玉方向にしか約定しない=flat なら no-op なので安全)。
        pp_close_px, pp_close_ok = self._perpl_close_leg(self.lead_leg, perpl_is_buy, size_lead)
        eth_close_px, eth_close_ok = None, True
        if eth_filled:
            eth_close_px, eth_close_ok = self._perpl_close_leg(self.hedge_leg, eth_is_buy, size_eth)

        # === 損益(long=close-open / short=open-close) ===
        tx_o, tx_c = tx_fill["px"], tx_cfill["px"]
        tx_pnl = (tx_c - tx_o) * size if tx_is_buy else (tx_o - tx_c) * size
        pp_pnl = ((pp_close_px - pp_open_px) * size_lead if perpl_is_buy
                  else (pp_open_px - pp_close_px) * size_lead)
        tx_fee = tx_fill.get("fee", 0) + tx_cfill.get("fee", 0)
        _pp_bps = (self.fees["perpl_maker_bps"] + self.fees["perpl_close_bps"]) / 1e4
        pp_fee = pp_open_px * size_lead * _pp_bps          # lead=常にmaker。実約定notionalで計上
        # --- ETH ヘッジ脚(3脚時のみ) ---
        # ★open は maker とは限らない(2026-07-28 taker フォールバック)。taker を maker 料率で
        #   計上すると1サイクルあたり 6bps×$40 を台帳が隠す=口座ガードも過小評価する。
        _pp_taker_bps = (self.fees["perpl_taker_bps"] + self.fees["perpl_close_bps"]) / 1e4
        eth_pnl = eth_fee = 0.0
        if eth_filled:
            eth_pnl = ((eth_close_px - eth_open_px) * size_eth if eth_is_buy
                       else (eth_open_px - eth_close_px) * size_eth)
            eth_fee = eth_open_px * size_eth * (_pp_taker_bps if eth_taker else _pp_bps)
        # --- ヘッジ不成立で部分建玉を畳んだぶん(従来は台帳に1ドルも出ていなかった) ---
        ab_pnl = ab_fee = ab_vol = 0.0
        ab_leg = None
        if eth_abort and float(eth_abort.get("sz") or 0) > 1e-8:
            a_sz = float(eth_abort["sz"]); a_o = float(eth_abort.get("px") or 0.0)
            a_u = eth_abort.get("unwind") or {}
            a_c = float(a_u.get("price") or a_u.get("px") or 0.0) or a_o
            if a_o > 0:
                ab_pnl = ((a_c - a_o) * a_sz if eth_is_buy else (a_o - a_c) * a_sz)
                ab_fee = a_o * a_sz * _pp_bps      # open=maker、unwind=reduce_only(≈無料)
                ab_vol = (a_o + a_c) * a_sz
                ab_leg = {"venue": "perpl", "symbol": self.hedge_leg.symbol, "role": "hedge_abort",
                          "is_buy": eth_is_buy, "size": a_sz,
                          "open_px": round(a_o, self.hedge_leg.price_decimals),
                          "close_px": round(a_c, self.hedge_leg.price_decimals),
                          "notional": round(a_o * a_sz, 4),
                          "pnl": round(ab_pnl, 5), "fees_usd": round(ab_fee, 6),
                          "open_maker": True, "close_recovered": bool(a_u)}
        fees = tx_fee + pp_fee + eth_fee + ab_fee
        net = tx_pnl + pp_pnl + eth_pnl + ab_pnl - fees
        # ★出来高は**実約定**から積む(2026-07-27 D-4)。旧コードは `notional * 4` の名目固定で、
        #   部分約定と価格変動を無視して +1.40% 過大に出ていた(実測: 記録$333,935 vs 実額$329,338)。
        #   効率(出来高÷損失)の分子なので、甘い方向に 1.4% ずれ続けていた。
        tx_osz = float(tx_fill.get("sz") or size)
        tx_csz = float(tx_cfill.get("sz") or tx_osz)
        volume = ((tx_o * tx_osz + tx_c * tx_csz)
                  + (pp_open_px + pp_close_px) * size_lead
                  + ((eth_open_px + eth_close_px) * size_eth if eth_filled else 0.0)
                  + ab_vol)                        # abort 脚も**実際に売買している**=出来高
        # --- v1 互換ミラー(既存の読み手が壊れないように併記する) ---
        tx_mirror = {"open": tx_o, "close": tx_c, "pnl": round(tx_pnl, 5), "taker_follow": tx_taker,
                     # 実約定を回収できたか。False=close価格が touch 近似(2026-07-27: 旗自体は
                     # 作っていたのに記録dictに載せておらず、後から該当行を除外できなかった)。
                     "close_recovered": bool(tx_cfill.get("recovered", True))}
        # size/notional は脚別の実額。銘柄別・会場別の集計が全量約定を前提にしないためのもの
        # (2026-07-25: 部分約定を全量計上して台帳が汚れた実害の再発防止)。
        pp_mirror = {"open": round(pp_open_px, 1), "close": round(pp_close_px, 1),
                     # ★`taker_hedge` は **lead 脚(常に maker)**を指す v1 の遺物で、恒久 False。
                     #   ヘッジ脚が taker で埋まったかは v2 の `legs["perpl:ETH"].open_maker`
                     #   を見ること。名前に釣られてここを読むと「taker を一度も使っていない」と
                     #   誤読する(2026-07-29 に実際に誤読した)。
                     "pnl": round(pp_pnl, 5), "taker_hedge": False,
                     "size": size_lead, "notional": round(pp_open_px * size_lead, 4),
                     "close_recovered": pp_close_ok}
        # --- v2: 脚を明示的に持つ(2026-07-27)。読み手は src/xvenue_ledger.py 経由で正規化する ---
        # ★perpl 脚が複数になっても取りこぼさないための構造。v1 の `perpl` ミラーは lead 脚しか
        #   表せないので、ETH ヘッジ脚を足したときに口座ガードから損益が漏れる。
        legs = {
            f"perpl:{self.lead_leg.symbol}": {
                "venue": "perpl", "symbol": self.lead_leg.symbol, "role": "lead",
                "is_buy": perpl_is_buy, "size": size_lead,
                "open_px": round(pp_open_px, 1), "close_px": round(pp_close_px, 1),
                "notional": round(pp_open_px * size_lead, 4),
                "pnl": round(pp_pnl, 5), "fees_usd": round(pp_fee, 6),
                "open_maker": True, "close_recovered": pp_close_ok,
            },
        }
        # ★txflow 退役時は脚そのものを載せない(2026-07-29)。size 0 の脚を残すと会場別集計に
        #   出来高0の txflow 行が混ざり、taker 率や bps の分母が壊れる。
        if tx_on:
            legs[f"txflow:{self.symbol}"] = {
                "venue": "txflow", "symbol": self.symbol, "role": "follow",
                "is_buy": tx_is_buy, "size": tx_osz,
                "open_px": tx_o, "close_px": tx_c,
                "notional": round(tx_o * tx_osz, 4),
                "pnl": round(tx_pnl, 5), "fees_usd": round(tx_fee, 6),
                "open_maker": not tx_taker,
                "close_recovered": bool(tx_cfill.get("recovered", True)),
            }
        if eth_filled:
            legs[f"perpl:{self.hedge_leg.symbol}"] = {
                "venue": "perpl", "symbol": self.hedge_leg.symbol, "role": "hedge",
                "is_buy": eth_is_buy, "size": size_eth,
                "open_px": round(eth_open_px, self.hedge_leg.price_decimals),
                "close_px": round(eth_close_px, self.hedge_leg.price_decimals),
                "notional": round(eth_open_px * size_eth, 4),
                "pnl": round(eth_pnl, 5), "fees_usd": round(eth_fee, 6),
                "open_maker": not eth_taker, "close_recovered": eth_close_ok,
            }
        elif ab_leg is not None:
            legs[f"perpl:{self.hedge_leg.symbol}"] = ab_leg
        row = {
            "ts": round(time.time(), 3), "schema": 2, "mode": mode,
            "symbol": self.symbol, "dir_buy": dir_buy, "dry_run": False,
            "size": size, "notional_usd": self.notional,
            "size_lead": size_lead, "lead_notional_usd": round(pp_open_px * size_lead, 4),
            "resid_usd": round(plan["resid_usd"], 4),
            "legs": legs,
            "txflow": tx_mirror, "perpl": pp_mirror,
            "fees_usd": round(fees, 6), "volume_usd": round(volume, 4), "net_usd": round(net, 6),
        }
        # ★A/B 稼働中の行にだけ腕を書く。固定運用の行に書くと、採用した腕だけが増えて
        #   他方が凍り、`join_offset_ab` で集計する scripts/ab_join_offset.py が静かに偏る。
        if joff_used is not None:
            row["join_offset_ab"] = joff_used
        if getattr(self, "_tk_ab", None) is not None:
            row["hedge_taker_ab"] = self._tk_ab
        if getattr(self, "_ln_ab", None) is not None:
            row["lead_notional_ab"] = self._ln_ab
            row["lead_notional_ab_arms"] = getattr(self, "_ln_ab_arms", "")
        if getattr(self, "_hold_ab", None) is not None:
            row["hold_seconds_ab"] = self._hold_ab
        # 実保有秒。**指定した hold ではなく実測**を残す(close の約定にも時間がかかるので、
        # hold 0 でも実際は中央値6秒。指定値で markout を語ると系統的にズレる)。
        row["open_ts"] = round(pp_open_ts, 3)
        row["hold_actual_s"] = round(row["ts"] - pp_open_ts, 2)
        return row

    def _write_status(self) -> Optional[float]:
        """status.json を書き、効率(net<0のときのみ)を返す。

        ★_record からだけでなく**ハルト時にも呼ぶ** — 2026-07-25、_record 経由でしか書いていな
        かったため loss_budget ハルト後も `halted: false` のまま status が固まり、45分の停止を
        外から検知できずユーザーの目視で見つかった(cycleが止まる=statusも止まる、が盲点)。

        ★2026-07-26: **dirty 再試行ループ**も同じ盲点だった。_reconcile_dirty が perpl 429 で
        失敗し続ける間、この関数が呼ばれないので `halted: false` のまま updated_ts が最後の
        完走サイクル時刻で固まり「稼働中に見える停止」になる。呼び出しを追加したうえで、
        単に updated_ts を進めるだけでは鮮度監視をすり抜けるので `dirty` / `dirty_since_ts` を
        status に出し、外側(halt_monitor.py)がその継続時間で判定できるようにする。"""
        eff = (self.cum_volume / abs(self.cum_net)) if self.cum_net < 0 else None
        STATUS_PATH.write_text(json.dumps({
            "updated_ts": int(time.time()), "dry_run": self.dry_run, "halted": self.halted,
            "halted_reason": self.halted_reason,
            "cycles": self.cycles, "cum_volume_usd": round(self.cum_volume, 2),
            "cum_fees_usd": round(self.cum_fees, 4), "cum_net_usd": round(self.cum_net, 4),
            "efficiency": round(eff, 1) if eff else None,
            # 未フラット疑いで新規サイクルを止めている状態。halted とは別(自己修復しうる)。
            "dirty": self.dirty,
            "dirty_since_ts": int(self.dirty_since) if self.dirty and self.dirty_since else None,
            # txflow 口座残高(2026-07-27追加)。60秒キャッシュ経由なので status を書く頻度で
            # API を叩くことはない。外側の監視がこの1点だけで証拠金枯渇を見られるようにする。
            "txflow_equity_usd": (None if self.dry_run else self._tx_equity()),
        }, indent=2))
        return eff

    def _record_abort(self, reason: str, fees: float,
                      volume: float = 0.0, pnl: float = 0.0) -> None:
        """中断サイクルのコストを台帳に残す(2026-07-27 D-5)。net = pnl - 手数料。

        ★volume/pnl は **lead が部分約定して畳んだ**ときに渡す(2026-07-29)。あれは
          「発注しただけ」ではなく**実際に建てて実際に畳んだ往復**なので、会場から見れば
          出来高であり、価格差も現実に出ている。volume=0 固定のままだと出来高を過小、
          効率を過大に見積もる。

        旧コードは見送り/中断を台帳に一切書かず、unwind の taker 手数料が cum_net に入って
        いなかった(実測 10時間窓で $0.151 = 損失の1.3%)。account_guard_24h_usd も同じ台帳を
        読むので、**ガードが同じだけ過小評価**していた。
        ★`skip_reason` を持つ行は完走サイクルではない。集計側は cycles に数えないこと
        (_load_ledger と scripts/efficiency.py で除外済み)。"""
        rec = {"ts": round(time.time(), 3), "symbol": self.symbol, "dry_run": self.dry_run,
               "skip_reason": reason, "volume_usd": round(volume, 4),
               "fees_usd": round(fees, 6), "net_usd": round(pnl - fees, 6)}
        with CYCLES_PATH.open("a") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self.cum_net += rec["net_usd"]
        self.cum_fees += rec["fees_usd"]
        self.cum_volume += rec["volume_usd"]
        self._write_status()
        log(f"中断コスト計上: {reason} vol=${volume:.0f} fees=${fees:.4f} "
            f"net=${rec['net_usd']:+.4f} cum_net=${self.cum_net:+.3f}")

    def _notify_halt_cleared(self) -> None:
        """自己ハルト**解除**を Discord へ1回だけ通知(状態遷移時のみ)。fail-open。"""
        try:
            subprocess.run(["discord-notify", "-t", "xvenue-hedge ハルト解除", "-c", "green",
                            f"ハルト条件が消えたので稼働を再開した。\n"
                            f"直前の理由: {self.halted_reason}\n"
                            f"cycles={self.cycles} 出来高=${self.cum_volume:,.0f} "
                            f"net=${self.cum_net:+.3f}"],
                           timeout=15, check=False)
        except Exception:
            pass

    def _notify_halt(self, reason: str) -> None:
        """自己ハルトを Discord へ1回だけ通知(状態遷移時のみ)。fail-open — 通知の失敗で
        botを落とさない。共通CLI経由(curl直叩き禁止。~/CLAUDE.md)。"""
        body = (f"{reason}\n"
                f"cycles={self.cycles} 出来高=${self.cum_volume:,.0f} net=${self.cum_net:+.3f}\n"
                f"発注は停止した(両会場は最終サイクルでフラット)。config見直しまで再開しない。")
        try:
            subprocess.run(["discord-notify", "-t", "xvenue-hedge 自己ハルト", "-c", "red", body],
                           timeout=15, check=False)
        except Exception:
            pass

    def _halt_reason(self) -> Optional[str]:
        """撤退基準(2026-07-26 改定)。

        ## 主役は perpl 口座の**合算ローリングガード**
        本セルは txflow の出来高farm が目的なので、**効率(出来高÷損失)は撤退基準にしない**
        (ユーザー方針 2026-07-26)。代わりに口座を守るのは合算ガード:
        pair_hedge と同一 perpl 口座を共有しているのにセル別予算しか無く、
        $200 x 2 = equity($186)の約2倍 = 実質ノーガードだった(監査 M-7)。

        ## なぜ効率を外せるのか / 外して何が残るのか
        効率の分岐点計算には次元の誤りがあった: volume_usd は `notional * 4`
        = **2会場x(open+close)のブレンド**なのに、pt率 10pt/$100k は **perpl 単独**の実測値。
        ブレンド出来高に perpl の率を掛けており収入が正確に2倍過大で、
        真の分岐点は 4,902 ではなく 9,804(k補正後 8,668)だった。
        現行効率 5,843 はそこを大きく割る = 効率で判定すると即停止になる。
        出来高farmを続ける方針なので効率ゲートは無効化し(config efficiency_floor: 0)、
        口座の生存だけを合算ガードで守る。

        ## 残る3段
        1. 合算ローリングガード(account_guard_24h_usd) … 口座を守る主役
        2. loss_budget_usd(セル累積) … バグ暴走用の外側の弁
        3. efficiency_floor … 既定0=無効。効率で止めたくなったら戻す口を残す
        """
        # 1. 口座レベル: 両セルの直近24h合算損失。全期間累積は既に -$144 で閾値を置けないため窓で見る。
        guard = _pag.halt_reason(float(self.cfg.get("account_guard_24h_usd", 0) or 0),
                                 equity_floor_usd=float(self.cfg.get("perpl_equity_floor_usd", 0) or 0))
        if guard:
            return guard
        # 1b. txflow 口座の証拠金(2026-07-27追加)。account_guard は両セルの**セルnet**を合算する
        #     ものなので perpl 口座も txflow 口座も残高としては見ていない。txflow は
        #     必要証拠金(notional/leverage)を割ると発注が通らなくなるだけで、誰も気づかない。
        # ★txflow 退役時(txflow_leg_enabled: false)はこの床を見ない。見ると発注しない口座の
        #   残高で**永久にハルトし続ける**(2026-07-29 に実際 $54.89 < $55 で 4.8時間停止した床)。
        min_eq = float(self.cfg.get("txflow_min_equity_usd", 0) or 0)
        if not self.cfg.get("txflow_leg_enabled", True):
            min_eq = 0.0
        if min_eq > 0 and not self.dry_run:
            eq = self._tx_equity()
            if eq is not None and eq < min_eq:
                return f"txflow equity ${eq:.2f} < 下限 ${min_eq:.2f}(証拠金の床)"
        if self.cum_net <= -float(self.cfg["loss_budget_usd"]):
            return (f"loss_budget${self.cfg['loss_budget_usd']}超過"
                    f"(net=${self.cum_net:.3f})")
        floor = float(self.cfg.get("efficiency_floor", 0) or 0)
        if floor <= 0 or self.cum_net >= 0:
            return None
        # 初動は1サイクルの誤差で効率が乱高下するので、一定の出来高が乗るまで評価しない。
        if self.cum_volume < float(self.cfg.get("efficiency_min_volume_usd", 1000)):
            return None
        eff = self.cum_volume / abs(self.cum_net)
        if eff < floor:
            return (f"効率{eff:.0f}(出来高${self.cum_volume:.0f}/損失${-self.cum_net:.3f})が"
                    f"floor{floor:.0f}割れ")
        return None

    def _record(self, rec: dict) -> None:
        with CYCLES_PATH.open("a") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self.cum_volume += rec["volume_usd"]
        self.cum_net += rec["net_usd"]
        self.cum_fees += rec["fees_usd"]
        self.cycles += 1
        eff = self._write_status()
        log(f"cycle#{self.cycles} vol=${rec['volume_usd']:.0f} net=${rec['net_usd']:+.4f} "
            f"cum_net=${self.cum_net:+.3f} eff={round(eff, 1) if eff else None}")

    def loop(self) -> None:
        log(f"xvenue-hedge 起動: dry_run={self.dry_run} notional=${self.notional} "
            f"(累積 cycles={self.cycles} net=${self.cum_net:+.3f})")
        self._startup_reconcile()  # 起動時に両会場BTCフラット(再起動での建玉残存防止)
        dir_buy = True
        while self.cfg.get("enabled", True):
            reason = self._halt_reason()
            if reason:
                if not self.halted:
                    self.halted = True
                    self.halted_reason = reason
                    log(f"⚠️ 自己ハルト: {reason}")
                    self._write_status()   # ハルトを status.json に残す(外から検知できるように)
                    self._notify_halt(reason)
                time.sleep(30)
                continue
            if self.halted:
                # ★ハルト条件が消えた=自動再開。旧コードは halted を False に戻す箇所が
                #   __init__ にしか無く、account_guard(ローリング24h窓)が戻って実際に稼働を
                #   再開しても status.json は永久に halted:true のままだった(2026-07-27 C-2)。
                #   「止まっているように見えて動いている」は「動いて見えて止まっている」と
                #   同じくらい危険なので、解除も状態遷移として通知する。
                log(f"自己ハルト解除(条件消失): 直前の理由={self.halted_reason}")
                self._notify_halt_cleared()
                self.halted = False
                self.halted_reason = ""
                self._write_status()
            # cycle例外後は新規サイクルより先に両会場をフラット化(裸脚の上に建てない)
            if self.dirty and not self.dry_run:
                if self.dirty_since is None:
                    self.dirty_since = time.time()
                if self._reconcile_dirty():
                    self.dirty = False
                    self.dirty_since = None
                    log("error後reconcile: 両会場フラット確認。稼働再開")
                    self._write_status()
                else:
                    stuck = time.time() - self.dirty_since
                    log(f"⚠️ error後reconcile未完(両会場flat未確認、{stuck:.0f}s継続)=次ループで再試行")
                    # ★status を必ず書く(2026-07-26)。ここを書かないと halted:false のまま
                    #   updated_ts が最後の完走サイクル時刻で固まり「稼働中に見える停止」になる。
                    #   429 が続くと長時間化する経路(実測6.5時間)なので、外から見えることが必須。
                    self._write_status()
                    time.sleep(self.cfg.get("cooldown_seconds", 10))
                    continue
            skipped = False
            try:
                rec = self.run_cycle(dir_buy)
                if rec.get("skip"):
                    skipped = True
                    log(f"cycle見送り: {rec['skip']}")
                    # 中断コスト(unwind の taker 手数料等)があれば台帳に残す(2026-07-27 D-5)
                    if rec.get("abort_fees_usd"):
                        self._record_abort(rec["skip"], float(rec["abort_fees_usd"]))
                    # lead の部分約定→畳み。**実際に建てて実際に畳んだ往復**なので
                    # 出来高も価格差も現実に出ている(2026-07-29)。
                    elif rec.get("fees_usd") or rec.get("volume_usd"):
                        self._record_abort(rec["skip"], float(rec.get("fees_usd") or 0.0),
                                           volume=float(rec.get("volume_usd") or 0.0),
                                           pnl=float(rec.get("pnl") or 0.0))
                    # ★見送り時にperplへ想定外建玉(cancel-fillレースの残脚。2026-07-24 HYPEで多発)が
                    #   あればガードがskipし続けて停止+脚放置になる。常駐AccountFeed(WS・429負荷ほぼ無)で
                    #   検出したらdirtyを立て次ループで両会場flat化=停止と裸脚を自己修復する。
                    if not self.dry_run:
                        try:
                            if any(abs(self._pp_szi(lg)) > 1e-8 for lg in self.legs.values()):
                                self.dirty = True
                                log("⚠️ 見送り時にperpl残脚検出→次ループでflatten(自己修復)")
                        except Exception:
                            pass
                    # 見送りが続く=perplを叩いても成立しない状態。cooldownのまま回すと
                    # WS再接続を1.3秒ごとに投げ続けてCF 1015を自分で焚きつける(2026-07-25の
                    # 6.5時間停止では309連続見送りの間ずっと429を煽っていた)。例外が飛ばない
                    # 経路(ガードのfail-closedはskipを返すだけ)なので連続数で待機を伸ばす。
                    self._skip_streak += 1
                    if self._skip_streak >= int(self.cfg.get("skip_streak_backoff_after", 5)):
                        base = float(self.cfg.get("rl_backoff_base_seconds", 30))
                        cap = float(self.cfg.get("rl_backoff_cap_seconds", 240))
                        self._rl_backoff = min(cap, base if self._rl_backoff <= 0 else self._rl_backoff * 2)
                        log(f"見送り{self._skip_streak}連続={self._rl_backoff:.0f}s バックオフ(叩くのを止める)")
                        time.sleep(self._rl_backoff)
                else:
                    self._record(rec)
                    dir_buy = not dir_buy
                    self._skip_streak = 0
                    self._rl_backoff = 0.0               # 完走した=詰まっていない。バックオフ解除
                    # 初回実弾は1サイクルで停止(canary検証用。確認後にfalseへ)
                    if not self.dry_run and self.cfg.get("canary_once", False):
                        log("canary_once: 1サイクル完了。停止(建玉フラット確認のこと)。")
                        return
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
            # 見送り(=建玉を1枚も取っていない)にフルcooldownを課す理由が無い。実測(07-25 12時以降)
            # では見送り1回=中位81s のうち30sがこのcooldownで、lead不成立率36.7%なので
            # 完走あたり ~17s の純粋な待ち。連続見送りの429自己増幅は上の skip_streak_backoff が
            # 別に受け持つので、単発の見送りは短く回して次の試行に移る。
            time.sleep(float(self.cfg.get("skip_cooldown_seconds", 5)) if skipped
                       else float(self.cfg.get("cooldown_seconds", 10)))


def main() -> None:
    cfg = yaml.safe_load((APP / "config.yaml").read_text())
    XVenueHedge(cfg).loop()


if __name__ == "__main__":
    main()
