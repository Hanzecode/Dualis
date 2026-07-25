# wing_a/main.py
# ─────────────────────────────────────────────────────────────────────────────
#  WING A — ENTRY POINT
#  Wires all components together and starts them in the correct order.
#
#  Startup sequence:
#  1. Logging
#  2. AlpacaFeed          — market data WebSocket → FlatOrderBook + pricing
#  3. FillSubscriber      — ZMQ SUB ← C++ engine fills
#  4. PnLTracker          — position + PnL state
#  5. RiskGate            — Python-side pre-trade checks
#  6. PortfolioConstructor — signal → order sizing
#  7. OrderManager        — TWAP/VWAP + ZMQ PUSH → C++ engine
#  8. SignalIngestor       — S3 poll → AlphaSignal
#  9. FastAPI dashboard   — WebSocket broadcast of live state
#
#  Shutdown: SIGINT/SIGTERM → stop all threads in reverse order
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import threading
from typing import Optional

import uvicorn   # pip install uvicorn

from config.settings import settings
from signal_ingestion.ingestor import SignalIngestor, AlphaSignal
from portfolio.constructor import PortfolioConstructor, Order
from risk.gate import RiskGate
from risk.circuit_breaker_explainer import (   # NEW
    CircuitBreakerExplainer, build_halt_context
)
from order_manager.manager import OrderManager
from gateway.fill_subscriber import FillSubscriber
from gateway.alpaca_feed import AlpacaFeed
from pnl.tracker import PnLTracker
from dashboard import api as dashboard_api


# ─────────────────────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────────────────────

def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(settings.log_file, mode="a"),
        ],
    )


logger = logging.getLogger("wing_a")


# ─────────────────────────────────────────────────────────────────────────────
#  WING A ORCHESTRATOR
# ─────────────────────────────────────────────────────────────────────────────

