# wing_a/risk/circuit_breaker_explainer.py
# ─────────────────────────────────────────────────────────────────────────────
#  CIRCUIT BREAKER EXPLAINER
#  Called exactly once when RiskGate trips the circuit breaker (daily loss
#  exceeds the limit). Gathers context from PnLTracker and recent fills,
#  then calls the Claude API to produce a structured post-mortem you can
#  read before deciding whether to re-enable trading tomorrow.
#
#  Where it plugs in:
#    main.py → WingA._on_pnl_update()
#      └─► RiskGate.on_fill() sets _halted = True
#            └─► WingA._on_halt() ← NEW callback
#                  └─► CircuitBreakerExplainer.explain()
#                        └─► Claude API
#                              └─► post-mortem written to log + dashboard
#
#  Cost per call: ~$0.003–0.015 (Sonnet 4).
#  Latency:  500ms–2s.  This is FINE — the halt has already happened.
#            The explainer runs asynchronously in a background thread so
#            it never delays the halt itself.
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests    # pip install requests  (already in requirements.txt via uvicorn)

from config.settings import Settings, settings as default_settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  DATA CLASSES — snapshot of system state at halt time
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PositionSnapshot:
    """One symbol's position at the moment the circuit breaker fired."""
    symbol:             str
    net_quantity:       int       # positive = long, negative = short
    avg_cost_usd:       float     # VWAP cost basis
    last_price_usd:     float     # Mid-price at halt time (0 if unknown)
    unrealised_pnl_usd: float
    realised_pnl_usd:   float

    @property
    def side(self) -> str:
        return "LONG" if self.net_quantity > 0 else ("SHORT" if self.net_quantity < 0 else "FLAT")

    @property
    def notional_usd(self) -> float:
        return abs(self.net_quantity) * self.last_price_usd


@dataclass
class FillRecord:
    """One fill that contributed to the daily PnL."""
    symbol:        str
    side:          str    # "BUY" or "SELL"
    quantity:      int
    price_usd:     float
    realised_pnl:  float  # 0 if no position was closed
    timestamp_ms:  int


@dataclass
class HaltContext:
    """Everything we know at the moment the circuit breaker fires."""
    daily_pnl_usd:       float              # Total PnL today (negative, exceeded limit)
    limit_usd:           float              # The configured limit that was breached
    positions:           List[PositionSnapshot]
    recent_fills:        List[FillRecord]   # Last N fills before halt
    halt_time:           datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    market_open_minutes: int = 0            # How many minutes into the session we halted


# ─────────────────────────────────────────────────────────────────────────────
#  PROMPT BUILDER
#  Constructs the prompt from live system state.
#  Kept separate from the API call so you can unit-test it without calling Claude.
# ─────────────────────────────────────────────────────────────────────────────

