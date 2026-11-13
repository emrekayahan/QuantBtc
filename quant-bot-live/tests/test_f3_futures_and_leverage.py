"""Phase F3 (K5 + K6) TDD tests: Futures v2 execution, WalletBalancer, LeverageAdvisor & RiskAllocator."""
from __future__ import annotations

import time
from typing import Any, Dict, List

import pytest

from config.settings import Settings
from database.db_manager import DatabaseManager


# ---------------------------------------------------------------------------
# K6: LeverageAdvisor & RiskAllocator Tests
# ---------------------------------------------------------------------------

def test_risk_scales_with_mu_lcb() -> None:
    """K6: Higher mu_lcb increases risk_pct in exploit mode, clamped to state max."""
    from engine.leverage_advisor import LeverageAdvisor

    adv = LeverageAdvisor()
    low_edge = adv.advise(
        equity=100.0,
        stop_dist_pct=1.0,
        mu_lcb=0.05,
        sigma_r=1.3,
        strategy_state="exploit_low",
    )
    high_edge = adv.advise(
        equity=100.0,
        stop_dist_pct=1.0,
        mu_lcb=0.25,
        sigma_r=1.3,
        strategy_state="exploit_low",
    )
    assert high_edge["risk_pct"] > low_edge["risk_pct"]
    assert high_edge["risk_pct"] <= 1.5  # RISK_MAX_PCT_EXPLOIT_LOW


def test_zero_edge_means_learn_risk_only() -> None:
    """K6: Zero/negative mu_lcb or learn state uses LEARN_RISK_PCT (0.5%) and L <= LEVERAGE_CAP_LEARN (5)."""
    from engine.leverage_advisor import LeverageAdvisor

    adv = LeverageAdvisor()
    res_learn = adv.advise(
        equity=100.0,
        stop_dist_pct=1.0,
        mu_lcb=0.20,
        sigma_r=1.3,
        strategy_state="learn",
    )
    assert res_learn["risk_pct"] == pytest.approx(0.5, abs=1e-4)
    assert res_learn["leverage"] <= 5

    res_zero = adv.advise(
        equity=100.0,
        stop_dist_pct=1.0,
        mu_lcb=0.0,
        sigma_r=1.3,
        strategy_state="exploit_low",
    )
    assert res_zero["risk_pct"] <= 0.5


def test_leverage_is_minimal_safe() -> None:
    """K6: Leverage is chosen as minimal L meeting MAX_MARGIN_PCT_PER_TRADE, not maxed to L_cap."""
    from engine.leverage_advisor import LeverageAdvisor

    adv = LeverageAdvisor()
    # equity=100, risk=1.5% ($1.50), stop=1% + cost~0.11% -> notional ~ $135, margin cap 25% ($25) -> L_min = ceil(135/25) = 6
    res = adv.advise(
        equity=100.0,
        stop_dist_pct=1.0,
        mu_lcb=0.30,
        sigma_r=1.0,
        strategy_state="exploit_high",  # cap=12
    )
    assert 1 <= res["leverage"] < 12
    assert res["margin_usd"] <= 25.0 + 1e-6


def test_leverage_never_breaches_liq_buffer() -> None:
    """K6: Leverage L never exceeds L_liq where liq_distance < LIQ_BUFFER_X * stop_dist."""
    from engine.leverage_advisor import LeverageAdvisor

    adv = LeverageAdvisor()
    # Wide stop=10% with LIQ_BUFFER_X=2.0 -> need >=20% liq distance -> max safe L <= 4
    res = adv.advise(
        equity=100.0,
        stop_dist_pct=10.0,
        mu_lcb=0.30,
        sigma_r=1.0,
        strategy_state="exploit_high",
    )
    liq_dist_pct = (1.0 / res["leverage"] - 0.005) * 100.0
    assert liq_dist_pct >= 2.0 * 10.0 - 1e-6 or res["leverage"] == 1