class WingA:
    """
    Owns all Wing A components and the wiring between them.
    The dependency graph (data flows downward):

    AlpacaFeed ──────────────────────────────────────────────────────────────┐
         │ on_quote(symbol, bid, ask)                                         │ MARKET_DEPTH
         ▼                                                                    ▼
    PortfolioConstructor.update_mid_price()          C++ engine ← ZMQ PUSH ──┘
    RiskGate.update_mid_price()
    PnLTracker.on_market_price()

    SignalIngestor ── on_signal ──► PortfolioConstructor.on_signal()
                                             │ on_order(Order)
                                             ▼
                                        RiskGate.on_order()
                                             │ on_approved(Order)
                                             ▼
                                        OrderManager.on_order()
                                             │ ZMQ PUSH
                                             ▼
                                        C++ ExecutionEngine
                                             │ ZMQ PUB "fills {...}"
                                             ▼
                                        FillSubscriber.on_fill(dict)
                                         /              \
                                        ▼                ▼
                               OrderManager         PnLTracker
                               .on_fill()           .on_fill()
                                                     /        \
                                                    ▼          ▼
                                            RiskGate     PortfolioConstructor
                                          .on_fill()     .update_position()
                                                    \
                                                     ▼
                                            dashboard_api.update_fill()
                                            dashboard_api.update_position()
    """

    def __init__(self):
        self._components = []

        # Rolling buffer of last 50 fills — passed to explainer at halt time.
        # 50 fills gives Claude enough context without bloating the prompt.
        # Protected by GIL (CPython list append is atomic for single items).
        self._fill_buffer: list = []
        self._FILL_BUFFER_MAX = 50

        # Track minutes since market open for post-mortem context
        self._market_open_time: float = 0.0

        # ── Build components bottom-up ────────────────────────────────────────

        # 1. AI explainer — no dependencies, build first
        self.explainer = CircuitBreakerExplainer()

        # 2. OrderManager
        self.order_manager = OrderManager(
            on_fill=self._on_fill_from_engine,
        )

        # 3. PnLTracker
        self.pnl_tracker = PnLTracker(
            on_position_update=self._on_position_update,
            on_pnl_update=self._on_pnl_update,
        )

        # 4. RiskGate — now wired with on_halt callback
        self.risk_gate = RiskGate(
            on_approved=self.order_manager.on_order,
            on_halt=self._on_halt,      # NEW: fires when circuit breaker trips
        )

        # 5. PortfolioConstructor
        self.portfolio = PortfolioConstructor(
            on_order=self.risk_gate.on_order,
        )

        # 6. FillSubscriber
        self.fill_subscriber = FillSubscriber(
            on_fill=self._on_fill_from_engine,
        )

        # 7. AlpacaFeed
        self.alpaca_feed = AlpacaFeed(
            on_quote=self._on_quote,
        )

        # 8. SignalIngestor
        self.signal_ingestor = SignalIngestor(
            on_signal=self._on_signal,
        )

        # 9. Give the dashboard API live references so its GET endpoints can
        # query real engine state instead of returning canned data.
        dashboard_api.register_components(
            risk_gate=self.risk_gate,
            pnl_tracker=self.pnl_tracker,
            alpaca_feed=self.alpaca_feed,
            signal_ingestor=self.signal_ingestor,
            order_manager=self.order_manager,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        import time as _time
        logger.info("Wing A starting — symbols=%s", settings.symbols)
        self._market_open_time = _time.monotonic()

        self.fill_subscriber.start()
        self.alpaca_feed.start()
        self.signal_ingestor.start()

        logger.info("Wing A running")

    def stop(self) -> None:
        logger.info("Wing A shutting down")
        self.signal_ingestor.stop()
        self.alpaca_feed.stop()
        self.fill_subscriber.stop()
        self.order_manager.shutdown()
        logger.info("Wing A stopped cleanly")

    # ── Cross-cutting callbacks (fan-out to multiple receivers) ───────────────

    def _on_fill_from_engine(self, fill: dict) -> None:
        """Fan-out: order state, PnL, fill buffer, dashboard."""
        # Maintain rolling fill buffer for explainer context
        self._fill_buffer.append(fill)
        if len(self._fill_buffer) > self._FILL_BUFFER_MAX:
            self._fill_buffer = self._fill_buffer[-self._FILL_BUFFER_MAX:]

        self.order_manager.on_fill(fill)
        self.pnl_tracker.on_fill(fill)
        dashboard_api.update_fill(fill)

    def _on_halt(self) -> None:
        """
        Called by RiskGate (in a background thread) the moment the circuit
        breaker fires. Assembles halt context and starts the AI explainer.
        The post-mortem appears in logs/halt_postmortem_*.txt within ~2s.
        """
        import time as _time
        minutes_open = int((_time.monotonic() - self._market_open_time) / 60)

        ctx = build_halt_context(
            daily_pnl_usd       = self.risk_gate.daily_pnl_usd,
            limit_usd           = settings.risk.max_daily_loss_usd,
            pnl_tracker         = self.pnl_tracker,
            recent_fills        = list(self._fill_buffer),  # snapshot at halt time
            market_open_minutes = minutes_open,
        )

        # on_complete: fires when Claude responds (~1–2s later).
        # Pushes the finished post-mortem to the dashboard WebSocket
        # so the browser updates without the user needing to refresh.
        def on_postmortem_ready(text: str) -> None:
            dashboard_api.update_postmortem(text)
            logger.info("Post-mortem pushed to dashboard (%d chars)", len(text))

        # Kick off async Claude API call — returns immediately
        self.explainer.explain_async(ctx, on_complete=on_postmortem_ready)

        # Notify dashboard immediately that halt has fired (before post-mortem arrives)
        dashboard_api.update_halt_status(
            halted=True,
            daily_pnl_usd=ctx.daily_pnl_usd,
            limit_usd=ctx.limit_usd,
        )

    def _on_quote(self, symbol: str, bid_usd: float, ask_usd: float) -> None:
        """
        Called by AlpacaFeed for every L2 quote update.
        Fan-out: update mid-price in portfolio, risk gate, PnL tracker, dashboard.
        """
        mid_usd = (bid_usd + ask_usd) / 2.0
        mid_bps = int(mid_usd * 10_000)

        self.portfolio.update_mid_price(symbol, mid_bps)
        self.risk_gate.update_mid_price(symbol, mid_usd)
        self.pnl_tracker.on_market_price(symbol, mid_bps)
        dashboard_api.update_quote(symbol, bid_usd, ask_usd)

    def _on_signal(self, signal) -> None:
        """Called by SignalIngestor for every validated AlphaSignal."""
        dashboard_api.update_signal(signal)
        self.portfolio.on_signal(signal)

    def _on_position_update(self, symbol: str, net_qty: int, avg_cost_bps: int) -> None:
        """Called by PnLTracker after each fill — updates downstream components."""
        self.portfolio.update_position(symbol, net_qty, avg_cost_bps)
        self.risk_gate.on_fill(
            symbol=symbol, side="BUY",   # Simplified — see audit gaps
            qty=0, price_usd=0.0, realised_pnl_usd=0.0
        )

    def _on_pnl_update(self, symbol: str, realised_usd: float, unrealised_usd: float) -> None:
        """Called by PnLTracker — forwards to dashboard API."""
        pos = self.pnl_tracker.get_position(symbol)
        if pos:
            dashboard_api.update_position(
                symbol=symbol,
                net_quantity=pos.net_quantity,
                avg_cost_bps=pos.avg_cost_bps,
                realised_pnl_usd=realised_usd,
                unrealised_pnl_usd=unrealised_usd,
            )


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging()
    logger.info("QuantCore Wing A initialising")

    wing = WingA()

    # ── Signal handlers ───────────────────────────────────────────────────────
    shutdown_event = threading.Event()

    def _handle_signal(sig, frame):
        logger.info("Received signal %s — initiating shutdown", sig)
        shutdown_event.set()

    signal.signal(signal.SIGINT,  _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # ── FastAPI in a background thread ────────────────────────────────────────
    # uvicorn runs its own asyncio event loop — run it in a daemon thread
    # so the main thread can block on shutdown_event
    loop = asyncio.new_event_loop()
    dashboard_api.set_event_loop(loop)

    def run_api():
        asyncio.set_event_loop(loop)
        config = uvicorn.Config(
            app=dashboard_api.app,
            host="0.0.0.0",
            port=8000,
            loop="none",      # We supply our own loop
            log_level="warning",
        )
        server = uvicorn.Server(config)
        loop.run_until_complete(server.serve())

    api_thread = threading.Thread(target=run_api, name="fastapi", daemon=True)
    api_thread.start()

    # ── Start Wing A ──────────────────────────────────────────────────────────
    wing.start()
    logger.info("Wing A running. Ctrl-C to stop.")

    # Block main thread until shutdown signal
    shutdown_event.wait()

    # ── Shutdown ──────────────────────────────────────────────────────────────
    wing.stop()
    loop.call_soon_threadsafe(loop.stop)
    logger.info("Wing A exited cleanly")


if __name__ == "__main__":
    main()