def build_prompt(ctx: HaltContext) -> str:
    """
    Build a structured prompt from the halt context.
    The prompt follows the principle of giving Claude precise numbers
    and asking for structured output — not open-ended questions.
    """
    # Format positions table
    pos_lines = []
    for p in sorted(ctx.positions, key=lambda x: abs(x.unrealised_pnl_usd), reverse=True):
        if p.net_quantity == 0:
            continue   # Skip flat positions
        pos_lines.append(
            f"  {p.symbol:6s} {p.side:5s} {abs(p.net_quantity):>5d} shares  "
            f"avg_cost=${p.avg_cost_usd:.2f}  "
            f"last=${p.last_price_usd:.2f}  "
            f"unrealised=${p.unrealised_pnl_usd:+.2f}  "
            f"realised=${p.realised_pnl_usd:+.2f}"
        )
    positions_str = "\n".join(pos_lines) if pos_lines else "  (no open positions)"

    # Format recent fills — last 10 most recent
    fill_lines = []
    for f in ctx.recent_fills[-10:]:
        ts = datetime.fromtimestamp(f.timestamp_ms / 1000, tz=timezone.utc).strftime("%H:%M:%S")
        pnl_str = f"  realised=${f.realised_pnl:+.2f}" if f.realised_pnl != 0 else ""
        fill_lines.append(
            f"  {ts}  {f.side:4s} {f.quantity:>4d} {f.symbol:6s} @ ${f.price_usd:.2f}{pnl_str}"
        )
    fills_str = "\n".join(fill_lines) if fill_lines else "  (no fills recorded)"

    # Breakdown: what portion is unrealised vs realised
    total_unrealised = sum(p.unrealised_pnl_usd for p in ctx.positions)
    total_realised   = ctx.daily_pnl_usd - total_unrealised

    prompt = f"""You are a quantitative trading risk analyst reviewing an automated circuit breaker halt.

HALT EVENT
----------
Time:           {ctx.halt_time.strftime("%Y-%m-%d %H:%M:%S UTC")}
Session time:   {ctx.market_open_minutes} minutes into trading session
Daily PnL:      ${ctx.daily_pnl_usd:+.2f}
Daily limit:    -${ctx.limit_usd:.2f}
Limit breached: ${abs(ctx.daily_pnl_usd) - ctx.limit_usd:.2f} over limit

PNL BREAKDOWN
-------------
Unrealised (open positions): ${total_unrealised:+.2f}
Realised (closed trades):    ${total_realised:+.2f}

OPEN POSITIONS AT HALT
----------------------
{positions_str}

LAST {len(ctx.recent_fills)} FILLS BEFORE HALT
{'-' * 40}
{fills_str}

TASK
----
Write a concise post-mortem with exactly these four sections:

1. WHAT HAPPENED (2-3 sentences)
   Describe the sequence of events that led to the halt. Was this driven by
   unrealised mark-to-market losses, realised losses from closed positions, or both?
   Which symbol was the largest contributor?

2. LIKELY CAUSE (2-3 sentences)
   Based on the pattern of fills and positions, what most likely caused this loss?
   Was it a directional position that went against us, high turnover with consistent
   slippage, or a sharp intraday move? Be specific about which positions.

3. WHAT TO CHECK BEFORE RE-ENABLING (4 bullet points)
   Concrete, specific checks a quant would do tomorrow morning before lifting the halt.
   Reference the actual symbols and numbers above.

4. RISK LIMIT REVIEW (1-2 sentences)
   Given this halt, should the daily loss limit, position limits, or order sizing
   be adjusted? Give a specific recommendation with a number.

Be direct and analytical. Do not hedge with "it could be" — state your assessment clearly."""

    return prompt


# ─────────────────────────────────────────────────────────────────────────────
#  CLAUDE API CALLER
# ─────────────────────────────────────────────────────────────────────────────

CLAUDE_API_URL  = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL    = "claude-sonnet-4-20250514"
CLAUDE_MAX_TOK  = 1024   # Post-mortem fits in ~600 tokens; 1024 gives headroom


def call_claude(prompt: str) -> str:
    """
    Call the Claude API and return the text response.
    Raises on HTTP error — caller handles retry / fallback.
    """
    headers = {
        "Content-Type":         "application/json",
        "anthropic-version":    "2023-06-01",
        # API key injected from environment — never hardcoded.
        # Set: export ANTHROPIC_API_KEY=sk-ant-...
        # In production: load from AWS Secrets Manager at startup.
    }

    payload = {
        "model":      CLAUDE_MODEL,
        "max_tokens": CLAUDE_MAX_TOK,
        "system": (
            "You are a quantitative trading risk analyst. "
            "You write concise, precise post-mortems for systematic trading halts. "
            "You never speculate beyond the data given. "
            "You format output exactly as instructed."
        ),
        "messages": [
            {"role": "user", "content": prompt}
        ],
    }

    # Timeout: 30s — Claude API p99 latency is well under this.
    # We do NOT retry here — the explainer is best-effort.
    # If the API is down, we log and move on.
    response = requests.post(
        CLAUDE_API_URL,
        headers=headers,
        json=payload,
        timeout=30,
    )
    response.raise_for_status()   # Raises HTTPError on 4xx/5xx

    data = response.json()

    # Extract text from the response content blocks
    # content is a list of blocks; we want all text blocks joined
    text_blocks = [
        block["text"]
        for block in data.get("content", [])
        if block.get("type") == "text"
    ]
    return "\n".join(text_blocks)


