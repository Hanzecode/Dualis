# wing_a/tests/test_circuit_breaker_explainer.py
# ─────────────────────────────────────────────────────────────────────────────
#  CIRCUIT BREAKER EXPLAINER TESTS
#  Tests every layer without making real API calls:
#    - build_prompt()           — prompt content and structure
#    - call_claude()            — HTTP mocking
#    - CircuitBreakerExplainer  — async flow, file write, error handling
#    - RiskGate integration     — on_halt fires at the right moment
#    - WingA integration        — fill buffer population, context assembly
#
#  Run:  cd wing_a && pytest tests/test_circuit_breaker_explainer.py -v
# ─────────────────────────────────────────────────────────────────────────────

import json
import os
import sys
import time
import threading
from datetime import datetime, timezone, timedelta
from typing import List
from unittest.mock import MagicMock, patch, call
import pytest

sys.path.insert(0, ".")

from risk.circuit_breaker_explainer import (
    PositionSnapshot, FillRecord, HaltContext,
    build_prompt, call_claude, CircuitBreakerExplainer,
    build_halt_context,
)


# ─────────────────────────────────────────────────────────────────────────────
#  FIXTURES
# ─────────────────────────────────────────────────────────────────────────────

def make_context(
    daily_pnl=-1300.0,
    limit=1000.0,
    n_positions=2,
    n_fills=5,
):
    """Build a realistic HaltContext for testing."""
    positions = [
        PositionSnapshot(
            symbol="TSLA", net_quantity=100,
            avg_cost_usd=251.00, last_price_usd=238.00,
            unrealised_pnl_usd=-1300.0, realised_pnl_usd=-147.0,
        ),
        PositionSnapshot(
            symbol="NVDA", net_quantity=50,
            avg_cost_usd=882.00, last_price_usd=865.00,
            unrealised_pnl_usd=-850.0, realised_pnl_usd=0.0,
        ),
    ][:n_positions]

    now_ms = int(time.time() * 1000)
    fills = [
        FillRecord(
            symbol="TSLA", side="BUY", quantity=50,
            price_usd=251.00, realised_pnl=0.0,
            timestamp_ms=now_ms - (i * 60_000),
        )
        for i in range(n_fills)
    ]

    return HaltContext(
        daily_pnl_usd=daily_pnl,
        limit_usd=limit,
        positions=positions,
        recent_fills=fills,
        market_open_minutes=47,
    )


MOCK_CLAUDE_RESPONSE = {
    "content": [
        {
            "type": "text",
            "text": (
                "1. WHAT HAPPENED\n"
                "Trading halted 47 minutes into the session due to a $1,300 loss "
                "driven primarily by TSLA long position. Unrealised losses of $1,300 "
                "across TSLA and NVDA exceeded the $1,000 daily limit.\n\n"
                "2. LIKELY CAUSE\n"
                "Sharp intraday decline in TSLA ($251 → $238, -5.2%). "
                "NVDA also declined ($882 → $865, -1.9%).\n\n"
                "3. WHAT TO CHECK BEFORE RE-ENABLING\n"
                "• Review TSLA news since open\n"
                "• Check if sector-wide move or TSLA-specific\n"
                "• Verify position sizing relative to volatility\n"
                "• Confirm stop-loss logic working correctly\n\n"
                "4. RISK LIMIT REVIEW\n"
                "Consider tightening daily limit to $800 or reducing TSLA position cap "
                "from 10,000 to 5,000 shares given recent volatility."
            )
        }
    ],
    "model": "claude-sonnet-4-20250514",
    "usage": {"input_tokens": 420, "output_tokens": 180},
}


