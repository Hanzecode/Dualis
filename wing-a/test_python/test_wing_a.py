# wing_a/tests/test_wing_a.py
# ─────────────────────────────────────────────────────────────────────────────
#  WING A TEST SUITE
#  Tests every component without needing real ZMQ, AWS, or Alpaca connections.
#  All external dependencies are mocked.
#
#  Run:  cd wing_a && pytest tests/ -v
# ─────────────────────────────────────────────────────────────────────────────

import json
import sys
import time
import threading
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List
from unittest.mock import MagicMock, patch, call
import pytest

# Make wing_a packages importable from this test file
sys.path.insert(0, ".")


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def fresh_settings():
    """Return a Settings instance with test-safe values."""
    from config.settings import Settings, RiskConfig, PortfolioConfig, S3Config
    s = Settings()
    s.symbols = ["AAPL", "MSFT"]
    s.risk.max_order_size           = 1000
    s.risk.max_position_per_symbol  = 5000
    s.risk.max_orders_per_second    = 100
    s.risk.max_daily_loss_usd       = 500.0
    s.risk.max_total_notional_usd   = 100_000.0
    s.risk.min_signal_strength      = 0.10
    s.risk.min_confidence           = 0.50
    s.s3.max_signal_age_s           = 300
    s.portfolio.min_order_shares    = 1
    s.portfolio.target_annual_vol   = 0.15
    return s


def make_signal(symbol="AAPL", signal=0.7, confidence=0.8,
                target_qty=200, limit_price_bps=1890000, age_s=0):
    """Build a fresh AlphaSignal for testing."""
    from signal_ingestion.ingestor import AlphaSignal
    return AlphaSignal(
        symbol=symbol,
        signal=signal,
        confidence=confidence,
        target_qty=target_qty,
        limit_price_bps=limit_price_bps,
        generated_at=datetime.now(timezone.utc) - timedelta(seconds=age_s),
    )


