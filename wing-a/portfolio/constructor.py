# wing_a/portfolio/constructor.py
# ─────────────────────────────────────────────────────────────────────────────
#  PORTFOLIO CONSTRUCTOR
#  Translates AlphaSignal objects into concrete Order objects.
#
#  Steps:
#  1. Compute current position delta  (target - current)
#  2. Apply vol-targeting position size  (scale by confidence and vol)
#  3. Set limit price from FlatOrderBook mid (received via market data feed)
#  4. Emit an Order to the RiskGate
#
#  Receives:  AlphaSignal  (from SignalIngestor)
#  Emits:     Order        (to RiskGate → OrderManager)
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

import numpy as np

from config.settings import Settings, settings as default_settings
from signal_ingestion.ingestor import AlphaSignal

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  ORDER DATACLASS
#  This is Wing A's internal order representation before it reaches ZMQ.
#  Maps directly to the JSON format push_pull_receiver.hpp expects.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Order:
    symbol:      str
    side:        str          # "BUY" or "SELL"
    order_type:  str          # "LIMIT" or "MARKET"
    quantity:    int          # shares (always positive)
    price_bps:   int          # 0 for MARKET orders

    def to_zmq_dict(self) -> dict:
        """Serialise to the exact JSON format push_pull_receiver.hpp parses."""
        return {
            "type":       "ORDER",
            "symbol":     self.symbol,
            "side":       self.side,
            "order_type": self.order_type,
            "price_bps":  self.price_bps,
            "quantity":   self.quantity,
        }


@dataclass
class Position:
    """Current known position in one symbol."""
    symbol:       str
    net_quantity: int    = 0      # positive = long, negative = short
    avg_cost_bps: int    = 0      # volume-weighted average cost


# ─────────────────────────────────────────────────────────────────────────────
#  PORTFOLIO CONSTRUCTOR
# ─────────────────────────────────────────────────────────────────────────────

