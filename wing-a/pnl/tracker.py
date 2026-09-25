# wing_a/pnl/tracker.py
# ─────────────────────────────────────────────────────────────────────────────
#  PNL TRACKER
#  Receives fills from FillSubscriber.
#  Maintains running positions and PnL (realised + unrealised).
#  Persists every fill and position update to TimescaleDB.
#  Sends PnL feedback to Wing B's PostgreSQL (for model retraining).
#  Notifies RiskGate and PortfolioConstructor of position changes.
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

import psycopg2                        # pip install psycopg2-binary
import psycopg2.extras

from config.settings import Settings, settings as default_settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  POSITION + PNL STATE
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PositionState:
    symbol:            str
    net_quantity:      int    = 0
    avg_cost_bps:      int    = 0      # VWAP cost basis
    realised_pnl_bps:  int    = 0      # From closed trades (locked in)
    unrealised_pnl_bps: int   = 0      # Mark-to-market on open position
    last_price_bps:    int    = 0      # Latest mid-price for MTM

    @property
    def total_pnl_usd(self) -> float:
        total_bps = self.realised_pnl_bps + self.unrealised_pnl_bps
        return total_bps / 10_000.0

    def mark_to_market(self, mid_bps: int) -> None:
        """Recompute unrealised PnL at current market price."""
        self.last_price_bps = mid_bps
        self.unrealised_pnl_bps = (
            self.net_quantity * (mid_bps - self.avg_cost_bps)
        )

    def apply_fill(self, side: str, qty: int, fill_price_bps: int) -> int:
        """
        Update position and cost basis from a fill.
        Returns realised PnL in bps for this fill (0 if adding to position).
        """
        delta = qty if side == "BUY" else -qty
        realised = 0

        if self.net_quantity == 0:
            # Opening new position
            self.avg_cost_bps = fill_price_bps
        elif (self.net_quantity > 0 and delta > 0) or (self.net_quantity < 0 and delta < 0):
            # Adding to existing position — update VWAP cost
            total_qty = self.net_quantity + delta
            if total_qty != 0:
                self.avg_cost_bps = (
                    (self.net_quantity * self.avg_cost_bps + delta * fill_price_bps)
                    // total_qty
                )
        else:
            # Reducing or flipping position — realise PnL
            closed_qty = min(abs(self.net_quantity), abs(delta))
            pnl_per_share = fill_price_bps - self.avg_cost_bps
            sign = 1 if self.net_quantity > 0 else -1
            realised = closed_qty * pnl_per_share * sign
            self.realised_pnl_bps += realised

        self.net_quantity += delta
        return realised


# ─────────────────────────────────────────────────────────────────────────────
#  PNL TRACKER
# ─────────────────────────────────────────────────────────────────────────────

