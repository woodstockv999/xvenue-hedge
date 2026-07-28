"""xvenue-hedge の脚(leg)分離まわりの回帰テスト。

★このファイルの主目的は **2026-07-24「銘柄ハードコードで裸建玉を量産」の再発防止**。
  旧実装は `PERPL_MCFG` というモジュールグローバルの単一 dict を __init__ で書き換える方式で、
  2市場を同時に扱うと「最後に書き換えた側の market_id/精度」が両脚に効く。テストで
  「グローバルが存在しないこと」と「脚が互いに汚染しないこと」を固定する。

★perpl / txflow へ実接続してはいけない。本番のレートバケットを枯渇させる
  (pytest が本番バケットを食った実例がある)。全て fake を注入して検証する。
"""
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))

main = pytest.importorskip("main", reason="perpl/txflow の依存が入った venv でのみ実行")


# --------------------------------------------------------------------------- fakes
class _FakeAccountFeed:
    """PerplAccountFeed の代役。market_id ごとに別の建玉/oid を返す。

    「脚が相手の market を読んでいないか」を検出するのが役目なので、
    **market_id をキーにして値を変える**ことが本質。"""

    def __init__(self, positions=None, oids=None):
        self.positions = positions or {}
        self.oids = oids or {}
        self.asked = []

    def get_position(self, market_id, price_decimals, size_decimals):
        self.asked.append(("pos", market_id, price_decimals, size_decimals))
        return self.positions.get(market_id)

    def get_live_oids(self, market_id):
        self.asked.append(("oids", market_id))
        return self.oids.get(market_id, [])


class _FakeExec:
    def __init__(self, szi=0.0, position=None, raises=False, open_orders=None,
                 market_px=None, market_raises=False):
        self._szi, self._position, self._raises = szi, position, raises
        self._open_orders = open_orders
        self._market_px, self._market_raises = market_px, market_raises
        self.cancelled = []
        self.calls = []
        self.orders = []          # place_order の履歴 (is_buy, size, reason, reduce_only)

    def place_order(self, is_buy, size, reason, reduce_only):
        self.orders.append((is_buy, size, reason, reduce_only))
        if self._market_raises and not reduce_only:
            raise RuntimeError("perpl: 成行注文の全量約定を確認できなかった")
        return {"price": self._market_px, "sz": size}

    def list_open_maker_orders(self):
        self.calls.append("list_open_maker_orders")
        return self._open_orders

    def cancel_order(self, oid):
        self.cancelled.append(oid)

    def get_position_szi(self):
        self.calls.append("get_position_szi")
        return self._szi

    def _fetch_position(self):
        self.calls.append("_fetch_position")
        if self._raises:
            raise RuntimeError("429")
        return self._position


class _FakeBook:
    def __init__(self, bbo=None):
        self._bbo = bbo

    def get_best_bid_ask(self):
        return self._bbo


class _FakeMarket:
    def __init__(self, bbo=None, size_decimals=5):
        self._bbo, self.size_decimals = bbo, size_decimals
        self.rest_calls = 0

    def get_best_bid_ask(self):
        self.rest_calls += 1
        return self._bbo

    def round_size(self, size, for_reduce_only=False):
        return round(size, self.size_decimals)


def make_leg(symbol, market_id, price_decimals, size_decimals,
             book_bbo=None, market_bbo=None, exec_=None, bbo_ttl=2.0):
    """_PerplLeg を **実クライアント無しで**組み立てる。__init__ は WS を張るので使わない。"""
    leg = main._PerplLeg.__new__(main._PerplLeg)
    leg.symbol = symbol.upper()
    leg.name = f"perpl:{leg.symbol}"
    leg.mcfg = {"market_id": market_id, "price_decimals": price_decimals,
                "size_decimals": size_decimals, "leverage": 3}
    leg.market_id = market_id
    leg.price_decimals = price_decimals
    leg.size_decimals = size_decimals
    leg.market = _FakeMarket(market_bbo, size_decimals)
    leg.exec = exec_ or _FakeExec()
    leg.book = _FakeBook(book_bbo)
    leg.bbo_ttl = bbo_ttl
    leg.bbo_cache = None
    return leg