# ─────────────────────────────────────────────────────────────────────────────
#  PROMPT BUILDER TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildPrompt:

    def test_prompt_contains_daily_pnl(self):
        ctx = make_context(daily_pnl=-1247.50)
        prompt = build_prompt(ctx)
        assert "-1247.50" in prompt or "1247.50" in prompt

    def test_prompt_contains_limit(self):
        ctx = make_context(limit=1000.0)
        prompt = build_prompt(ctx)
        assert "1000" in prompt

    def test_prompt_contains_all_symbols(self):
        ctx = make_context()
        prompt = build_prompt(ctx)
        assert "TSLA" in prompt
        assert "NVDA" in prompt

    def test_prompt_contains_fill_timestamps(self):
        """Recent fills should appear in the prompt with timestamps."""
        ctx = make_context(n_fills=3)
        prompt = build_prompt(ctx)
        # Fills section should be present
        assert "FILLS" in prompt.upper() or "fills" in prompt.lower()

    def test_prompt_contains_four_required_sections(self):
        """The prompt must explicitly ask for all four post-mortem sections."""
        ctx = make_context()
        prompt = build_prompt(ctx)
        assert "WHAT HAPPENED" in prompt
        assert "LIKELY CAUSE" in prompt
        assert "WHAT TO CHECK" in prompt
        assert "RISK LIMIT REVIEW" in prompt

    def test_prompt_flat_positions_excluded(self):
        """Positions with net_quantity=0 should not appear in prompt."""
        ctx = make_context()
        ctx.positions.append(PositionSnapshot(
            symbol="AAPL", net_quantity=0,
            avg_cost_usd=189.0, last_price_usd=189.0,
            unrealised_pnl_usd=0.0, realised_pnl_usd=0.0,
        ))
        prompt = build_prompt(ctx)
        # AAPL should not appear — it's flat
        assert "AAPL" not in prompt

    def test_prompt_minutes_open_included(self):
        ctx = make_context()
        ctx.market_open_minutes = 47
        prompt = build_prompt(ctx)
        assert "47" in prompt

    def test_prompt_is_string_not_empty(self):
        ctx = make_context()
        prompt = build_prompt(ctx)
        assert isinstance(prompt, str)
        assert len(prompt) > 200   # Substantial prompt


# ─────────────────────────────────────────────────────────────────────────────
#  CALL_CLAUDE TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestCallClaude:

    def test_successful_api_call_returns_text(self):
        """A 200 response with a text block returns the text content."""
        with patch("requests.post") as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = MOCK_CLAUDE_RESPONSE
            mock_response.raise_for_status = MagicMock()
            mock_post.return_value = mock_response

            result = call_claude("test prompt")

        assert "WHAT HAPPENED" in result
        assert "LIKELY CAUSE" in result

    def test_correct_model_is_used(self):
        """Must always use claude-sonnet-4-20250514."""
        with patch("requests.post") as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = MOCK_CLAUDE_RESPONSE
            mock_response.raise_for_status = MagicMock()
            mock_post.return_value = mock_response

            call_claude("test")

        # Extract the json payload from the post call
        _, kwargs = mock_post.call_args
        actual_payload = kwargs.get("json", {})
        assert actual_payload["model"] == "claude-sonnet-4-20250514"

    def test_system_prompt_is_set(self):
        """The system prompt must be included — shapes Claude's response format."""
        with patch("requests.post") as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = MOCK_CLAUDE_RESPONSE
            mock_response.raise_for_status = MagicMock()
            mock_post.return_value = mock_response

            call_claude("test")

        actual_payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
        assert "system" in actual_payload
        assert len(actual_payload["system"]) > 20

    def test_http_error_raises(self):
        """4xx/5xx from the API should propagate as HTTPError."""
        import requests as req
        with patch("requests.post") as mock_post:
            mock_response = MagicMock()
            mock_response.raise_for_status.side_effect = req.HTTPError(
                response=MagicMock(status_code=401)
            )
            mock_post.return_value = mock_response

            with pytest.raises(req.HTTPError):
                call_claude("test")

    def test_multiple_text_blocks_joined(self):
        """If Claude returns multiple text blocks they should all be included."""
        multi_block_response = {
            "content": [
                {"type": "text", "text": "Part one."},
                {"type": "text", "text": "Part two."},
            ]
        }
        with patch("requests.post") as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = multi_block_response
            mock_response.raise_for_status = MagicMock()
            mock_post.return_value = mock_response

            result = call_claude("test")

        assert "Part one." in result
        assert "Part two." in result


