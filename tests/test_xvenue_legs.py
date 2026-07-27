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
    def __init__(self, szi=0.0, position=None, raises=False):
        self._szi, self._position, self._raises = szi, position, raises
        self.calls = []

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