def make_bot(legs, account=None, tx_position=0.0):
    """XVenueHedge を __init__ を通さずに組み立てる(接続を張らないため)。"""
    bot = main.XVenueHedge.__new__(main.XVenueHedge)
    bot.legs = {lg.symbol: lg for lg in legs}
    bot.lead_leg = legs[0]
    bot.hedge_leg = legs[1] if len(legs) > 1 else None
    bot.pp_account = account or _FakeAccountFeed()
    bot.cfg = {}
    bot.dry_run = False
    bot._skip_streak = 0        # feed 不信の発火判定に使う(_pp_entry_blocked)
    bot._tx_position = lambda: tx_position
    return bot


# --------------------------------------------------------------------------- tests
def test_global_market_config_is_gone():
    """★回帰: グローバルの単一 market dict を復活させない。

    `PERPL_MCFG` を「差し替える」設計に戻すと、2市場化した瞬間に片方の市場パラメータが
    もう片方に乗る(2026-07-24 事故と同型)。**持たない**ことをテストで固定する。"""
    assert not hasattr(main, "PERPL_MCFG"), \
        "PERPL_MCFG が復活している。脚ごとに _PerplLeg で持つこと"


def test_eth_market_defined():
    eth = main._PERPL_MARKETS["ETH"]
    assert eth["market_id"] == 20
    assert eth["price_decimals"] == 2
    assert eth["size_decimals"] == 3


def test_legs_do_not_share_market_params():
    """★回帰: BTC と ETH が互いの market_id / 精度を持ち込まない。"""
    btc = make_leg("BTC", 1, 1, 5)
    eth = make_leg("ETH", 20, 2, 3)
    assert (btc.market_id, btc.price_decimals, btc.size_decimals) == (1, 1, 5)
    assert (eth.market_id, eth.price_decimals, eth.size_decimals) == (20, 2, 3)
    assert btc.mcfg is not eth.mcfg


def test_pp_szi_reads_the_given_leg_market():
    """_pp_szi が **渡された脚の market_id** を読む(相手の建玉を自分のものにしない)。"""
    acct = _FakeAccountFeed(positions={1: {"szi": 0.0043}, 20: {"szi": -0.067}})
    btc, eth = make_leg("BTC", 1, 1, 5), make_leg("ETH", 20, 2, 3)
    bot = make_bot([btc, eth], account=acct)
    assert bot._pp_szi(btc) == pytest.approx(0.0043)
    assert bot._pp_szi(eth) == pytest.approx(-0.067)
    assert ("pos", 1, 1, 5) in acct.asked and ("pos", 20, 2, 3) in acct.asked


def test_pp_szi_falls_back_to_leg_executor():
    """WS が読めない脚は **その脚の** executor へ落ちる。"""
    acct = _FakeAccountFeed(positions={})          # 全 market で None
    btc = make_leg("BTC", 1, 1, 5, exec_=_FakeExec(szi=0.5))
    eth = make_leg("ETH", 20, 2, 3, exec_=_FakeExec(szi=-0.25))
    bot = make_bot([btc, eth], account=acct)
    assert bot._pp_szi(btc) == 0.5
    assert bot._pp_szi(eth) == -0.25


def test_pp_szi_strict_returns_none_when_unreadable():
    """★N-2: 読めないときに 0.0(=フラット)を返してはいけない。None で「判定不能」を伝える。"""
    acct = _FakeAccountFeed(positions={})
    leg = make_leg("BTC", 1, 1, 5, exec_=_FakeExec(raises=True))
    bot = make_bot([leg], account=acct)
    assert bot._pp_szi_strict(leg) is None