def test_drawdown_throttle() -> None:
    """K6: Drawdown throttle scales risk down from 1.0x at 0% DD toward 0.25x at high DD."""
    from engine.leverage_advisor import LeverageAdvisor

    adv = LeverageAdvisor()
    r0 = adv.advise(equity=100.0, stop_dist_pct=1.5, mu_lcb=0.20, strategy_state="exploit_low", drawdown_pct=0.0)
    r10 = adv.advise(equity=100.0, stop_dist_pct=1.5, mu_lcb=0.20, strategy_state="exploit_low", drawdown_pct=10.0)
    assert r10["risk_pct"] < r0["risk_pct"]
    assert r10["risk_pct"] >= 0.25 * r0["risk_pct"] - 1e-6


def test_notional_derives_from_risk_not_leverage() -> None:
    """K6: Increasing leverage cap does not inflate notional (notional comes from risk/stop)."""
    from engine.leverage_advisor import LeverageAdvisor

    adv_low = LeverageAdvisor(Settings(LEVERAGE_CAP_EXPLOIT_LOW=5))
    adv_high = LeverageAdvisor(Settings(LEVERAGE_CAP_EXPLOIT_LOW=15))
    r_low = adv_low.advise(equity=100.0, stop_dist_pct=2.0, mu_lcb=0.15, strategy_state="exploit_low")
    r_high = adv_high.advise(equity=100.0, stop_dist_pct=2.0, mu_lcb=0.15, strategy_state="exploit_low")
    assert r_low["notional_usd"] == pytest.approx(r_high["notional_usd"], rel=1e-6)


# ---------------------------------------------------------------------------
# K5: Futures v2 Execution & WalletBalancer Tests
# ---------------------------------------------------------------------------

def test_no_silent_spot_fallback(tmp_path) -> None:
    """K5: With FUTURES_ONLY_MODE=True and ALLOW_SPOT_FALLBACK=False, insufficient futures rejects instead of Spot."""
    from engine.market_router import MarketRouter
    from tests.test_kelly_and_risk import make_valid_decision

    cfg = Settings(
        ENABLE_FUTURES=True,
        FUTURES_ONLY_MODE=True,
        ALLOW_SPOT_FALLBACK=False,
        DB_PATH=str(tmp_path / "no_spot.db"),
    )
    db = DatabaseManager(db_path=cfg.DB_PATH)
    try:
        router = MarketRouter(db=db, custom_settings=cfg)
        dec = make_valid_decision(symbol="BTC/USDT")
        route = router.route_market_and_size(decision=dec, spot_free_usdt=100.0, futures_free_usdt=0.50)
        assert route["approved"] is False
        assert route["market_type"] != "SPOT"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_wallet_split_warning_and_auto_transfer(tmp_path) -> None:
    """K5: WalletBalancer warns when AUTO_TRANSFER=False and transfers safely when AUTO_TRANSFER=True."""
    from engine.wallet_balancer import WalletBalancer

    class FakeExchange:
        def __init__(self) -> None:
            self.transfers: List[Dict[str, Any]] = []

        async def transfer_spot_to_futures(self, amount_usdt: float) -> Dict[str, Any]:
            self.transfers.append({"amount": amount_usdt})
            return {"id": "TR_1", "amount": amount_usdt}

    cfg_warn = Settings(AUTO_TRANSFER=False, FUTURES_WALLET_TARGET_PCT=95.0, DB_PATH=str(tmp_path / "wb1.db"))
    db1 = DatabaseManager(db_path=cfg_warn.DB_PATH)
    try:
        wb = WalletBalancer(db=db1, custom_settings=cfg_warn)
        res = await wb.check_and_balance(spot_free_usdt=40.0, futures_free_usdt=10.0, has_open_positions=False, has_open_orders=False)
        assert res["wallet_split_warning"] is True
        assert db1.get_state("wallet_split_warning") == "true"
    finally:
        db1.close()

    cfg_auto = Settings(AUTO_TRANSFER=True, FUTURES_WALLET_TARGET_PCT=95.0, DB_PATH=str(tmp_path / "wb2.db"))
    db2 = DatabaseManager(db_path=cfg_auto.DB_PATH)
    try:
        ex = FakeExchange()
        wb2 = WalletBalancer(db=db2, exchange=ex, custom_settings=cfg_auto)
        res2 = await wb2.check_and_balance(spot_free_usdt=40.0, futures_free_usdt=10.0, has_open_positions=False, has_open_orders=False)
        assert res2["transferred_usd"] == pytest.approx(37.5, abs=0.1)  # 95% of 50 = 47.5 -> transfer 37.5
        assert len(ex.transfers) == 1
    finally:
        db2.close()