# ─────────────────────────────────────────────────────────────────────────────
#  CIRCUIT BREAKER EXPLAINER
# ─────────────────────────────────────────────────────────────────────────────

class CircuitBreakerExplainer:
    """
    Asynchronous post-mortem generator.
    Called from WingA._on_halt() — runs the Claude API call in a background
    daemon thread so the halt itself is never delayed.

    The post-mortem is:
      1. Written to the log file (always)
      2. Stored in self.last_postmortem (for dashboard API to serve)
      3. Optionally pushed to a Slack webhook (if configured)
    """

    def __init__(self, cfg: Settings = default_settings):
        self._cfg = cfg
        self.last_postmortem: Optional[str] = None    # Latest post-mortem text
        self.last_halt_context: Optional[HaltContext] = None

    def explain_async(
        self,
        ctx: HaltContext,
        on_complete: Optional[callable] = None,  # Called with (postmortem: str) when done
    ) -> None:
        """
        Fire-and-forget: spawns a daemon thread to call Claude.
        Returns immediately — the halt path is not blocked.

        on_complete(text): optional callback fired from the worker thread
        when the post-mortem is ready. Use this to push the result to the
        dashboard or trigger further notifications.
        """
        self.last_halt_context = ctx
        t = threading.Thread(
            target=self._explain_worker,
            args=(ctx, on_complete),
            name="cb-explainer",
            daemon=True,
        )
        t.start()
        logger.info(
            "CircuitBreakerExplainer: post-mortem generation started "
            "(async, daily_pnl=$%.2f)", ctx.daily_pnl_usd
        )

    def _explain_worker(self, ctx: HaltContext, on_complete: Optional[callable] = None) -> None:
        """Background thread body."""
        start = time.monotonic()
        try:
            prompt     = build_prompt(ctx)
            postmortem = call_claude(prompt)
            elapsed    = time.monotonic() - start

            self.last_postmortem = postmortem

            # ── Push to dashboard WebSocket (browser updates in real time) ──
            try:
                from dashboard import api as dashboard_api
                dashboard_api.update_postmortem(postmortem)
            except Exception:
                pass   # Dashboard may not be running in test environments

            # ── Fire on_complete callback (e.g. main.py logs it to Slack) ──
            if on_complete:
                try:
                    on_complete(postmortem)
                except Exception as e:
                    logger.warning("on_complete callback error: %s", e)

            # ── Log it ───────────────────────────────────────────────────────
            sep = "═" * 60
            logger.critical(
                "\n%s\n  CIRCUIT BREAKER POST-MORTEM  (%.1fs)\n%s\n%s\n%s",
                sep, elapsed, sep, postmortem, sep
            )

            # ── Write to file (survives process restart) ─────────────────────
            ts_str = ctx.halt_time.strftime("%Y%m%d_%H%M%S")
            path = f"logs/halt_postmortem_{ts_str}.txt"
            os.makedirs("logs", exist_ok=True)
            with open(path, "w") as f:
                f.write("QUANTCORE HALT POST-MORTEM\n")
                f.write(f"Generated: {ctx.halt_time.isoformat()}\n")
                f.write(f"Daily PnL: ${ctx.daily_pnl_usd:+.2f}\n\n")
                f.write(postmortem)
            logger.info("Post-mortem written to %s", path)

            # ── Optional: Slack webhook ───────────────────────────────────────
            slack_url = getattr(self._cfg, "slack_webhook_url", None)
            if slack_url:
                self._post_to_slack(slack_url, postmortem, ctx)

        except requests.HTTPError as e:
            logger.error(
                "CircuitBreakerExplainer: Claude API HTTP error %s — "
                "post-mortem not generated. Check ANTHROPIC_API_KEY.",
                e.response.status_code
            )
        except requests.Timeout:
            logger.error(
                "CircuitBreakerExplainer: Claude API timed out after 30s — "
                "post-mortem not generated."
            )
        except Exception as e:
            # Never let the explainer crash the process
            logger.error(
                "CircuitBreakerExplainer: unexpected error — %s", e, exc_info=True
            )

    def _post_to_slack(self, webhook_url: str, postmortem: str, ctx: HaltContext) -> None:
        """Post the post-mortem to a Slack channel via incoming webhook."""
        # Slack message: brief header + collapsible full text
        message = {
            "text": f":rotating_light: *QuantCore circuit breaker fired* — "
                    f"Daily PnL ${ctx.daily_pnl_usd:+.2f} vs limit "
                    f"-${ctx.limit_usd:.2f}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f":rotating_light: *Circuit breaker fired* at "
                            f"{ctx.halt_time.strftime('%H:%M UTC')}\n"
                            f"Daily PnL: *${ctx.daily_pnl_usd:+.2f}* "
                            f"(limit: -${ctx.limit_usd:.2f})"
                        )
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        # Slack has 3000-char limit per block; truncate safely
                        "text": f"```{postmortem[:2800]}```"
                    }
                }
            ]
        }
        try:
            r = requests.post(webhook_url, json=message, timeout=10)
            r.raise_for_status()
            logger.info("Post-mortem posted to Slack")
        except Exception as e:
            logger.warning("Slack post failed: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
#  CONTEXT BUILDER
#  Pulls live data from PnLTracker and RiskGate into a HaltContext.
#  Called by WingA._on_halt() right after the halt is confirmed.
# ─────────────────────────────────────────────────────────────────────────────

def build_halt_context(
    daily_pnl_usd: float,
    limit_usd: float,
    pnl_tracker,          # pnl.tracker.PnLTracker instance
    recent_fills: list,   # Last N fill dicts from FillSubscriber
    market_open_minutes: int = 0,
) -> HaltContext:
    """
    Assemble a HaltContext from live Wing A components.
    Call this immediately when the halt is detected so data is fresh.
    """
    from config.settings import settings

    positions = []
    for sym in settings.symbols:
        pos = pnl_tracker.get_position(sym)
        if pos is None:
            continue
        # Convert bps to dollars
        avg_cost_usd = pos.avg_cost_bps / 10_000.0
        last_price_usd = pos.last_price_bps / 10_000.0 if pos.last_price_bps else 0.0

        positions.append(PositionSnapshot(
            symbol=sym,
            net_quantity=pos.net_quantity,
            avg_cost_usd=avg_cost_usd,
            last_price_usd=last_price_usd,
            unrealised_pnl_usd=pos.unrealised_pnl_bps / 10_000.0,
            realised_pnl_usd=pos.realised_pnl_bps / 10_000.0,
        ))

    fill_records = []
    for f in recent_fills:
        fill_records.append(FillRecord(
            symbol=f.get("symbol", ""),
            side=f.get("taker_side", "BUY"),      # taker_side now in fill JSON (bug fix)
            quantity=int(f.get("quantity", 0)),
            price_usd=float(f.get("price_dollars", 0)),
            realised_pnl=0.0,                      # Available from PnLTracker if needed
            timestamp_ms=int(f.get("timestamp_ms", 0)),
        ))

    return HaltContext(
        daily_pnl_usd=daily_pnl_usd,
        limit_usd=limit_usd,
        positions=positions,
        recent_fills=fill_records,
        market_open_minutes=market_open_minutes,
    )