def test_pp_szi_strict_zero_means_confirmed_flat():
    acct = _FakeAccountFeed(positions={})
    leg = make_leg("BTC", 1, 1, 5, exec_=_FakeExec(position=None))
    bot = make_bot([leg], account=acct)
    assert bot._pp_szi_strict(leg) == 0.0


def test_bbo_cache_is_per_leg():
    """★回帰: BBO キャッシュが脚別。単一だと ETH の発注に BTC の板が乗る。"""
    btc = make_leg("BTC", 1, 1, 5, market_bbo=(65000.0, 65001.0))
    eth = make_leg("ETH", 20, 2, 3, market_bbo=(1957.0, 1957.5))
    bot = make_bot([btc, eth])
    assert bot._perpl_bbo(btc) == (65000.0, 65001.0)
    assert bot._perpl_bbo(eth) == (1957.0, 1957.5)      # BTC のキャッシュが漏れない
    assert bot._perpl_bbo(btc) == (65000.0, 65001.0)    # 2回目はキャッシュ
    assert btc.market.rest_calls == 1, "脚別キャッシュが効いていない"
    assert eth.market.rest_calls == 1


def test_bbo_prefers_book_feed_over_rest():
    leg = make_leg("BTC", 1, 1, 5, book_bbo=(1.0, 2.0), market_bbo=(9.0, 9.5))
    bot = make_bot([leg])
    assert bot._perpl_bbo(leg) == (1.0, 2.0)
    assert leg.market.rest_calls == 0


def test_entry_blocked_checks_only_its_own_market():
    """相手の脚に指値が生きていても、自分の脚は止まらない(市場別なので degrade 継続できる)。"""
    acct = _FakeAccountFeed(oids={20: [111]})          # ETH にだけ生存指値
    btc, eth = make_leg("BTC", 1, 1, 5), make_leg("ETH", 20, 2, 3)
    bot = make_bot([btc, eth], account=acct)
    assert bot._pp_entry_blocked(eth) is True
    assert bot._pp_entry_blocked(btc) is False


def test_venues_flat_requires_every_leg():
    """★全脚を見る。1脚でも建玉が残れば False(fail-closed)。"""
    acct = _FakeAccountFeed(positions={1: {"szi": 0.0}, 20: {"szi": 0.05}})
    btc, eth = make_leg("BTC", 1, 1, 5), make_leg("ETH", 20, 2, 3)
    bot = make_bot([btc, eth], account=acct, tx_position=0.0)
    assert bot._venues_flat() is False

    acct2 = _FakeAccountFeed(positions={1: {"szi": 0.0}, 20: {"szi": 0.0}})
    bot2 = make_bot([make_leg("BTC", 1, 1, 5), make_leg("ETH", 20, 2, 3)],
                    account=acct2, tx_position=0.0)
    assert bot2._venues_flat() is True


def test_venues_flat_is_fail_closed_when_unreadable():
    """WS も REST も読めない脚があれば False(フラットと決めつけない)。"""
    acct = _FakeAccountFeed(positions={})
    leg = make_leg("BTC", 1, 1, 5, exec_=_FakeExec(position={"s": 100}))
    bot = make_bot([leg], account=acct, tx_position=0.0)
    assert bot._venues_flat() is False


def test_venues_flat_false_when_txflow_has_position():
    acct = _FakeAccountFeed(positions={1: {"szi": 0.0}})
    bot = make_bot([make_leg("BTC", 1, 1, 5)], account=acct, tx_position=0.004)
    assert bot._venues_flat() is False