# ─────────────────────────────────────────────────────────────────────────────
#  CIRCUIT BREAKER EXPLAINER TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestCircuitBreakerExplainer:

    def _mock_call_claude(self, prompt: str) -> str:
        return MOCK_CLAUDE_RESPONSE["content"][0]["text"]

    def test_explain_async_does_not_block(self):
        """explain_async() must return immediately — never wait for Claude."""
        explainer = CircuitBreakerExplainer()
        ctx = make_context()

        with patch("risk.circuit_breaker_explainer.call_claude",
                   side_effect=lambda p: time.sleep(5)):   # Simulate slow API
            start = time.monotonic()
            explainer.explain_async(ctx)
            elapsed = time.monotonic() - start

        # Should return in well under 1 second
        assert elapsed < 0.5, f"explain_async blocked for {elapsed:.2f}s"

    def test_postmortem_stored_after_completion(self):
        """last_postmortem should be populated once the background thread finishes."""
        explainer = CircuitBreakerExplainer()
        ctx = make_context()

        done = threading.Event()

        def fast_claude(prompt):
            return MOCK_CLAUDE_RESPONSE["content"][0]["text"]

        with patch("risk.circuit_breaker_explainer.call_claude", side_effect=fast_claude):
            explainer.explain_async(ctx)
            # Wait for background thread to finish (max 5s)
            for _ in range(50):
                if explainer.last_postmortem:
                    break
                time.sleep(0.1)

        assert explainer.last_postmortem is not None
        assert "WHAT HAPPENED" in explainer.last_postmortem

    def test_postmortem_written_to_file(self, tmp_path, monkeypatch):
        """Post-mortem should be written to a file in the logs/ directory."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "logs").mkdir()

        explainer = CircuitBreakerExplainer()
        ctx = make_context()

        with patch("risk.circuit_breaker_explainer.call_claude",
                   return_value=MOCK_CLAUDE_RESPONSE["content"][0]["text"]):
            explainer.explain_async(ctx)
            # Wait until last_postmortem is set OR 5s passes
            for _ in range(50):
                if explainer.last_postmortem:
                    time.sleep(0.1)   # Let file write complete after postmortem is set
                    break
                time.sleep(0.1)

        files = list((tmp_path / "logs").glob("halt_postmortem_*.txt"))
        assert len(files) == 1
        content = files[0].read_text()
        assert "QUANTCORE HALT POST-MORTEM" in content
        assert "WHAT HAPPENED" in content

    def test_api_error_does_not_crash_process(self):
        """An HTTP error from Claude must never propagate to the caller."""
        import requests as req
        explainer = CircuitBreakerExplainer()
        ctx = make_context()

        def raise_http(*args, **kwargs):
            raise req.HTTPError(response=MagicMock(status_code=429))

        with patch("risk.circuit_breaker_explainer.call_claude", side_effect=raise_http):
            explainer.explain_async(ctx)   # Must not raise
            time.sleep(0.3)

        # last_postmortem stays None — graceful degradation
        assert explainer.last_postmortem is None

    def test_timeout_does_not_crash_process(self):
        """A request timeout from Claude must be handled silently."""
        import requests as req
        explainer = CircuitBreakerExplainer()
        ctx = make_context()

        with patch("risk.circuit_breaker_explainer.call_claude",
                   side_effect=req.Timeout("timed out")):
            explainer.explain_async(ctx)
            time.sleep(0.3)

        assert explainer.last_postmortem is None


# ─────────────────────────────────────────────────────────────────────────────
#  RISK GATE INTEGRATION TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestRiskGateHaltCallback:

    def _make_order(self, qty=10):
        from portfolio.constructor import Order
        return Order("AAPL", "BUY", "LIMIT", qty, 1_890_000)

    def test_on_halt_fired_exactly_once_when_breaker_trips(self):
        """on_halt must fire exactly once — not on every subsequent fill."""
        from risk.gate import RiskGate
        from config.settings import Settings

        halt_calls = []
        cfg = Settings()
        cfg.symbols = ["AAPL"]
        cfg.risk.max_daily_loss_usd = 100.0
        cfg.risk.max_order_size = 1000
        cfg.risk.max_orders_per_second = 1000

        gate = RiskGate(
            on_approved=lambda o: None,
            cfg=cfg,
            on_halt=lambda: halt_calls.append(1),
        )
        gate.update_mid_price("AAPL", 189.0)

        # Three fills — first trips the breaker, next two should NOT re-fire
        gate.on_fill("AAPL", "BUY", 100, 189.0, realised_pnl_usd=-120.0)
        time.sleep(0.1)   # Let daemon thread start
        gate.on_fill("AAPL", "BUY", 10,  189.0, realised_pnl_usd=-10.0)
        gate.on_fill("AAPL", "BUY", 10,  189.0, realised_pnl_usd=-10.0)
        time.sleep(0.1)

        assert len(halt_calls) == 1, \
            f"on_halt fired {len(halt_calls)} times, expected exactly 1"

    def test_on_halt_not_fired_below_limit(self):
        """on_halt must NOT fire if daily loss is within limits."""
        from risk.gate import RiskGate
        from config.settings import Settings

        halt_calls = []
        cfg = Settings()
        cfg.symbols = ["AAPL"]
        cfg.risk.max_daily_loss_usd = 500.0
        cfg.risk.max_order_size = 1000
        cfg.risk.max_orders_per_second = 1000

        gate = RiskGate(
            on_approved=lambda o: None,
            cfg=cfg,
            on_halt=lambda: halt_calls.append(1),
        )
        gate.update_mid_price("AAPL", 189.0)

        # Loss well below limit
        gate.on_fill("AAPL", "BUY", 10, 189.0, realised_pnl_usd=-50.0)
        time.sleep(0.1)

        assert len(halt_calls) == 0

    def test_reset_daily_allows_halt_to_fire_again_next_day(self):
        """After reset_daily(), the next loss breach should fire on_halt again."""
        from risk.gate import RiskGate
        from config.settings import Settings

        halt_calls = []
        cfg = Settings()
        cfg.symbols = ["AAPL"]
        cfg.risk.max_daily_loss_usd = 100.0
        cfg.risk.max_order_size = 1000
        cfg.risk.max_orders_per_second = 1000

        gate = RiskGate(
            on_approved=lambda o: None,
            cfg=cfg,
            on_halt=lambda: halt_calls.append(1),
        )
        gate.update_mid_price("AAPL", 189.0)

        # Trip the breaker today
        gate.on_fill("AAPL", "BUY", 100, 189.0, realised_pnl_usd=-150.0)
        time.sleep(0.1)
        assert len(halt_calls) == 1

        # Next morning: reset
        gate.reset_daily()

        # Trip again tomorrow
        gate.on_fill("AAPL", "BUY", 100, 189.0, realised_pnl_usd=-150.0)
        time.sleep(0.1)
        assert len(halt_calls) == 2


# ─────────────────────────────────────────────────────────────────────────────
#  BUILD_HALT_CONTEXT TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildHaltContext:

    def _make_mock_tracker(self, symbols=None):
        """Build a mock PnLTracker with realistic position data."""
        from pnl.tracker import PositionState
        symbols = symbols or ["AAPL", "MSFT"]

        mock_tracker = MagicMock()

        def get_position(sym):
            if sym == "AAPL":
                pos = PositionState(symbol="AAPL")
                pos.net_quantity = 100
                pos.avg_cost_bps = 1_890_000
                pos.last_price_bps = 1_850_000
                pos.unrealised_pnl_bps = -400_000
                pos.realised_pnl_bps = -100_000
                return pos
            return None

        mock_tracker.get_position.side_effect = get_position
        return mock_tracker

    def test_context_has_correct_pnl(self):
        """daily_pnl_usd and limit_usd must match what's passed in."""
        tracker = self._make_mock_tracker()
        recent_fills = [
            {"symbol": "AAPL", "taker_side": "BUY",
             "quantity": 100, "price_dollars": 189.0,
             "timestamp_ms": int(time.time() * 1000)}
        ]

        ctx = build_halt_context(
            daily_pnl_usd=-1247.0,
            limit_usd=1000.0,
            pnl_tracker=tracker,
            recent_fills=recent_fills,
            market_open_minutes=30,
        )

        assert ctx.daily_pnl_usd == -1247.0
        assert ctx.limit_usd == 1000.0
        assert ctx.market_open_minutes == 30

    def test_context_fills_converted_correctly(self):
        """Fill dicts should be converted to FillRecord objects."""
        tracker = self._make_mock_tracker()
        now_ms = int(time.time() * 1000)
        fills = [
            {"symbol": "AAPL", "taker_side": "BUY",
             "quantity": 50, "price_dollars": 189.00,
             "timestamp_ms": now_ms}
        ]

        ctx = build_halt_context(
            daily_pnl_usd=-500.0,
            limit_usd=1000.0,
            pnl_tracker=tracker,
            recent_fills=fills,
        )

        assert len(ctx.recent_fills) == 1
        assert ctx.recent_fills[0].symbol == "AAPL"
        assert ctx.recent_fills[0].quantity == 50
        assert ctx.recent_fills[0].price_usd == 189.00

    def test_context_positions_bps_converted_to_dollars(self):
        """avg_cost_usd and last_price_usd must be in dollars, not bps."""
        tracker = self._make_mock_tracker()

        ctx = build_halt_context(
            daily_pnl_usd=-500.0,
            limit_usd=1000.0,
            pnl_tracker=tracker,
            recent_fills=[],
        )

        aapl = next((p for p in ctx.positions if p.symbol == "AAPL"), None)
        assert aapl is not None
        # 1,890,000 bps / 10,000 = $189.00
        assert abs(aapl.avg_cost_usd - 189.0) < 0.01
        # 1,850,000 bps / 10,000 = $185.00
        assert abs(aapl.last_price_usd - 185.0) < 0.01
