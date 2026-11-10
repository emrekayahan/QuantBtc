"""Main Async Entrypoint: orchestrates the Trading Engine, 30s Reconciler, APScheduler Jobs, and FastAPI LAN Dashboard."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Dict

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config.settings import settings
from dashboard.server import create_dashboard_app
from database.db_manager import DatabaseManager
from engine.decision_reflex import DecisionReflexScorer
from engine.exchange import BinanceExchangeWrapper
from engine.execution import ExecutionEngine
from engine.mistake_learner import MistakeLearner
from engine.openrouter_pool import OpenRouterPool
from engine.pre_trade_analyzer import PreTradeAnalyzer
from engine.reconciler import StateReconciler
from engine.telegram_bot import TelegramNotifier
from engine.risk_engine import RiskEngine
from engine.scanner import AutonomousMarketScanner
from engine.state_engine import DeterministicStateEngine
from review.overnight_review import OvernightBrierReviewer


def configure_logging() -> None:
    log_dir = Path(settings.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / "quant_bot.log", encoding="utf-8"),
        ],
    )


logger = logging.getLogger("quant_bot.main")


class AutonomousQuantEngineOrchestrator:
    """Master orchestrator running Layer 1 Brain, Layer 2 Reflex, Reconciler, and LAN Web Dashboard."""

    def __init__(self) -> None:
        self.db = DatabaseManager()
        self.openrouter_pool = OpenRouterPool()
        self.exchange = BinanceExchangeWrapper(db=self.db)
        self.scanner = AutonomousMarketScanner(
            exchange=self.exchange,
            db=self.db,
            openrouter_pool=self.openrouter_pool,
        )
        self.state_engine = DeterministicStateEngine(exchange=self.exchange, db=self.db)
        self.reflex_scorer = DecisionReflexScorer(db=self.db)
        self.risk_engine = RiskEngine(db=self.db, exchange=self.exchange)
        self.telegram = TelegramNotifier(
            db=self.db,
            risk_engine=self.risk_engine,
        )
        self.mistake_learner = MistakeLearner(
            db=self.db,
            openrouter_pool=self.openrouter_pool,
            telegram=self.telegram,
        )
        self.pre_trade_analyzer = PreTradeAnalyzer(
            db=self.db,
            exchange=self.exchange,
            openrouter_pool=self.openrouter_pool,
            telegram=self.telegram,
            mistake_learner=self.mistake_learner,
        )
        self.execution_engine = ExecutionEngine(
            exchange=self.exchange,
            db=self.db,
            risk_engine=self.risk_engine,
            telegram=self.telegram,
            pre_trade_analyzer=self.pre_trade_analyzer,
        )
        self.reconciler = StateReconciler(
            exchange=self.exchange,
            db=self.db,
            execution=self.execution_engine,
            telegram=self.telegram,
            mistake_learner=self.mistake_learner,
        )
        self.overnight_reviewer = OvernightBrierReviewer(
            db=self.db,
            openrouter_pool=self.openrouter_pool,
        )
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self._last_evaluated_candle_ts: Dict[str, int] = {}
        self._stop_event = asyncio.Event()

    async def initial_bootstrap(self) -> None:
        logger.info("Starting initial bootstrap of Autonomous Quantitative Trading Engine v3.0...")
        try:
            drift_ms = await self.exchange.verify_clock_drift()
            logger.info("Initial NTP/Binance clock drift: %d ms", drift_ms)
        except Exception as exc:
            logger.warning("Clock drift check warning during bootstrap: %s", exc)

        if self.cfg_has_keys():
            try:
                await self.reconciler.reconcile_cycle()
            except Exception as exc:
                logger.warning("Initial balance/position reconciliation warning: %s", exc)

        try:
            await self.exchange.ensure_markets_loaded()
            await self.scanner.run_scan_cycle()
        except Exception as exc:
            logger.error("Initial market scan failed (will retry on schedule): %s", exc)

    @staticmethod
    def cfg_has_keys() -> bool:
        return settings.has_valid_binance_keys

    async def evaluate_watchlist_reflex_cycle(self) -> None:
        """
        Layer 2 Reflex Loop:
        Evaluates closed candles across the active watchlist.
        If a new closed candle is detected (or updated telemetry needed), scores setup in <50ms
        and passes qualifying proposals to the Hard Deterministic Risk & Execution Gate.
        """
        if self.db.is_system_halted():
            logger.debug("System is halted; skipping Reflex execution cycle.")
            return

        watchlist = self.db.get_watchlist()
        if not watchlist:
            return

        free_usdt = float(self.db.get_state("free_usdt", "0.0") or 0.0)

        for item in watchlist:
            symbol = item["symbol"]
            try:
                snapshot = await self.state_engine.capture_live_snapshot(
                    symbol=symbol,
                    timeframe=settings.TIMEFRAME,
                    limit=250,
                )
                decision = self.reflex_scorer.evaluate(snapshot)

                last_ts = self._last_evaluated_candle_ts.get(symbol, 0)
                is_new_closed_candle = snapshot.causal_timestamp_ms > last_ts
                self._last_evaluated_candle_ts[symbol] = snapshot.causal_timestamp_ms

                if (
                    is_new_closed_candle
                    and decision.direction == "long"
                    and decision.setup_quality >= settings.MIN_SETUP_QUALITY
                    and decision.direction_confidence >= self.risk_engine.get_active_min_confidence()
                    and self.cfg_has_keys()
                ):
                    logger.info(
                        "High-conviction setup detected on %s (Q=%d, Conf=%.2f, Regime=%s). Passing to Risk Gate...",
                        symbol,
                        decision.setup_quality,
                        decision.direction_confidence,
                        decision.regime,
                    )
                    exec_res = await self.execution_engine.execute_approved_entry(
                        decision=decision,
                        free_usdt=free_usdt,
                    )
                    if exec_res.get("executed"):
                        logger.info(
                            "LIVE ORDER EXECUTED & SERVER-SIDE SL ARMED: %s (PosID=%s)",
                            symbol,
                            exec_res.get("position_id"),
                        )
                        if exec_res.get("trading_mode") == "SPOT":
                            free_usdt = max(0.0, free_usdt - float(exec_res.get("position_usd", 0.0)))
                    else:
                        logger.info(
                            "Proposal for %s not executed: %s", symbol, exec_res.get("reason")
                        )
            except Exception as exc:
                logger.warning("Reflex evaluation warning on %s: %s", symbol, exc)

    async def safe_reconcile_job(self) -> None:
        if not self.cfg_has_keys():
            return
        try:
            await self.reconciler.reconcile_cycle()
        except Exception as exc:
            logger.warning("30s Reconciler job warning: %s", exc)

    async def safe_scanner_job(self) -> None:
        try:
            await self.scanner.run_scan_cycle()
        except Exception as exc:
            logger.error("1h Brain Scanner job error: %s", exc)

    async def safe_clock_sync_job(self) -> None:
        try:
            await self.exchange.verify_clock_drift()
        except Exception as exc:
            logger.warning("15m Clock drift check warning: %s", exc)

    async def safe_overnight_brier_job(self) -> None:
        try:
            res = await self.overnight_reviewer.run_overnight_review()
            logger.info(
                "00:05 UTC Overnight Brier Review complete: BS=%.4f (n=%d)",
                res["brier_score"],
                res["sample_count"],
            )
            await self.telegram.send_message(
                f"🌙 *00:05 UTC Gece Denetim Raporu*\n\n"
                f"• Brier Skoru: `{res.get('brier_score', 0):.4f}`\n"
                f"• 24s İşlem Sayısı: `{res.get('sample_count', 0)}`\n"
                f"• Min Güven Eşiği: `%{res.get('min_confidence_threshold', 0.8)*100:.0f}`"
            )
        except Exception as exc:
            logger.error("00:05 UTC Overnight Brier Review error: %s", exc)

    def setup_schedules(self) -> None:
        # 1. 30-second state sync between DuckDB and Binance Spot
        self.scheduler.add_job(
            self.safe_reconcile_job,
            trigger=IntervalTrigger(seconds=30),
            id="reconciler_30s",
            replace_existing=True,
            max_instances=1,
        )
        # 2. 60-second Reflex watchlist candle evaluation loop
        self.scheduler.add_job(
            self.evaluate_watchlist_reflex_cycle,
            trigger=IntervalTrigger(seconds=60),
            id="reflex_60s",
            replace_existing=True,
            max_instances=1,
        )
        # 3. 15-minute clock drift verification
        self.scheduler.add_job(
            self.safe_clock_sync_job,
            trigger=IntervalTrigger(minutes=15),
            id="clock_sync_15m",
            replace_existing=True,
            max_instances=1,
        )
        # 4. 1-hour Layer 1 Brain Scanner & Macro Regime update
        self.scheduler.add_job(
            self.safe_scanner_job,
            trigger=IntervalTrigger(hours=1),
            id="brain_scanner_1h",
            replace_existing=True,
            max_instances=1,
        )
        # 5. 00:05 UTC Overnight Brier Score Calibration
        self.scheduler.add_job(
            self.safe_overnight_brier_job,
            trigger=CronTrigger(hour=0, minute=5, timezone="UTC"),
            id="overnight_brier_0005",
            replace_existing=True,
            max_instances=1,
        )

    async def run(self) -> None:
        await self.initial_bootstrap()
        self.setup_schedules()
        self.scheduler.start()
        await self.telegram.start_polling()
        await self.telegram.notify_system_event(
            "Sistem Başlatıldı",
            f"🤖 *QuantBot Live v3.0 Aktif*\n\n"
            f"• Sunucu: `Canlı Üretim (Spot)`\n"
            f"• Bakiye: `${float(self.db.get_state('total_equity_usd', '0.0') or 0.0):,.2f} USDT`\n"
            f"• Rejim: `{self.db.get_state('macro_regime', 'mean_reverting').upper()}`\n"
            f"• Komutları görmek için: `/yardim`"
        )

        # Run initial reflex evaluation pass immediately after bootstrap
        await self.evaluate_watchlist_reflex_cycle()

        app = create_dashboard_app(
            db=self.db,
            exchange=self.exchange,
            risk_engine=self.risk_engine,
            execution_engine=self.execution_engine,
            openrouter_pool=self.openrouter_pool,
        )

        uv_config = uvicorn.Config(
            app=app,
            host=settings.DASHBOARD_HOST,
            port=settings.DASHBOARD_PORT,
            log_level="info",
        )
        server = uvicorn.Server(uv_config)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._handle_shutdown_signal, server)
            except NotImplementedError:
                pass

        logger.info(
            "Local LAN Dashboard live at http://%s:%d",
            settings.DASHBOARD_HOST,
            settings.DASHBOARD_PORT,
        )
        try:
            await server.serve()
        finally:
            logger.info("Shutting down scheduler, CCXT exchange session, and DuckDB...")
            await self.telegram.stop()
            if self.scheduler.running:
                self.scheduler.shutdown(wait=False)
            await self.exchange.close()
            self.db.close()

    def _handle_shutdown_signal(self, server: uvicorn.Server) -> None:
        logger.info("Received termination signal. Initiating graceful shutdown...")
        server.should_exit = True
        self._stop_event.set()


async def async_main() -> None:
    configure_logging()
    orchestrator = AutonomousQuantEngineOrchestrator()
    await orchestrator.run()


if __name__ == "__main__":
    asyncio.run(async_main())