def test_entry_blocked_recovers_from_stale_feed():
    """★実害回帰(2026-07-27): feed が消えた指値を返し続けて約3時間停止した。

    見送りが続いたら取引所に問い合わせ、**空なら feed 陳腐化とみなして続行**する。
    これが無いと再起動でしか復帰できない。"""
    acct = _FakeAccountFeed(oids={1: [5945875300366]})     # feed は「生存」と言う
    leg = make_leg("BTC", 1, 1, 5, exec_=_FakeExec(open_orders=[]))  # 取引所は空
    bot = make_bot([leg], account=acct)

    bot._skip_streak = 0
    assert bot._pp_entry_blocked(leg) is True, "見送りが浅いうちは feed を信じる"

    bot._skip_streak = 5
    assert bot._pp_entry_blocked(leg) is False, "取引所が空なら続行すべき"


def test_entry_blocked_cancels_real_orphan_while_running():
    """稼働中に**本物の孤児 resting**があればその場で取消す(従来は再起動が必要だった)。"""
    acct = _FakeAccountFeed(oids={1: [999]})
    ex = _FakeExec(open_orders=[("perpl:BTC", 999)])
    leg = make_leg("BTC", 1, 1, 5, exec_=ex)
    bot = make_bot([leg], account=acct)
    bot._skip_streak = 5
    assert bot._pp_entry_blocked(leg) is True
    assert 999 in ex.cancelled, "孤児 resting を取り消していない"


def test_entry_blocked_stays_blocked_when_exchange_unreadable():
    """取引所に問い合わせられないときは従来どおり見送る(fail-closed)。"""
    acct = _FakeAccountFeed(oids={1: [777]})
    ex = _FakeExec(open_orders=None)          # 取得失敗
    leg = make_leg("BTC", 1, 1, 5, exec_=ex)
    bot = make_bot([leg], account=acct)
    bot._skip_streak = 5
    assert bot._pp_entry_blocked(leg) is True


# --------------------------------------------------------------------------- 3脚(C5)
def _bot_for_plan(hedge=True, lead=190.0, tx=150.0, hr=1.0, eth_bbo=(1957.0, 1957.5)):
    btc = make_leg("BTC", 1, 1, 5)
    eth = make_leg("ETH", 20, 2, 3, market_bbo=eth_bbo) if hedge else None
    bot = make_bot([btc] + ([eth] if eth else []))
    bot.notional = tx
    bot._size_round = 4          # txflow BTC sd=4 と perpl BTC sd=5 の粗い方
    bot._sr_hedge = 3            # perpl ETH sd=3
    bot.cfg = {"lead_notional_usd": lead, "hedge_ratio": hr}
    if not hedge:
        bot.hedge_leg = None
    return bot


def test_plan_sizes_no_hedge_leg_is_two_leg():
    """hedge_leg が無ければ ETH サイズ 0 = 現行2脚と等価。"""
    bot = _bot_for_plan(hedge=False)
    p = bot._plan_sizes(65000.0, 65002.0)
    assert p["size_eth"] == 0.0


def test_plan_sizes_zero_residual_when_lead_equals_txflow():
    """★カナリアの土台: lead == txflow なら残差0 → ETH 脚が構造的に不在。"""
    bot = _bot_for_plan(lead=150.0, tx=150.0)
    p = bot._plan_sizes(65000.0, 65002.0)
    assert p["size_lead"] == p["size_tx"]
    assert p["size_eth"] == 0.0


def test_plan_sizes_eth_comes_from_rounded_residual():
    """★ETH サイズは config の名目でなく**丸め後の BTC 残差**から出す。

    名目から引くと、丸め後の実残差とズレた分がそのまま裸デルタになる。"""
    bot = _bot_for_plan(lead=190.0, tx=150.0)
    mid = 65001.0
    p = bot._plan_sizes(65000.0, 65002.0)
    resid_sz = round(p["size_lead"] - p["size_tx"], 4)
    assert p["resid_usd"] == pytest.approx(resid_sz * mid, rel=1e-9)
    # ETH notional が BTC 残差と一致(hedge_ratio=1.0)
    assert p["size_eth"] * p["eth_mid"] == pytest.approx(p["resid_usd"], rel=0.01)