def make_fill(symbol="AAPL", qty=100, price_bps=1890000, trade_id=1):
    """Build a fill dict matching C++ execution_engine.hpp trade_to_json()."""
    return {
        "trade_id":       trade_id,
        "symbol":         symbol,
        "price_bps":      price_bps,
        "price_dollars":  price_bps / 10_000.0,
        "quantity":       qty,
        "maker_order_id": 10,
        "taker_order_id": 11,
        "timestamp_ms":   int(time.time() * 1000),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  SIGNAL INGESTOR TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestSignalIngestor:

    def test_valid_signal_parsed_correctly(self):
        """A well-formed S3 signal file produces a correct AlphaSignal."""
        from signal_ingestion.ingestor import SignalIngestor

        received: List = []
        cfg = fresh_settings()

        with patch("boto3.client") as mock_boto:
            mock_s3 = MagicMock()
            mock_boto.return_value = mock_s3

            # Simulate S3 list returning one object
            mock_s3.list_objects_v2.return_value = {
                "Contents": [{"Key": "signals/latest/AAPL.json"}]
            }

            # Simulate S3 get returning a valid signal
            signal_json = json.dumps({
                "symbol":          "AAPL",
                "signal":          0.73,
                "confidence":      0.85,
                "target_qty":      500,
                "limit_price_bps": 1890000,
                "generated_at":    datetime.now(timezone.utc).isoformat(),
            })
            mock_s3.get_object.return_value = {
                "Body": MagicMock(read=lambda: signal_json.encode())
            }

            ingestor = SignalIngestor(on_signal=received.append, cfg=cfg)
            ingestor._poll_once()

        assert len(received) == 1
        sig = received[0]
        assert sig.symbol == "AAPL"
        assert abs(sig.signal - 0.73) < 1e-6
        assert sig.confidence == 0.85
        assert sig.target_qty == 500

    def test_stale_signal_rejected(self):
        """Signals older than max_signal_age_s are dropped."""
        from signal_ingestion.ingestor import SignalIngestor

        received: List = []
        cfg = fresh_settings()
        cfg.s3.max_signal_age_s = 10   # Very tight window

        with patch("boto3.client") as mock_boto:
            mock_s3 = MagicMock()
            mock_boto.return_value = mock_s3
            mock_s3.list_objects_v2.return_value = {
                "Contents": [{"Key": "signals/latest/AAPL.json"}]
            }
            # Signal generated 60s ago — should be stale
            stale_time = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
            signal_json = json.dumps({
                "symbol": "AAPL", "signal": 0.73, "confidence": 0.85,
                "target_qty": 500, "limit_price_bps": 1890000,
                "generated_at": stale_time,
            })
            mock_s3.get_object.return_value = {
                "Body": MagicMock(read=lambda: signal_json.encode())
            }
            ingestor = SignalIngestor(on_signal=received.append, cfg=cfg)
            ingestor._poll_once()

        assert len(received) == 0   # Stale signal must be dropped

    def test_unknown_symbol_rejected(self):
        """Signals for symbols outside the universe are dropped silently."""
        from signal_ingestion.ingestor import SignalIngestor

        received: List = []
        cfg = fresh_settings()
        cfg.symbols = ["AAPL", "MSFT"]   # TSLA not in universe

        with patch("boto3.client") as mock_boto:
            mock_s3 = MagicMock()
            mock_boto.return_value = mock_s3
            mock_s3.list_objects_v2.return_value = {
                "Contents": [{"Key": "signals/latest/TSLA.json"}]
            }
            signal_json = json.dumps({
                "symbol": "TSLA", "signal": 0.5, "confidence": 0.8,
                "target_qty": 100, "limit_price_bps": 0,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            })
            mock_s3.get_object.return_value = {
                "Body": MagicMock(read=lambda: signal_json.encode())
            }
            ingestor = SignalIngestor(on_signal=received.append, cfg=cfg)
            ingestor._poll_once()

        assert len(received) == 0

    def test_weak_signal_rejected(self):
        """Signals below min_signal_strength threshold are dropped."""
        from signal_ingestion.ingestor import SignalIngestor

        received: List = []
        cfg = fresh_settings()

        with patch("boto3.client") as mock_boto:
            mock_s3 = MagicMock()
            mock_boto.return_value = mock_s3
            mock_s3.list_objects_v2.return_value = {
                "Contents": [{"Key": "signals/latest/AAPL.json"}]
            }
            signal_json = json.dumps({
                "symbol": "AAPL", "signal": 0.05,   # Below 0.10 threshold
                "confidence": 0.9, "target_qty": 100,
                "limit_price_bps": 0,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            })
            mock_s3.get_object.return_value = {
                "Body": MagicMock(read=lambda: signal_json.encode())
            }
            ingestor = SignalIngestor(on_signal=received.append, cfg=cfg)
            ingestor._poll_once()

        assert len(received) == 0

    def test_malformed_json_dropped_safely(self):
        """Malformed JSON in S3 doesn't crash the ingestor."""
        from signal_ingestion.ingestor import SignalIngestor

        received: List = []
        cfg = fresh_settings()

        with patch("boto3.client") as mock_boto:
            mock_s3 = MagicMock()
            mock_boto.return_value = mock_s3
            mock_s3.list_objects_v2.return_value = {
                "Contents": [{"Key": "signals/latest/AAPL.json"}]
            }
            mock_s3.get_object.return_value = {
                "Body": MagicMock(read=lambda: b"THIS IS NOT JSON {{{{")
            }
            ingestor = SignalIngestor(on_signal=received.append, cfg=cfg)
            ingestor._poll_once()   # Must not raise

        assert len(received) == 0


# ─────────────────────────────────────────────────────────────────────────────
#  PORTFOLIO CONSTRUCTOR TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestPortfolioConstructor:

    def test_buy_signal_produces_buy_order(self):
        """A positive signal with sufficient confidence produces a BUY order."""
        from portfolio.constructor import PortfolioConstructor

        orders: List = []
        cfg = fresh_settings()
        pc = PortfolioConstructor(on_order=orders.append, cfg=cfg)

        # Set a mid-price so sizing works
        pc.update_mid_price("AAPL", 1_890_000)   # $189.00

        sig = make_signal(symbol="AAPL", signal=0.7, confidence=0.9, target_qty=200)
        pc.on_signal(sig)

        assert len(orders) == 1
        assert orders[0].side == "BUY"
        assert orders[0].symbol == "AAPL"
        assert orders[0].quantity > 0

    def test_sell_signal_produces_sell_order(self):
        """A negative signal produces a SELL order."""
        from portfolio.constructor import PortfolioConstructor

        orders: List = []
        cfg = fresh_settings()
        pc = PortfolioConstructor(on_order=orders.append, cfg=cfg)
        pc.update_mid_price("AAPL", 1_890_000)

        sig = make_signal(symbol="AAPL", signal=-0.6, confidence=0.8, target_qty=100)
        pc.on_signal(sig)

        assert len(orders) == 1
        assert orders[0].side == "SELL"

    def test_no_mid_price_no_order(self):
        """Without a market price, portfolio constructor cannot size — no order emitted."""
        from portfolio.constructor import PortfolioConstructor

        orders: List = []
        cfg = fresh_settings()
        pc = PortfolioConstructor(on_order=orders.append, cfg=cfg)
        # Do NOT set mid price

        sig = make_signal(symbol="AAPL", signal=0.7, confidence=0.9)
        pc.on_signal(sig)

        assert len(orders) == 0   # Can't size without a price

    def test_quantity_capped_at_max_order_size(self):
        """Order quantity never exceeds risk.max_order_size."""
        from portfolio.constructor import PortfolioConstructor

        orders: List = []
        cfg = fresh_settings()
        cfg.risk.max_order_size = 50   # Very small cap for test
        pc = PortfolioConstructor(on_order=orders.append, cfg=cfg)
        pc.update_mid_price("AAPL", 1_890_000)

        sig = make_signal(symbol="AAPL", signal=0.9, confidence=1.0, target_qty=1000)
        pc.on_signal(sig)

        assert len(orders) == 1
        assert orders[0].quantity <= cfg.risk.max_order_size

    def test_at_target_position_no_order(self):
        """If already at target, no order is emitted."""
        from portfolio.constructor import PortfolioConstructor

        orders: List = []
        cfg = fresh_settings()
        pc = PortfolioConstructor(on_order=orders.append, cfg=cfg)
        pc.update_mid_price("AAPL", 1_890_000)

        # Set current position to the target vol-targeted size
        # Simulate a previous buy fill that already matched target
        sig = make_signal(symbol="AAPL", signal=0.7, confidence=0.9, target_qty=100)
        pc.on_signal(sig)
        qty_ordered = orders[0].quantity if orders else 0

        # Now update position to match what was ordered
        pc.update_position("AAPL", qty_ordered, 1_890_000)
        orders.clear()

        # Same signal again — delta should be zero, no new order
        pc.on_signal(sig)
        assert len(orders) == 0


# ─────────────────────────────────────────────────────────────────────────────
#  RISK GATE TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestRiskGate:

    def _make_order(self, side="BUY", qty=100, symbol="AAPL", price_bps=1890000):
        from portfolio.constructor import Order
        return Order(
            symbol=symbol, side=side,
            order_type="LIMIT", quantity=qty, price_bps=price_bps
        )

    def test_normal_order_approved(self):
        from risk.gate import RiskGate
        approved: List = []
        cfg = fresh_settings()
        gate = RiskGate(on_approved=approved.append, cfg=cfg)
        gate.update_mid_price("AAPL", 189.0)

        gate.on_order(self._make_order(qty=100))
        assert len(approved) == 1

    def test_oversized_order_rejected(self):
        from risk.gate import RiskGate, RejectionReason
        approved: List = []
        cfg = fresh_settings()
        cfg.risk.max_order_size = 50
        gate = RiskGate(on_approved=approved.append, cfg=cfg)
        gate.update_mid_price("AAPL", 189.0)

        gate.on_order(self._make_order(qty=100))   # 100 > 50
        assert len(approved) == 0

    def test_circuit_breaker_halts_all_orders(self):
        """After daily loss limit is breached, every subsequent order is rejected."""
        from risk.gate import RiskGate
        approved: List = []
        cfg = fresh_settings()
        cfg.risk.max_daily_loss_usd = 100.0
        gate = RiskGate(on_approved=approved.append, cfg=cfg)
        gate.update_mid_price("AAPL", 189.0)

        # Trigger circuit breaker via on_fill with a big realised loss
        gate.on_fill("AAPL", "BUY", 100, 189.0, realised_pnl_usd=-200.0)
        assert gate.is_halted

        # Now try to send an order — should be blocked
        gate.on_order(self._make_order(qty=10))
        assert len(approved) == 0

    def test_reset_daily_lifts_halt(self):
        """After reset_daily(), the circuit breaker is lifted."""
        from risk.gate import RiskGate
        approved: List = []
        cfg = fresh_settings()
        cfg.risk.max_daily_loss_usd = 100.0
        gate = RiskGate(on_approved=approved.append, cfg=cfg)
        gate.update_mid_price("AAPL", 189.0)

        gate.on_fill("AAPL", "BUY", 100, 189.0, realised_pnl_usd=-200.0)
        assert gate.is_halted

        gate.reset_daily()
        assert not gate.is_halted

        gate.on_order(self._make_order(qty=10))
        assert len(approved) == 1   # Order goes through after reset

    def test_position_limit_rejection(self):
        """Order that would push position beyond max is rejected."""
        from risk.gate import RiskGate
        approved: List = []
        cfg = fresh_settings()
        cfg.risk.max_position_per_symbol = 200
        gate = RiskGate(on_approved=approved.append, cfg=cfg)
        gate.update_mid_price("AAPL", 189.0)

        # Build up position via fills
        gate.on_fill("AAPL", "BUY", 150, 189.0, 0.0)

        # Now try to buy 100 more: 150 + 100 = 250 > 200
        gate.on_order(self._make_order(qty=100))
        assert len(approved) == 0

    def test_rate_limit_rejects_burst(self):
        """Sending more than max_orders_per_second orders in 1s triggers rate limit."""
        from risk.gate import RiskGate
        from unittest.mock import patch
        approved: List = []
        cfg = fresh_settings()
        cfg.risk.max_orders_per_second = 3
        gate = RiskGate(on_approved=approved.append, cfg=cfg)
        gate.update_mid_price("AAPL", 189.0)

        # Patch time.monotonic so all 5 orders appear to happen at the same instant
        # This means the sliding window never expires between calls
        with patch("risk.gate.time.monotonic", return_value=1000.0):
            for _ in range(5):
                gate.on_order(self._make_order(qty=10))

        assert len(approved) == 3


# ─────────────────────────────────────────────────────────────────────────────
#  PNL TRACKER TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestPnLTracker:

    def _make_tracker(self):
        from pnl.tracker import PnLTracker
        pos_updates: List = []
        pnl_updates: List = []
        # Patch psycopg2 so we don't need a real DB
        with patch("psycopg2.connect", side_effect=Exception("no db in test")):
            tracker = PnLTracker(
                on_position_update=lambda s, q, c: pos_updates.append((s, q, c)),
                on_pnl_update=lambda s, r, u: pnl_updates.append((s, r, u)),
                cfg=fresh_settings(),
            )
        return tracker, pos_updates, pnl_updates

    def test_first_fill_opens_position(self):
        """A fill on a flat position opens a new long position."""
        tracker, pos_updates, _ = self._make_tracker()
        tracker.on_fill(make_fill("AAPL", qty=100, price_bps=1_890_000))

        pos = tracker.get_position("AAPL")
        assert pos is not None
        # Position should reflect the fill (side simplified to BUY in tracker)
        assert pos.avg_cost_bps == 1_890_000

    def test_mark_to_market_updates_unrealised_pnl(self):
        """After a fill, marking at a higher price increases unrealised PnL."""
        tracker, _, pnl_updates = self._make_tracker()
        tracker.on_fill(make_fill("AAPL", qty=100, price_bps=1_890_000))

        # Mark at $190.00 — $1 above cost
        tracker.on_market_price("AAPL", 1_900_000)

        pos = tracker.get_position("AAPL")
        assert pos.unrealised_pnl_bps > 0   # Should have unrealised gain

    def test_total_pnl_zero_on_no_fills(self):
        """Without any fills, total PnL is zero."""
        tracker, _, _ = self._make_tracker()
        assert tracker.total_pnl_usd() == 0.0

    def test_unknown_symbol_fill_ignored_safely(self):
        """Fill for a symbol not in the universe is dropped without crashing."""
        tracker, pos_updates, _ = self._make_tracker()
        # TSLA is not in fresh_settings().symbols = ["AAPL", "MSFT"]
        tracker.on_fill(make_fill("TSLA", qty=100, price_bps=2_500_000))
        assert len(pos_updates) == 0


# ─────────────────────────────────────────────────────────────────────────────
#  FILL SUBSCRIBER TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestFillSubscriber:

    def test_valid_fill_message_dispatched(self):
        """A well-formed 'fills {...}' ZMQ message calls on_fill once."""
        from gateway.fill_subscriber import FillSubscriber

        received: List = []
        cfg = fresh_settings()

        fill_dict = make_fill("AAPL", qty=50, price_bps=1_890_000)
        raw_msg = "fills " + json.dumps(fill_dict)

        with patch("zmq.Context.instance"), patch("zmq.Context"):
            sub = FillSubscriber(on_fill=received.append, cfg=cfg)
            sub._handle(raw_msg)   # Call handler directly — no real socket needed

        assert len(received) == 1
        assert received[0]["symbol"] == "AAPL"
        assert received[0]["quantity"] == 50

    def test_wrong_topic_ignored(self):
        """Messages with a different topic prefix are silently dropped."""
        from gateway.fill_subscriber import FillSubscriber

        received: List = []
        cfg = fresh_settings()

        with patch("zmq.Context.instance"), patch("zmq.Context"):
            sub = FillSubscriber(on_fill=received.append, cfg=cfg)
            sub._handle("orders {}")   # Wrong topic

        assert len(received) == 0

    def test_malformed_json_ignored(self):
        """Malformed JSON in fill message doesn't crash the subscriber."""
        from gateway.fill_subscriber import FillSubscriber

        received: List = []
        cfg = fresh_settings()

        with patch("zmq.Context.instance"), patch("zmq.Context"):
            sub = FillSubscriber(on_fill=received.append, cfg=cfg)
            sub._handle("fills NOT_JSON{{{{")   # Must not raise

        assert len(received) == 0


# ─────────────────────────────────────────────────────────────────────────────
#  ORDER MANAGER TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestOrderManager:

    def _make_manager(self):
        from order_manager.manager import OrderManager
        fills: List = []
        with patch("zmq.Context.instance") as mock_ctx:
            mock_socket = MagicMock()
            mock_ctx.return_value.socket.return_value = mock_socket
            mgr = OrderManager(on_fill=fills.append, cfg=fresh_settings())
            return mgr, fills, mock_socket

    def test_small_order_sent_immediately(self):
        """Orders under TWAP_THRESHOLD are sent in one ZMQ message."""
        from portfolio.constructor import Order
        mgr, fills, mock_socket = self._make_manager()

        order = Order("AAPL", "BUY", "LIMIT", 100, 1_890_000)
        mgr.on_order(order)

        # ZMQ send_string should have been called once
        mock_socket.send_string.assert_called_once()
        msg = json.loads(mock_socket.send_string.call_args[0][0])
        assert msg["type"] == "ORDER"
        assert msg["symbol"] == "AAPL"
        assert msg["quantity"] == 100

    def test_cancel_sends_correct_message(self):
        """cancel_order() sends a CANCEL message with the right order ID."""
        mgr, _, mock_socket = self._make_manager()
        mgr.cancel_order("AAPL", cpp_order_id=42)

        mock_socket.send_string.assert_called_once()
        msg = json.loads(mock_socket.send_string.call_args[0][0])
        assert msg["type"] == "CANCEL"
        assert msg["order_id"] == 42

    def test_fill_updates_managed_order_state(self):
        """A fill for the right symbol advances the managed order's filled_qty."""
        from portfolio.constructor import Order
        mgr, fills, mock_socket = self._make_manager()

        order = Order("AAPL", "BUY", "LIMIT", 100, 1_890_000)
        mgr.on_order(order)

        fill = make_fill("AAPL", qty=100, price_bps=1_890_000)
        mgr.on_fill(fill)

        # Fill should be forwarded to on_fill callback
        assert len(fills) == 1

    def test_zmq_error_handled_gracefully(self):
        """ZMQ send failure doesn't raise — order is marked rejected."""
        import zmq as zmq_module
        from portfolio.constructor import Order

        mgr, _, mock_socket = self._make_manager()
        # Make ZMQ send raise an error
        mock_socket.send_string.side_effect = zmq_module.ZMQError("test error")

        order = Order("AAPL", "BUY", "LIMIT", 100, 1_890_000)
        mgr.on_order(order)   # Must not raise

        # Check the managed order was marked REJECTED
        with mgr._lock:
            states = [o.state.name for o in mgr._orders.values()]
        assert any(s == "REJECTED" for s in states)


# ─────────────────────────────────────────────────────────────────────────────
#  INTEGRATION: SIGNAL → ORDER → RISK → ZMQ
# ─────────────────────────────────────────────────────────────────────────────

class TestEndToEnd:

    def test_signal_flows_to_zmq_push(self):
        """
        Full happy path: AlphaSignal → PortfolioConstructor → RiskGate → OrderManager → ZMQ.
        Uses mocked ZMQ socket; verifies the message sent matches the signal.
        """
        from signal_ingestion.ingestor import AlphaSignal
        from portfolio.constructor import PortfolioConstructor
        from risk.gate import RiskGate
        from order_manager.manager import OrderManager

        zmq_messages: List[dict] = []
        cfg = fresh_settings()

        # Build the chain
        with patch("zmq.Context.instance") as mock_ctx:
            mock_socket = MagicMock()
            mock_socket.send_string.side_effect = lambda msg, **kw: zmq_messages.append(json.loads(msg))
            mock_ctx.return_value.socket.return_value = mock_socket

            order_mgr = OrderManager(on_fill=lambda f: None, cfg=cfg)
            risk_gate = RiskGate(on_approved=order_mgr.on_order, cfg=cfg)
            portfolio = PortfolioConstructor(on_order=risk_gate.on_order, cfg=cfg)

            # Set market price and inject a signal with small target_qty to avoid TWAP
            portfolio.update_mid_price("AAPL", 1_890_000)
            risk_gate.update_mid_price("AAPL", 189.0)

            # Inject a signal with target_qty well below TWAP_THRESHOLD (500)
            sig = make_signal("AAPL", signal=0.8, confidence=0.9, target_qty=100)
            portfolio.on_signal(sig)

        # Should have produced exactly one ZMQ ORDER message
        order_msgs = [m for m in zmq_messages if m.get("type") == "ORDER"]
        assert len(order_msgs) == 1
        msg = order_msgs[0]
        assert msg["symbol"] == "AAPL"
        assert msg["side"] == "BUY"
        assert msg["quantity"] > 0
        assert msg["quantity"] <= cfg.risk.max_order_size

    def test_negative_signal_produces_sell(self):
        """A sell signal flows through the full chain and produces a SELL ZMQ message."""
        from portfolio.constructor import PortfolioConstructor
        from risk.gate import RiskGate
        from order_manager.manager import OrderManager

        zmq_messages: List[dict] = []
        cfg = fresh_settings()

        with patch("zmq.Context.instance") as mock_ctx:
            mock_socket = MagicMock()
            mock_socket.send_string.side_effect = lambda msg, **kw: zmq_messages.append(json.loads(msg))
            mock_ctx.return_value.socket.return_value = mock_socket

            order_mgr = OrderManager(on_fill=lambda f: None, cfg=cfg)
            risk_gate = RiskGate(on_approved=order_mgr.on_order, cfg=cfg)
            portfolio = PortfolioConstructor(on_order=risk_gate.on_order, cfg=cfg)

            portfolio.update_mid_price("AAPL", 1_890_000)
            risk_gate.update_mid_price("AAPL", 189.0)

            # Negative signal → SELL
            sig = make_signal("AAPL", signal=-0.7, confidence=0.85, target_qty=100)
            portfolio.on_signal(sig)

        order_msgs = [m for m in zmq_messages if m.get("type") == "ORDER"]
        assert len(order_msgs) == 1
        assert order_msgs[0]["side"] == "SELL"