class PortfolioConstructor:
    """
    Stateful: tracks current positions and recent price history for vol estimation.
    Thread-safe: on_signal() may be called from the signal ingestor thread.
    All state updates use a simple dict — GIL protects us from races in CPython.
    """

    def __init__(
        self,
        on_order: Callable[[Order], None],
        cfg: Settings = default_settings,
    ):
        self._on_order = on_order    # Callback → RiskGate
        self._cfg = cfg

        # Current positions — updated by PnLTracker on fill confirmation
        self._positions: Dict[str, Position] = {
            sym: Position(symbol=sym) for sym in cfg.symbols
        }

        # Recent mid-price history per symbol — used for vol estimation
        # Deque of last vol_lookback_days closing prices
        self._price_history: Dict[str, list] = {
            sym: [] for sym in cfg.symbols
        }

        # Latest mid-price per symbol — set by market data feed
        self._mid_prices: Dict[str, Optional[int]] = {
            sym: None for sym in cfg.symbols
        }

    def on_signal(self, signal: AlphaSignal) -> None:
        """
        Called by SignalIngestor for each validated signal.
        Computes the desired order and emits it to on_order().
        """
        sym = signal.symbol

        # ── 1. Get current mid price from FlatOrderBook feed ─────────────────
        mid_bps = self._mid_prices.get(sym)
        if mid_bps is None:
            logger.warning("[%s] No market data yet — cannot size order", sym)
            return

        # ── 2. Compute target position size via vol-targeting ─────────────────
        vol = self._estimate_vol(sym)
        target_qty = self._vol_target_size(signal, mid_bps, vol)

        if target_qty < self._cfg.portfolio.min_order_shares:
            logger.debug("[%s] Computed qty %d below minimum — skipping", sym, target_qty)
            return

        # ── 3. Compute order delta (target - current) ─────────────────────────
        current_qty = self._positions[sym].net_quantity
        # target_signed: positive = want long, negative = want short
        target_signed = target_qty if signal.is_buy else -target_qty
        delta = target_signed - current_qty

        if delta == 0:
            logger.debug("[%s] Already at target position — no order needed", sym)
            return

        # ── 4. Build order ────────────────────────────────────────────────────
        side = "BUY" if delta > 0 else "SELL"
        abs_delta = abs(delta)

        if signal.limit_price_bps > 0:
            # Use Wing B's suggested limit price
            price_bps   = signal.limit_price_bps
            order_type  = "LIMIT"
        else:
            # Use current mid-price as limit (better than market — avoids crossing spread)
            price_bps   = mid_bps
            order_type  = "LIMIT"

        order = Order(
            symbol=sym,
            side=side,
            order_type=order_type,
            quantity=min(abs_delta, self._cfg.risk.max_order_size),
            price_bps=price_bps,
        )

        logger.info(
            "[%s] Emitting order: side=%s qty=%d price=$%.2f signal=%.3f conf=%.2f",
            sym, side, order.quantity, price_bps / 10_000, signal.signal, signal.confidence
        )

        self._on_order(order)   # → RiskGate

    def update_mid_price(self, symbol: str, mid_bps: int) -> None:
        """Called by AlpacaFeed on each market data update."""
        self._mid_prices[symbol] = mid_bps
        # Append to price history for vol estimation
        history = self._price_history[symbol]
        history.append(mid_bps)
        # Keep only the lookback window
        lookback = self._cfg.portfolio.vol_lookback_days
        if len(history) > lookback + 1:
            self._price_history[symbol] = history[-(lookback + 1):]

    def update_position(self, symbol: str, net_quantity: int, avg_cost_bps: int) -> None:
        """Called by PnLTracker after each fill confirmation."""
        self._positions[symbol] = Position(
            symbol=symbol,
            net_quantity=net_quantity,
            avg_cost_bps=avg_cost_bps,
        )

    # ── Private ───────────────────────────────────────────────────────────────

    def _estimate_vol(self, symbol: str) -> float:
        """
        Estimate realised daily volatility from recent price history.
        Returns annualised vol as a fraction (e.g. 0.30 = 30% annual).
        Falls back to a conservative default if insufficient history.
        """
        prices = self._price_history.get(symbol, [])
        if len(prices) < 5:
            # Not enough history — use a conservative default
            return 0.35   # 35% annual vol is a safe overestimate

        # Compute log returns
        arr = np.array(prices, dtype=float)
        log_returns = np.diff(np.log(arr))

        # Daily vol → annualised (252 trading days)
        daily_vol = float(np.std(log_returns, ddof=1))
        annual_vol = daily_vol * math.sqrt(252)

        return max(annual_vol, 0.05)   # Floor at 5% — avoid divide-by-near-zero

    def _vol_target_size(
        self,
        signal: AlphaSignal,
        mid_bps: int,
        annual_vol: float,
    ) -> int:
        """
        Vol-targeting position sizing:
            notional = target_annual_vol / stock_annual_vol × portfolio_value
            shares   = notional / stock_price
        Then scale down by signal confidence.

        This ensures more volatile stocks get smaller positions for the
        same risk contribution — the standard approach in equity quant funds.
        """
        target_vol = self._cfg.portfolio.target_annual_vol    # e.g. 0.15
        stock_price_usd = mid_bps / 10_000.0

        if stock_price_usd <= 0:
            return 0

        # Vol-targeted notional for this stock
        # Using a fixed $10,000 "risk budget" per position as a simplification.
        # In production: use actual portfolio NAV from the PnL tracker.
        risk_budget_usd = 10_000.0
        notional_usd = (target_vol / annual_vol) * risk_budget_usd

        # Cap at max_concentration × total portfolio (simplified)
        max_notional = self._cfg.risk.max_concentration * risk_budget_usd * len(self._cfg.symbols)
        notional_usd = min(notional_usd, max_notional)

        # Scale by confidence — low confidence → smaller position
        notional_usd *= signal.confidence

        # Convert to shares (floor to integer)
        shares = int(notional_usd / stock_price_usd)

        return max(0, min(shares, signal.target_qty))   # Don't exceed Wing B's suggestion