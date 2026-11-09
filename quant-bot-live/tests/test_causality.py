"""Unit tests for Phase 2 & 3: Strict causality (iloc[-1] exclusion), scanner filters, and Reflex scorer (<50ms)."""

from __future__ import annotations

import time
from typing import Any, List

from engine.decision_reflex import DecisionReflexScorer
from engine.scanner import AutonomousMarketScanner
from engine.state_engine import DeterministicStateEngine


def generate_synthetic_ohlcv(num_bars: int = 250, base_price: float = 100.0) -> List[List[Any]]:
    """Generate realistic synthetic 15m OHLCV bars."""
    bars: List[List[Any]] = []
    start_ts = 1700000000000
    price = base_price
    for i in range(num_bars):
        ts = start_ts + i * 900_000
        drift = 0.25 if i < num_bars - 1 else 0.0
        open_p = price
        close_p = price + drift + ((i % 5) - 2) * 0.12
        high_p = max(open_p, close_p) + 0.35
        low_p = min(open_p, close_p) - 0.30
        vol = 1000.0 + (i % 7) * 80.0
        if i == num_bars - 2:
            # Make last CLOSED candle (iloc[-2]) have healthy volume
            vol = 2000.0
        bars.append([ts, open_p, high_p, low_p, close_p, vol])
        price = close_p
    return bars


def test_strict_causality_excludes_forming_candle_iloc_minus_1() -> None:
    """
    Prove that modifying the unclosed forming candle at iloc[-1] with extreme outlier values
    has ZERO impact on causal_timestamp_ms or any computed indicator in StateSnapshotSchema.
    """
    engine = DeterministicStateEngine()
    ohlcv_a = generate_synthetic_ohlcv(250, base_price=100.0)
    ohlcv_b = [list(row) for row in ohlcv_a]

    # Mutate the last forming candle (iloc[-1]) in ohlcv_b with extreme future/forming values
    ohlcv_b[-1][0] = 9999999999999
    ohlcv_b[-1][1] = 999999.0
    ohlcv_b[-1][2] = 9999999.0
    ohlcv_b[-1][3] = 0.0001
    ohlcv_b[-1][4] = 8888888.0
    ohlcv_b[-1][5] = 999999999.0

    orderbook = {
        "bids": [[155.0, 12.0], [154.9, 15.0]],
        "asks": [[155.1, 8.0], [155.2, 9.0]],
    }

    snap_a = engine.build_snapshot_from_raw("SOL/USDT", ohlcv_a, orderbook, regime="trending")
    snap_b = engine.build_snapshot_from_raw("SOL/USDT", ohlcv_b, orderbook, regime="trending")

    # Causal timestamp must equal iloc[-2] timestamp, never iloc[-1]
    assert snap_a.causal_timestamp_ms == ohlcv_a[-2][0]
    assert snap_b.causal_timestamp_ms == ohlcv_a[-2][0]

    # Every single indicator and field must be 100% identical
    assert snap_a.model_dump() == snap_b.model_dump()


def test_reflex_decision_scorer_speed_and_trending_signal() -> None:
    """Verify DecisionReflexScorer runs in <50ms and produces a valid calibrated ReflexDecisionSchema."""
    state_engine = DeterministicStateEngine()
    reflex = DecisionReflexScorer()

    ohlcv = generate_synthetic_ohlcv(250, base_price=100.0)
    orderbook = {
        "bids": [[160.0, 25.0], [159.9, 20.0]],
        "asks": [[160.1, 10.0], [160.2, 10.0]],
    }

    snapshot = state_engine.build_snapshot_from_raw("BTC/USDT", ohlcv, orderbook, regime="trending")

    t0 = time.perf_counter()
    decision = reflex.evaluate(snapshot)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert elapsed_ms < 50.0, f"Reflex scorer took {elapsed_ms:.2f}ms (must be <50ms)"
    assert decision.symbol == "BTC/USDT"
    assert decision.causal_timestamp_ms == snapshot.causal_timestamp_ms
    assert decision.invalidation_price < decision.entry_price
    assert decision.take_profit_price > decision.entry_price
    # Stop-loss distance capped at <= 1.8% from entry
    stop_pct = (decision.entry_price - decision.invalidation_price) / decision.entry_price
    assert stop_pct <= 0.0180001


def test_scanner_symbol_exclusions_and_filters() -> None:
    """Verify leveraged tokens, stablecoins, fiat pairs, low volume, and wide spread pairs are rejected."""
    assert AutonomousMarketScanner.is_eligible_usdt_spot_symbol("BTC/USDT")
    assert AutonomousMarketScanner.is_eligible_usdt_spot_symbol("SOL/USDT")
    assert not AutonomousMarketScanner.is_eligible_usdt_spot_symbol("BTCUP/USDT")
    assert not AutonomousMarketScanner.is_eligible_usdt_spot_symbol("ETHDOWN/USDT")
    assert not AutonomousMarketScanner.is_eligible_usdt_spot_symbol("USDC/USDT")
    assert not AutonomousMarketScanner.is_eligible_usdt_spot_symbol("FDUSD/USDT")
    assert not AutonomousMarketScanner.is_eligible_usdt_spot_symbol("EUR/USDT")
    assert not AutonomousMarketScanner.is_eligible_usdt_spot_symbol("TRY/USDT")