def test_plan_sizes_respects_hedge_ratio():
    bot = _bot_for_plan(lead=190.0, tx=150.0, hr=0.5)
    p = bot._plan_sizes(65000.0, 65002.0)
    assert p["size_eth"] * p["eth_mid"] == pytest.approx(p["resid_usd"] * 0.5, rel=0.02)


def test_plan_sizes_lead_below_txflow_is_clamped():
    """lead < txflow は設計外(残差が負)。txflow に丸めて2脚に落とす。"""
    bot = _bot_for_plan(lead=100.0, tx=150.0)
    p = bot._plan_sizes(65000.0, 65002.0)
    assert p["size_lead"] == p["size_tx"]
    assert p["size_eth"] == 0.0


def test_plan_sizes_eth_size_uses_eth_decimals():
    """ETH のサイズ丸めは ETH の size_decimals(3)で行う(BTC の5ではない)。"""
    bot = _bot_for_plan(lead=190.0, tx=150.0)
    p = bot._plan_sizes(65000.0, 65002.0)
    assert p["size_eth"] == round(p["size_eth"], 3)


# ------------------------------------------------------- ヘッジ脚 taker フォールバック(2026-07-28)
# ★実害回帰: maker 一本槍だと ETH follow が **40%(49/122)不成立**で、その全部が hold に入らず
#   即クローズ = 積みたい OI が4割の周回で消えていた。さらに部分約定→unwind のコストは
#   台帳にも口座ガードにも 1ドルも現れていなかった。
def _hedge_bot(exec_=None, taker_fallback=True):
    btc = make_leg("BTC", 1, 1, 5)
    eth = make_leg("ETH", 20, 2, 3, exec_=exec_ or _FakeExec(market_px=1958.0))
    bot = make_bot([btc, eth])
    bot._sr_hedge = 3
    bot.cfg = {"perpl_hedge_timeout_seconds": 120,
               "perpl_hedge_taker_fallback": taker_fallback}
    return bot


def _stub_maker(bot, result, seen):
    def _fake(leg, is_buy, size, timeout_s=None, keep_partial=False):
        seen.append({"leg": leg.symbol, "size": size, "timeout_s": timeout_s,
                     "keep_partial": keep_partial})
        return result
    bot._perpl_maker_lead = _fake


def test_maker_lead_returns_three_tuple():
    """★契約: (filled, px, got_sz)。2要素に戻すと呼び側が黙って壊れる。"""
    acct = _FakeAccountFeed(oids={1: [1]})       # entry_blocked=True で即 return させる
    leg = make_leg("BTC", 1, 1, 5)
    bot = make_bot([leg], account=acct)
    bot.cfg = {"perpl_lead_timeout_seconds": 1, "requote_interval_seconds": 12,
               "poll_interval_seconds": 5}
    assert bot._perpl_maker_lead(leg, True, 0.001) == (False, None, 0.0)


def test_hedge_full_maker_fill_does_not_take():
    bot = _hedge_bot()
    seen = []
    _stub_maker(bot, (True, 1957.0, 0.02), seen)
    ok, px, got, took, ab = bot._perpl_hedge_follow(True, 0.02)
    assert (ok, px, got, took, ab) == (True, 1957.0, 0.02, False, None)
    assert bot.hedge_leg.exec.orders == [], "maker で埋まっているのに taker を打っている"
    assert seen[0]["keep_partial"] is True


def test_hedge_partial_maker_is_completed_by_taker():
    """★本丸: 0.001/0.02 の部分約定を**捨てずに**残り 0.019 を taker で埋める。"""
    ex = _FakeExec(market_px=1958.0)
    bot = _hedge_bot(exec_=ex)
    _stub_maker(bot, (False, 1957.0, 0.001), [])
    ok, px, got, took, ab = bot._perpl_hedge_follow(True, 0.02)
    assert ok is True and took is True and ab is None
    assert got == pytest.approx(0.02)
    assert ex.orders == [(True, 0.019, "hedge_taker", False)]
    # open 価格は約定加重平均(片方の価格で全量計上すると損益の捏造になる)
    assert px == pytest.approx((1957.0 * 0.001 + 1958.0 * 0.019) / 0.02)


