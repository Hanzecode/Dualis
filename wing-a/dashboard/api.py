"""
dashboard/api.py — FastAPI endpoints for the Wing A React dashboard.

Add these routes to your existing FastAPI app instance.
The dashboard polls GET /api/snapshot every 2 seconds as the primary feed.
All other endpoints are available for targeted fetches or future drill-downs.

Dependencies:
    pip install fastapi uvicorn psycopg2-binary

Run:
    uvicorn dashboard.api:app --reload --port 8000
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Literal

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config.settings import settings

app = FastAPI(title="QuantCore Wing A API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # React dev server
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ─── Pydantic response models ─────────────────────────────────────────────────

class OrderBookLevel(BaseModel):
    price: float
    size: int
    total: float

class OrderBook(BaseModel):
    ticker: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    spread: float
    mid: float
    timestamp: str

class RiskMetric(BaseModel):
    label: str
    current: float
    limit: float
    unit: str
    pct: float
    status: Literal["ok", "warn", "breach"]

class RiskState(BaseModel):
    metrics: list[RiskMetric]
    overall: Literal["PASS", "WARN", "BREACH"]

class PnLPoint(BaseModel):
    time: str
    value: float

class PnLState(BaseModel):
    session: float
    unrealised: float
    realised: float
    cash: float
    equity: float
    history: list[PnLPoint]

class Fill(BaseModel):
    id: str
    side: Literal["buy", "sell"]
    ticker: str
    qty: int
    price: float
    timestamp: str
    source: str

class Signal(BaseModel):
    ticker: str
    value: float
    direction: Literal[1, -1]
    confidence: float
    strategy: str
    received_at: str

class ZmqState(BaseModel):
    signals: list[Signal]
    latency_ms: float
    socket_status: Literal["connected", "reconnecting", "disconnected"]
    messages_recv: int

class CircuitBreaker(BaseModel):
    name: str
    state: Literal["open", "half", "tripped"]
    reason: str
    triggered_at: str | None = None

class GatewayState(BaseModel):
    connected: bool
    stream_active: bool
    symbols_subscribed: int
    last_tick_ms: int
    fills_today: int
    book_router_status: str
    ws_reconnects: int

class DashboardSnapshot(BaseModel):
    order_book: OrderBook
    risk: RiskState
    pnl: PnLState
    fills: list[Fill]
    zmq: ZmqState
    circuit_breakers: list[CircuitBreaker]
    gateway: GatewayState
    engine_status: Literal["running", "paused", "error"]
    mode: Literal["PAPER", "LIVE"]
    open_orders: int
    # No beta model exists anywhere in the codebase — null is the honest
    # answer until one does, rather than a fixed number that looks computed.
    beta: float | None


# ─── Live state hooks (called by main.py's WingA orchestrator) ───────────────
# main.py pushes every fill/quote/signal/position update through these as it
# happens, and the GET accessors below read straight out of this module-level
# state — that's what makes /api/snapshot reflect the live engine instead of
# canned numbers.

_event_loop = None
_latest_fills: list[dict] = []
_latest_positions: dict[str, dict] = {}
_latest_postmortem: str | None = None
_latest_halt_status: dict | None = None
_latest_quotes: dict[str, dict] = {}     # symbol -> {bid, ask, ts}
_latest_signals: list[dict] = []         # last N AlphaSignals, most recent last
_pnl_history: list[dict] = []            # [{time, value}] rolling session PnL
_messages_recv: int = 0

# References to live components — set once at startup via register_components()
_risk_gate = None
_pnl_tracker = None
_alpaca_feed = None
_signal_ingestor = None
_order_manager = None


def set_event_loop(loop) -> None:
    global _event_loop
    _event_loop = loop


def register_components(
    risk_gate=None,
    pnl_tracker=None,
    alpaca_feed=None,
    signal_ingestor=None,
    order_manager=None,
) -> None:
    """Called once from WingA.__init__ so GET endpoints can query live state."""
    global _risk_gate, _pnl_tracker, _alpaca_feed, _signal_ingestor, _order_manager
    _risk_gate = risk_gate
    _pnl_tracker = pnl_tracker
    _alpaca_feed = alpaca_feed
    _signal_ingestor = signal_ingestor
    _order_manager = order_manager


def update_fill(fill: dict) -> None:
    _latest_fills.append(fill)
    del _latest_fills[:-50]


def update_position(
    symbol: str,
    net_quantity: int,
    avg_cost_bps: int,
    realised_pnl_usd: float,
    unrealised_pnl_usd: float,
) -> None:
    _latest_positions[symbol] = {
        "net_quantity": net_quantity,
        "avg_cost_bps": avg_cost_bps,
        "realised_pnl_usd": realised_pnl_usd,
        "unrealised_pnl_usd": unrealised_pnl_usd,
    }
    total = sum(
        p["realised_pnl_usd"] + p["unrealised_pnl_usd"]
        for p in _latest_positions.values()
    )
    _pnl_history.append({
        "time": datetime.utcnow().strftime("%H:%M:%S"),
        "value": round(total, 2),
    })
    del _pnl_history[:-200]


def update_postmortem(text: str) -> None:
    global _latest_postmortem
    _latest_postmortem = text


def update_halt_status(halted: bool, daily_pnl_usd: float, limit_usd: float) -> None:
    global _latest_halt_status
    _latest_halt_status = {
        "halted": halted,
        "daily_pnl_usd": daily_pnl_usd,
        "limit_usd": limit_usd,
    }


def update_quote(symbol: str, bid_usd: float, ask_usd: float) -> None:
    global _messages_recv
    _latest_quotes[symbol] = {"bid": bid_usd, "ask": ask_usd, "ts": time.time()}
    _messages_recv += 1


def update_signal(signal) -> None:
    """signal is an AlphaSignal from signal_ingestion/ingestor.py."""
    _latest_signals.append({
        "ticker": signal.symbol,
        "value": signal.signal,
        "direction": 1 if signal.is_buy else -1,
        "confidence": signal.confidence,
        "strategy": "wing_b",
        "received_at": datetime.utcnow().isoformat(),
    })
    del _latest_signals[:-20]


# ─── Data accessors ────────────────────────────────────────────────────────
# Read from the live state pushed in by main.py's WingA orchestrator (see the
# hooks above) and from the component references set by register_components().
#
# Known gap: full L2 order-book depth lives only in the C++ engine's
# FlatOrderBook and isn't exposed to this Python process over any IPC path
# today — get_order_book() below can only show top-of-book (bid/ask/mid) from
# the same Alpaca quotes the engine itself consumes, not real depth/size.

def get_order_book(ticker: str = "AAPL") -> OrderBook:
    q = _latest_quotes.get(ticker)
    if q is None:
        return OrderBook(
            ticker=ticker, bids=[], asks=[], spread=0.0, mid=0.0,
            timestamp=datetime.utcnow().isoformat(),
        )
    bid, ask = q["bid"], q["ask"]
    mid = round((bid + ask) / 2.0, 2)
    spread = round(ask - bid, 2)
    return OrderBook(
        ticker=ticker,
        spread=spread,
        mid=mid,
        timestamp=datetime.utcfromtimestamp(q["ts"]).isoformat(),
        # size/total unknown — Alpaca IEX quotes only give top-of-book, not depth
        bids=[OrderBookLevel(price=round(bid, 2), size=0, total=0.0)],
        asks=[OrderBookLevel(price=round(ask, 2), size=0, total=0.0)],
    )

def get_risk() -> RiskState:
    if _risk_gate is None:
        return RiskState(overall="PASS", metrics=[])

    daily_pnl = _risk_gate.daily_pnl_usd
    max_loss = settings.risk.max_daily_loss_usd
    notional = _risk_gate.total_notional_usd()
    max_notional = settings.risk.max_total_notional_usd
    max_pos_qty = max((abs(p["net_quantity"]) for p in _latest_positions.values()), default=0)
    max_pos_limit = settings.risk.max_position_per_symbol

    def pct_of(current: float, limit: float) -> float:
        return round(min(100.0, (current / limit) * 100), 1) if limit else 0.0

    def status(pct: float) -> Literal["ok", "warn", "breach"]:
        if pct >= 100:
            return "breach"
        if pct >= 70:
            return "warn"
        return "ok"

    drawdown_pct = pct_of(abs(daily_pnl), max_loss)
    notional_pct = pct_of(notional, max_notional)
    position_pct = pct_of(max_pos_qty, max_pos_limit)

    metrics = [
        RiskMetric(label="Daily drawdown", current=round(daily_pnl, 2), limit=max_loss,
                   unit="$", pct=drawdown_pct, status=status(drawdown_pct)),
        RiskMetric(label="Total notional", current=round(notional, 2), limit=max_notional,
                   unit="$", pct=notional_pct, status=status(notional_pct)),
        RiskMetric(label="Max position", current=max_pos_qty, limit=max_pos_limit,
                   unit="sh", pct=position_pct, status=status(position_pct)),
    ]
    overall: Literal["PASS", "WARN", "BREACH"] = (
        "BREACH" if _risk_gate.is_halted
        else "WARN" if any(m.status == "warn" for m in metrics)
        else "PASS"
    )
    return RiskState(metrics=metrics, overall=overall)

def get_pnl() -> PnLState:
    realised = sum(p["realised_pnl_usd"] for p in _latest_positions.values())
    unrealised = sum(p["unrealised_pnl_usd"] for p in _latest_positions.values())
    history = [PnLPoint(time=h["time"], value=h["value"]) for h in _pnl_history[-50:]]

    # Cash/equity live on PnLTracker itself (registered via register_components).
    # Fall back to the configured starting cash if the tracker isn't wired up
    # yet (e.g. dashboard booted before WingA finished initialising).
    if _pnl_tracker is not None:
        cash = _pnl_tracker.cash_usd
        equity = _pnl_tracker.equity_usd()
    else:
        cash = settings.starting_cash_usd
        equity = settings.starting_cash_usd

    return PnLState(
        session=round(realised + unrealised, 2),
        unrealised=round(unrealised, 2),
        realised=round(realised, 2),
        cash=round(cash, 2),
        equity=round(equity, 2),
        history=history,
    )

def get_fills(limit: int = 20) -> list[Fill]:
    out = []
    for f in reversed(_latest_fills[-limit:]):
        ts_ms = f.get("timestamp_ms", 0)
        # our_side = side of OUR position change (taker_side is the synthetic
        # market order crossing our resting limit — the opposite of ours).
        raw_side = f.get("our_side", "BUY")
        out.append(Fill(
            id=str(f.get("trade_id", "")),
            side="buy" if raw_side == "BUY" else "sell",
            ticker=f.get("symbol", ""),
            qty=int(f.get("quantity", 0)),
            price=round(f.get("price_bps", 0) / 10_000.0, 2),
            timestamp=datetime.utcfromtimestamp(ts_ms / 1000).strftime("%H:%M:%S") if ts_ms else "",
            source="alpaca_feed",
        ))
    return out

def get_zmq() -> ZmqState:
    signals = [Signal(**s) for s in reversed(_latest_signals[-5:])]
    return ZmqState(
        signals=signals,
        latency_ms=0.0,   # not instrumented — no send/recv timestamps to diff
        socket_status="connected" if _latest_quotes else "disconnected",
        messages_recv=_messages_recv,
    )

def get_circuit_breakers() -> list[CircuitBreaker]:
    # Only one circuit breaker actually exists in the code — RiskGate's daily
    # loss halt (risk/gate.py). Not fabricating the others.
    if _risk_gate is None:
        return []
    halted = _risk_gate.is_halted
    return [
        CircuitBreaker(
            name="Daily loss circuit breaker",
            state="tripped" if halted else "open",
            reason=f"${_risk_gate.daily_pnl_usd:,.2f} / -${settings.risk.max_daily_loss_usd:,.2f}",
            triggered_at=datetime.utcnow().isoformat() if halted else None,
        ),
    ]

def get_gateway() -> GatewayState:
    now_ms = int(time.time() * 1000)
    last_tick_ms = max((int(q["ts"] * 1000) for q in _latest_quotes.values()), default=0)
    stream_active = bool(_latest_quotes) and (now_ms - last_tick_ms) < 30_000
    return GatewayState(
        connected=bool(_latest_quotes),
        stream_active=stream_active,
        symbols_subscribed=len(_latest_quotes),
        last_tick_ms=(now_ms - last_tick_ms) if last_tick_ms else 0,
        fills_today=len(_latest_fills),
        book_router_status="live" if stream_active else "no data",
        ws_reconnects=0,   # not tracked by AlpacaFeed today
    )


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/api/snapshot", response_model=DashboardSnapshot)
def snapshot():
    """Primary polling endpoint — returns full dashboard state in one request."""
    return DashboardSnapshot(
        order_book=get_order_book(),
        risk=get_risk(),
        pnl=get_pnl(),
        fills=get_fills(),
        zmq=get_zmq(),
        circuit_breakers=get_circuit_breakers(),
        gateway=get_gateway(),
        engine_status="paused" if (_risk_gate and _risk_gate.is_halted) else "running",
        mode="PAPER",
        open_orders=_order_manager.open_order_count if _order_manager else 0,
        beta=None,   # no beta model exists — see DashboardSnapshot.beta comment above
    )

@app.get("/api/orderbook", response_model=OrderBook)
def orderbook(ticker: str = Query(default="AAPL", description="Ticker symbol")):
    return get_order_book(ticker)

@app.get("/api/risk", response_model=RiskState)
def risk():
    return get_risk()

@app.get("/api/pnl", response_model=PnLState)
def pnl():
    return get_pnl()

@app.get("/api/fills", response_model=list[Fill])
def fills(limit: int = Query(default=20, ge=1, le=200)):
    return get_fills(limit)

@app.get("/api/zmq", response_model=ZmqState)
def zmq():
    return get_zmq()

@app.get("/api/circuit_breakers", response_model=list[CircuitBreaker])
def circuit_breakers():
    return get_circuit_breakers()

@app.get("/api/gateway", response_model=GatewayState)
def gateway():
    return get_gateway()

@app.get("/health")
def health():
    return {"status": "ok", "ts": datetime.utcnow().isoformat()}