@pytest.mark.asyncio
async def test_futures_entry_sl_liq_readback_and_short(tmp_path) -> None:
    """K5: Futures v2 verifies one-way/leverage readback, places SL, verifies liq price, and handles short."""
    from engine.futures_executor import FuturesExecutor

    class FakeFutClient:
        def __init__(self, readback_lev: int = 3, hedge_mode: bool = False, liq_price: float = 130.0) -> None:
            self.readback_lev = readback_lev
            self.hedge_mode = hedge_mode
            self.liq_price = liq_price
            self.orders: List[Dict[str, Any]] = []

        async def fetch_position_mode(self, symbol=None):
            return {"hedged": self.hedge_mode}

        async def set_margin_mode(self, mode, symbol):
            return {"mode": mode}

        async def set_leverage(self, lev, symbol):
            return {"leverage": lev}

        async def fetch_positions(self, symbols=None):
            return [{"symbol": "BTC/USDT:USDT", "contracts": 0.1, "leverage": self.readback_lev, "liquidationPrice": self.liq_price}]

        async def create_order(self, **kwargs):
            self.orders.append(kwargs)
            return {"id": f"ORD_{len(self.orders)}", "status": "closed", "filled": kwargs.get("amount"), "average": 100.0}

    # 1. Readback mismatch blocks entry
    ex_bad_lev = FakeFutClient(readback_lev=10)
    fe_bad = FuturesExecutor(client=ex_bad_lev)
    r_bad = await fe_bad.execute_futures_entry(symbol="BTC/USDT", side="short", quantity=0.1, entry_price=100.0, stop_price=103.0, target_leverage=3)
    assert r_bad["executed"] is False
    assert r_bad["reject_reason"] == "leverage_readback_mismatch"
    assert len(ex_bad_lev.orders) == 0

    # 2. Hedge mode enabled blocks entry
    ex_hedge = FakeFutClient(hedge_mode=True)
    fe_hedge = FuturesExecutor(client=ex_hedge)
    r_hedge = await fe_hedge.execute_futures_entry(symbol="BTC/USDT", side="short", quantity=0.1, entry_price=100.0, stop_price=103.0, target_leverage=3)
    assert r_hedge["executed"] is False
    assert r_hedge["reject_reason"] == "hedge_mode_enabled"

    # 3. Short entry + reduceOnly buy SL works
    ex_ok = FakeFutClient(readback_lev=3, liq_price=130.0)
    fe_ok = FuturesExecutor(client=ex_ok)
    r_ok = await fe_ok.execute_futures_entry(symbol="BTC/USDT", side="short", quantity=0.1, entry_price=100.0, stop_price=103.0, target_leverage=3)
    assert r_ok["executed"] is True
    assert ex_ok.orders[0]["side"] == "sell"
    assert ex_ok.orders[1]["type"] == "STOP_MARKET"
    assert ex_ok.orders[1]["side"] == "buy"
    assert ex_ok.orders[1]["params"]["reduceOnly"] is True

    # 4. Liquidation price violating LIQ_BUFFER_X closes immediately
    ex_liq = FakeFutClient(readback_lev=3, liq_price=99.5)
    fe_liq = FuturesExecutor(client=ex_liq)
    r_liq = await fe_liq.execute_futures_entry(symbol="BTC/USDT", side="long", quantity=0.1, entry_price=100.0, stop_price=97.0, target_leverage=3)
    assert r_liq["executed"] is False
    assert r_liq["reject_reason"] == "liquidation_buffer_breach"
    assert ex_liq.orders[-1]["params"]["reduceOnly"] is True
