# wing_a/risk/gate.py
# ─────────────────────────────────────────────────────────────────────────────
#  PYTHON RISK GATE
#  First line of defence before orders reach the C++ engine.
#  The C++ RiskManager is the second gate — both must pass.
#
#  Checks in order:
#  1. Circuit breaker — halt if daily loss exceeded
#  2. Order size limit
#  3. Rate limit (orders per second)
#  4. Position limit (would this order breach max position?)
#  5. Notional limit (would this push total portfolio value too high?)
#
#  Receives:  Order  (from PortfolioConstructor)
#  Emits:     Order  (to OrderManager) or drops it with a log
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Dict, Optional, Optional

from config.settings import Settings, settings as default_settings
from portfolio.constructor import Order, Position

logger = logging.getLogger(__name__)


class RejectionReason(Enum):
    APPROVED        = auto()
    CIRCUIT_BREAKER = auto()   # Daily loss limit breached — all trading halted
    ORDER_SIZE      = auto()   # Single order too large
    RATE_LIMIT      = auto()   # Too many orders per second
    POSITION_LIMIT  = auto()   # Would breach max position in this symbol
    NOTIONAL_LIMIT  = auto()   # Would breach total portfolio notional cap


@dataclass
class CheckResult:
    reason:  RejectionReason
    message: str = ""

    @property
    def approved(self) -> bool:
        return self.reason == RejectionReason.APPROVED


class RiskGate:
    """
    Thread-safe Python-side risk gate.
    Uses a threading.Lock for position/PnL state.
    Rate limiting uses a sliding window (deque of timestamps).
    """

    def __init__(
        self,
        on_approved: Callable[[Order], None],
        cfg: Settings = default_settings,
        on_halt: Optional[Callable[[], None]] = None,   # NEW: called once when breaker fires
    ):
        self._on_approved = on_approved   # → OrderManager
        self._on_halt = on_halt           # → WingA._on_halt() — triggers post-mortem
        self._cfg = cfg

        self._lock = threading.Lock()

        # Current positions (updated by PnLTracker on fill)
        self._positions: Dict[str, int] = {s: 0 for s in cfg.symbols}

        # Current mid-prices for notional calculation (updated by market feed)
        self._mid_prices: Dict[str, float] = {s: 0.0 for s in cfg.symbols}

        # Daily PnL tracking
        self._daily_pnl_usd: float = 0.0
        self._halted: bool = False

        # Rate limiting: sliding window of order timestamps (last 1 second)
        self._order_timestamps: deque = deque()

    # ── Public interface ──────────────────────────────────────────────────────

    def on_order(self, order: Order) -> None:
        """Called by PortfolioConstructor. Checks risk and routes to OrderManager."""
        result = self._check(order)
        if result.approved:
            logger.debug("[%s] Risk gate APPROVED: side=%s qty=%d",
                         order.symbol, order.side, order.quantity)
            self._on_approved(order)   # → OrderManager
        else:
            logger.warning("[%s] Risk gate REJECTED (%s): %s",
                           order.symbol, result.reason.name, result.message)

    def on_fill(self, symbol: str, side: str, qty: int,
                price_usd: float, realised_pnl_usd: float) -> None:
        """Called by PnLTracker after each confirmed fill."""
        with self._lock:
            delta = qty if side == "BUY" else -qty
            self._positions[symbol] = self._positions.get(symbol, 0) + delta
            self._daily_pnl_usd += realised_pnl_usd

            # Check if we've hit the daily loss limit
            if self._daily_pnl_usd < -self._cfg.risk.max_daily_loss_usd:
                if not self._halted:
                    logger.critical(
                        "CIRCUIT BREAKER TRIPPED — daily loss $%.2f exceeds limit $%.2f",
                        abs(self._daily_pnl_usd), self._cfg.risk.max_daily_loss_usd
                    )
                    self._halted = True
                    # Fire on_halt in its own thread — never block the fill path.
                    # The callback triggers the Claude API post-mortem (async).
                    if self._on_halt:
                        import threading
                        threading.Thread(
                            target=self._on_halt,
                            name="halt-notify",
                            daemon=True,
                        ).start()

    def update_mid_price(self, symbol: str, price_usd: float) -> None:
        with self._lock:
            self._mid_prices[symbol] = price_usd

    def reset_daily(self) -> None:
        """Call at market open each day to reset loss counter and lift halt."""
        with self._lock:
            self._daily_pnl_usd = 0.0
            self._halted = False
        logger.info("Risk gate: daily reset complete")

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def daily_pnl_usd(self) -> float:
        return self._daily_pnl_usd

    def total_notional_usd(self) -> float:
        """Public accessor — acquires lock itself."""
        with self._lock:
            return self._total_notional_unlocked()

    def _total_notional_unlocked(self) -> float:
        """Internal version — must be called with self._lock already held."""
        return sum(
            abs(qty) * self._mid_prices.get(sym, 0.0)
            for sym, qty in self._positions.items()
        )

    # ── Private ───────────────────────────────────────────────────────────────

    def _check(self, order: Order) -> CheckResult:
        """Run all checks in priority order. Return on first failure."""
        with self._lock:
            # 1. Circuit breaker — always first
            if self._halted:
                return CheckResult(
                    RejectionReason.CIRCUIT_BREAKER,
                    f"daily loss ${abs(self._daily_pnl_usd):.2f} exceeds limit"
                )

            # 2. Order size
            if order.quantity > self._cfg.risk.max_order_size:
                return CheckResult(
                    RejectionReason.ORDER_SIZE,
                    f"qty={order.quantity} > max={self._cfg.risk.max_order_size}"
                )

            # 3. Rate limit — sliding window, last 1 second
            now = time.monotonic()
            # Remove timestamps older than 1 second
            while self._order_timestamps and self._order_timestamps[0] < now - 1.0:
                self._order_timestamps.popleft()
            if len(self._order_timestamps) >= self._cfg.risk.max_orders_per_second:
                return CheckResult(
                    RejectionReason.RATE_LIMIT,
                    f"{len(self._order_timestamps)} orders in last 1s "
                    f"(limit={self._cfg.risk.max_orders_per_second})"
                )
            self._order_timestamps.append(now)   # Record this order attempt

            # 4. Position limit
            current = self._positions.get(order.symbol, 0)
            delta = order.quantity if order.side == "BUY" else -order.quantity
            projected = current + delta
            if abs(projected) > self._cfg.risk.max_position_per_symbol:
                return CheckResult(
                    RejectionReason.POSITION_LIMIT,
                    f"projected={projected} would exceed max={self._cfg.risk.max_position_per_symbol}"
                )

            # 5. Total notional check
            price_usd = self._mid_prices.get(order.symbol, 0.0)
            order_notional = order.quantity * price_usd
            if self._total_notional_unlocked() + order_notional > self._cfg.risk.max_total_notional_usd:
                return CheckResult(
                    RejectionReason.NOTIONAL_LIMIT,
                    f"total notional would exceed ${self._cfg.risk.max_total_notional_usd:,.0f}"
                )

            return CheckResult(RejectionReason.APPROVED)