def test_hedge_zero_maker_fill_is_all_taker():
    ex = _FakeExec(market_px=1958.0)
    bot = _hedge_bot(exec_=ex)
    _stub_maker(bot, (False, 1957.0, 0.0), [])
    ok, px, got, took, ab = bot._perpl_hedge_follow(True, 0.02)
    assert (ok, took, got) == (True, True, 0.02)
    assert ex.orders == [(True, 0.02, "hedge_taker", False)]
    assert px == pytest.approx(1958.0), "maker 約定0なら taker 価格そのもの"


def test_hedge_taker_failure_unwinds_partial_and_reports_cost():
    """★taker も失敗したら maker 部分建玉を必ず畳む(裸で残さない)。

    そのコストを abort dict で返す — 従来は unwind が戻り値を持たず、
    このコストが台帳から丸ごと消えていた。"""
    ex = _FakeExec(market_px=1956.0, market_raises=True)
    bot = _hedge_bot(exec_=ex)
    bot.dirty = False
    _stub_maker(bot, (False, 1957.0, 0.001), [])
    ok, px, got, took, ab = bot._perpl_hedge_follow(True, 0.02)
    assert ok is False and took is False
    assert ab is not None and ab["sz"] == pytest.approx(0.001) and ab["px"] == 1957.0
    assert (False, 0.001, "unwind", True) in ex.orders, "部分建玉を畳んでいない"
    assert bot.dirty is True


def test_hedge_taker_fallback_can_be_disabled():
    """kill switch: false なら従来動作(maker のみ・部分約定は畳んで見送り)。"""
    ex = _FakeExec(market_px=1958.0)
    bot = _hedge_bot(exec_=ex, taker_fallback=False)
    seen = []
    _stub_maker(bot, (False, None, 0.0), seen)
    assert bot._perpl_hedge_follow(True, 0.02) == (False, None, 0.0, False, None)
    assert ex.orders == []
    assert seen[0]["keep_partial"] is False, \
        "fallback 無効なのに建玉を残している(畳み手がいなくなる)"


def test_hedge_unreadable_position_does_not_take():
    """★fail-closed: maker 後の建玉が読めない(429)ときに taker を打つと**過剰ヘッジ**になる。

    `_pp_szi` は 429 で 0.0 に fail-open するので、部分建玉を『0約定』と誤読したまま
    全量を taker で上塗りする経路が実在した。got=None(判定不能)で止める。"""
    ex = _FakeExec(market_px=1958.0)
    bot = _hedge_bot(exec_=ex)
    bot.dirty = False
    _stub_maker(bot, (False, 1957.0, None), [])
    assert bot._perpl_hedge_follow(True, 0.02) == (False, None, 0.0, False, None)
    assert ex.orders == [], "建玉不明なのに taker を打っている"
    assert bot.dirty is True


def test_maker_lead_returns_none_size_when_position_unreadable():
    """keep_partial 経路は strict 読み。読めなければ 0.0 でなく None を返す。"""
    acct = _FakeAccountFeed(positions={})
    leg = make_leg("ETH", 20, 2, 3, exec_=_FakeExec(raises=True))
    bot = make_bot([leg], account=acct)
    bot.dirty = False
    bot.cfg = {"perpl_lead_timeout_seconds": 0, "requote_interval_seconds": 12,
               "poll_interval_seconds": 0}
    ok, _px, got = bot._perpl_maker_lead(leg, True, 0.02, timeout_s=0, keep_partial=True)
    assert ok is False and got is None
    assert bot.dirty is True