class PnLTracker:
    """
    Stateful position and PnL book.
    Called from FillSubscriber's thread — uses no locks because CPython's
    GIL protects dict operations. For true multi-threaded safety, add a Lock.
    """

    def __init__(
        self,
        on_position_update: Callable[[str, int, int], None],  # (symbol, net_qty, avg_cost_bps)
        on_pnl_update: Callable[[str, float, float], None],   # (symbol, realised_usd, unrealised_usd)
        cfg: Settings = default_settings,
    ):
        self._on_position_update = on_position_update   # → PortfolioConstructor + RiskGate
        self._on_pnl_update = on_pnl_update             # → Dashboard / Wing B feedback
        self._cfg = cfg

        self._positions: Dict[str, PositionState] = {
            sym: PositionState(symbol=sym) for sym in cfg.symbols
        }

        # Synthetic cash ledger — not backed by any real account. Starts at
        # cfg.starting_cash_usd and moves only in on_fill(): a BUY fill debits
        # qty × price, a SELL fill credits it. This is the "wallet" half of
        # the accounting; PositionState above is the "holdings" half.
        self._cash_usd: float = cfg.starting_cash_usd

        # TimescaleDB connection for persistence
        self._db_conn: Optional[psycopg2.extensions.connection] = None
        self._connect_db()

    def on_fill(self, fill: dict) -> None:
        """
        Called by FillSubscriber for every confirmed fill from C++ engine.
        Updates position, computes PnL, persists to DB, notifies callbacks.
        """
        symbol        = fill["symbol"]
        qty           = int(fill["quantity"])
        fill_price_bps = int(fill["price_bps"])
        trade_id      = int(fill["trade_id"])
        timestamp_ms  = int(fill["timestamp_ms"])

        if symbol not in self._positions:
            logger.warning("Fill for unknown symbol %s — skipping", symbol)
            return

        pos = self._positions[symbol]

        # our_side (execution_engine.hpp::trade_to_json) is the side of OUR
        # position change. NOT taker_side: in this engine the taker is the
        # synthetic market order that crosses our resting limit, i.e. the
        # opposite of what we did. Missing key defaults to BUY (old payloads).
        side = fill.get("our_side", "BUY")
        realised_bps = pos.apply_fill(side, qty, fill_price_bps)
        realised_usd = realised_bps / 10_000.0
        unrealised_usd = pos.unrealised_pnl_bps / 10_000.0

        # Cash ledger: a BUY spends cash, a SELL raises it. Purely synthetic —
        # see Settings.starting_cash_usd — but it now moves the same direction
        # a real brokerage account would.
        fill_notional_usd = qty * (fill_price_bps / 10_000.0)
        self._cash_usd += -fill_notional_usd if side == "BUY" else fill_notional_usd

        logger.info(
            "[%s] Fill: side=%s qty=%d price=$%.2f realised=$%.2f pos=%d cash=$%.2f",
            symbol, side, qty, fill_price_bps / 10_000,
            realised_usd, pos.net_quantity, self._cash_usd
        )

        # Persist to TimescaleDB
        self._persist_fill(trade_id, symbol, fill_price_bps, qty, timestamp_ms, realised_usd)
        self._persist_position(pos)

        # Notify downstream
        self._on_position_update(symbol, pos.net_quantity, pos.avg_cost_bps)
        self._on_pnl_update(symbol, realised_usd, unrealised_usd)

    def on_market_price(self, symbol: str, mid_bps: int) -> None:
        """Mark-to-market all positions at current price."""
        if symbol in self._positions:
            pos = self._positions[symbol]
            pos.mark_to_market(mid_bps)
            self._on_pnl_update(
                symbol,
                pos.realised_pnl_bps / 10_000.0,
                pos.unrealised_pnl_bps / 10_000.0,
            )

    def total_pnl_usd(self) -> float:
        return sum(p.total_pnl_usd for p in self._positions.values())

    def get_position(self, symbol: str) -> Optional[PositionState]:
        return self._positions.get(symbol)

    @property
    def cash_usd(self) -> float:
        """Remaining synthetic cash — starts at cfg.starting_cash_usd."""
        return self._cash_usd

    def equity_usd(self) -> float:
        """
        Cash still sitting there + the mark-to-market value of every
        holding. Should always equal starting_cash_usd + total_pnl_usd() —
        that identity is the sanity check that the cash ledger and the
        mark-to-market ledger agree with each other.
        """
        market_value = sum(
            p.net_quantity * (p.last_price_bps or p.avg_cost_bps) / 10_000.0
            for p in self._positions.values()
        )
        return self._cash_usd + market_value

    # ── Database ──────────────────────────────────────────────────────────────

    def _connect_db(self) -> None:
        try:
            self._db_conn = psycopg2.connect(self._cfg.db.timescale_url)
            self._db_conn.autocommit = False
            logger.info("PnLTracker connected to TimescaleDB")
        except Exception as e:
            logger.error("PnLTracker DB connect failed: %s — running without persistence", e)
            self._db_conn = None

    def _persist_fill(self, trade_id: int, symbol: str, price_bps: int,
                      qty: int, timestamp_ms: int, realised_usd: float) -> None:
        if self._db_conn is None:
            return
        try:
            with self._db_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO trades
                        (trade_id, symbol, price_bps, quantity, realised_pnl_usd, executed_at)
                    VALUES (%s, %s, %s, %s, %s, to_timestamp(%s / 1000.0))
                    ON CONFLICT (trade_id) DO NOTHING
                    """,
                    (trade_id, symbol, price_bps, qty, realised_usd, timestamp_ms)
                )
            self._db_conn.commit()
        except Exception as e:
            logger.error("PnLTracker fill persist error: %s", e)
            self._db_conn.rollback()

    def _persist_position(self, pos: PositionState) -> None:
        if self._db_conn is None:
            return
        try:
            with self._db_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO positions
                        (symbol, net_quantity, avg_cost_bps,
                         realised_pnl_bps, unrealised_pnl_bps, updated_at)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (symbol) DO UPDATE SET
                        net_quantity      = EXCLUDED.net_quantity,
                        avg_cost_bps      = EXCLUDED.avg_cost_bps,
                        realised_pnl_bps  = EXCLUDED.realised_pnl_bps,
                        unrealised_pnl_bps = EXCLUDED.unrealised_pnl_bps,
                        updated_at        = NOW()
                    """,
                    (pos.symbol, pos.net_quantity, pos.avg_cost_bps,
                     pos.realised_pnl_bps, pos.unrealised_pnl_bps)
                )
            self._db_conn.commit()
        except Exception as e:
            logger.error("PnLTracker position persist error: %s", e)
            self._db_conn.rollback()