def test_hedge_follow_uses_hedge_timeout_not_lead():
    bot = _hedge_bot()
    seen = []
    _stub_maker(bot, (True, 1957.0, 0.02), seen)
    bot._perpl_hedge_follow(True, 0.02)
    assert seen[0]["timeout_s"] == 120


# ------------------------------------------------------- txflow join offset(2026-07-28)
# ★狙い: txflow open の taker 落ち 47% を、板の1tick内側に置くことで下げる。
#   クランプを外すと post_only 拒否 → taker 落ちで**悪化する**ので、そこを固定する。
def _join_bot(off=1, tick=0.1, dec=1):
    bot = make_bot([make_leg("BTC", 1, 1, 5)])
    bot._tx_tick, bot._px_round = tick, dec
    bot.cfg = {"follow_join_offset_ticks": off}
    return bot


def test_join_offset_zero_is_touch():
    """既定(0)は従来どおり touch に並ぶ = 完全な後方互換。"""
    b = _join_bot(off=0)
    assert b._tx_join_px(True, 63191.9, 63192.1, 0) == 63191.9
    assert b._tx_join_px(False, 63191.9, 63192.1, 0) == 63192.1


def test_join_offset_moves_inside_on_two_tick_spread():
    """実測のスプレッド2tickでは1tick内側に入れる(無競争の最良気配)。"""
    b = _join_bot()
    assert b._tx_join_px(True, 63191.9, 63192.1, 1) == pytest.approx(63192.0)
    assert b._tx_join_px(False, 63191.9, 63192.1, 1) == pytest.approx(63192.0)


def test_join_offset_never_crosses_on_one_tick_spread():
    """★スプレッドが1tickなら内側は無い。touch に留まること(越えると post_only 拒否→taker)。"""
    b = _join_bot()
    assert b._tx_join_px(True, 63192.0, 63192.1, 1) == pytest.approx(63192.0)
    assert b._tx_join_px(False, 63192.0, 63192.1, 1) == pytest.approx(63192.1)


def test_join_offset_clamped_when_offset_exceeds_spread():
    """offset がスプレッドより大きくても反対 touch を越えない。"""
    b = _join_bot(off=5)
    buy = b._tx_join_px(True, 63191.9, 63192.1, 5)
    sell = b._tx_join_px(False, 63191.9, 63192.1, 5)
    assert buy <= 63192.1 - 0.1 + 1e-9, f"buy が ask を越えた: {buy}"
    assert sell >= 63191.9 + 0.1 - 1e-9, f"sell が bid を割った: {sell}"


def test_join_offset_stable_when_we_are_the_touch():
    """★自分が最良気配になったら requote しない(キュー先頭を捨てない)。

    BBO は自分の指値を含むので、置いた直後の再読み取りでは bid==自分の価格になる。
    そこで再計算した join 価格が元と一致することが、requote 抑止の前提。"""
    b = _join_bot()
    mpx = b._tx_join_px(True, 63191.9, 63192.1, 1)      # 63192.0 に置く
    again = b._tx_join_px(True, mpx, 63192.1, 1)        # 板は bid=自分, ask 据え置き
    assert again == pytest.approx(mpx), "自分が touch なのに置き直そうとしている"


def test_join_offset_requotes_when_outbid():
    """他者に抜かれたら追随する(そこは並び直す必要がある)。"""
    b = _join_bot()
    mpx = b._tx_join_px(True, 63191.9, 63192.3, 1)      # 63192.0
    after = b._tx_join_px(True, 63192.1, 63192.3, 1)    # 誰かが 63192.1 で上乗せ
    assert after > mpx


def test_hedge_follow_noop_without_hedge_leg():
    bot = _hedge_bot()
    bot.hedge_leg = None
    assert bot._perpl_hedge_follow(True, 0.02) == (False, None, 0.0, False, None)
    assert bot._perpl_hedge_follow(True, 0.0) == (False, None, 0.0, False, None)
