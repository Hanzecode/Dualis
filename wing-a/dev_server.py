# wing_a/dev_server.py
# ─────────────────────────────────────────────────────────────────────────────
#  WING A DEVELOPMENT SERVER
#  Replaces every external dependency so Wing A runs completely standalone.
#
#  What it mocks:
#    1. ZMQ PULL socket  (port 5557) — acts as the C++ engine
#       Receives orders from order_manager.py, prints them, sends fake fills back
#
#    2. ZMQ PUB socket   (port 5556) — publishes fake fills to fill_subscriber.py
#
#    3. S3 signal files              — writes fake AlphaSignal JSON locally
#       Wing A's ingestor polls these via a patched boto3 client
#
#    4. Alpaca WebSocket             — sends fake quote ticks to alpaca_feed.py
#
#    5. PostgreSQL                   — in-memory dict, no real DB connection
#
#  Run:
#    Terminal 1:  cd wing_a && python dev_server.py
#    Terminal 2:  cd wing_a && python main.py --dev
#
#  Or run both together:
#    cd wing_a && python dev_server.py --with-wing-a
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import zmq

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("dev_server")


# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG — matches settings.py defaults
# ─────────────────────────────────────────────────────────────────────────────

PULL_PORT   = 5557   # C++ engine PULL — we bind this, Wing A PUSH connects
PUB_PORT    = 5556   # C++ engine PUB  — we bind this, Wing A SUB connects
SYMBOLS     = ["AAPL", "MSFT", "TSLA", "NVDA"]

# Fake mid-prices in basis points (price × 10,000)
BASE_PRICES = {
    "AAPL": 1_890_000,
    "MSFT": 4_150_000,
    "TSLA": 2_500_000,
    "NVDA": 8_750_000,
}

SIGNAL_DIR  = Path("/tmp/quantcore_dev_signals")
SIGNAL_DIR.mkdir(exist_ok=True)

trade_id_counter = 1000


# ─────────────────────────────────────────────────────────────────────────────
#  1. ZMQ MOCK ENGINE
#     Binds PULL on 5557 — receives orders from Wing A's order_manager.py
#     Binds PUB  on 5556 — publishes fake fills back to Wing A's fill_subscriber
# ─────────────────────────────────────────────────────────────────────────────

class MockCppEngine:
    """Pretends to be the C++ matching engine."""

    def __init__(self):
        self._ctx  = zmq.Context()
        self._pull = self._ctx.socket(zmq.PULL)
        self._pub  = self._ctx.socket(zmq.PUB)
        self._pull.bind(f"tcp://*:{PULL_PORT}")
        self._pub.bind(f"tcp://*:{PUB_PORT}")
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()
        logger.info("MockCppEngine: PULL bound on :%d  PUB bound on :%d",
                    PULL_PORT, PUB_PORT)

    def stop(self):
        self._stop.set()
        self._pull.setsockopt(zmq.LINGER, 0)
        self._pub.setsockopt(zmq.LINGER, 0)
        self._pull.close()
        self._pub.close()

    def _loop(self):
        self._pull.setsockopt(zmq.RCVTIMEO, 500)   # 500ms timeout — check stop flag
        while not self._stop.is_set():
            try:
                raw = self._pull.recv_string()
            except zmq.Again:
                continue
            self._handle(raw)

    def _handle(self, raw: str):
        global trade_id_counter
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type", "")

        if msg_type == "ORDER":
            symbol   = msg.get("symbol", "AAPL")
            side     = msg.get("side", "BUY")
            qty      = int(msg.get("quantity", 0))
            price    = int(msg.get("price_bps", BASE_PRICES.get(symbol, 1_000_000)))
            order_type = msg.get("order_type", "LIMIT")

            logger.info("  ← ORDER  %s %s %d @ $%.2f (%s)",
                        side, symbol, qty, price / 10_000, order_type)

            # Simulate immediate fill (market maker always fills in dev mode)
            time.sleep(random.uniform(0.01, 0.05))   # 10–50ms fake latency
            trade_id_counter += 1

            fill = {
                "trade_id":       trade_id_counter,
                "symbol":         symbol,
                "price_bps":      price,
                "price_dollars":  price / 10_000.0,
                "quantity":       qty,
                "taker_side":     side,
                "maker_order_id": trade_id_counter * 10,
                "taker_order_id": trade_id_counter * 10 + 1,
                "timestamp_ms":   int(time.time() * 1000),
            }

            # Publish fill — topic prefix "fills " matches fill_subscriber.py
            payload = "fills " + json.dumps(fill)
            self._pub.send_string(payload)
            logger.info("  → FILL   %s %s %d @ $%.2f  trade_id=%d",
                        side, symbol, qty, price / 10_000, trade_id_counter)

        elif msg_type == "CANCEL":
            logger.info("  ← CANCEL %s order_id=%s",
                        msg.get("symbol"), msg.get("order_id"))

        elif msg_type == "MARKET_DEPTH":
            # Silently accept depth updates — log only occasionally
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  2. FAKE SIGNAL WRITER
#     Writes JSON signal files to /tmp/quantcore_dev_signals/
#     Wing A's ingestor.py is patched to read from there instead of real S3
# ─────────────────────────────────────────────────────────────────────────────

class FakeSignalWriter:
    """Writes new AlphaSignal JSON files every N seconds."""

    def __init__(self, interval_s: float = 15.0):
        self._interval = interval_s
        self._stop     = threading.Event()
        self._thread   = threading.Thread(target=self._loop, daemon=True)
        self._counter  = 0

    def start(self):
        # Write one immediately so Wing A has something to read right away
        self._write_signals()
        self._thread.start()
        logger.info("FakeSignalWriter: writing signals to %s every %.0fs",
                    SIGNAL_DIR, self._interval)

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            self._stop.wait(timeout=self._interval)
            if not self._stop.is_set():
                self._write_signals()

    def _write_signals(self):
        self._counter += 1
        for sym in SYMBOLS:
            # Alternate between buy and sell signals each cycle
            signal_strength = random.uniform(0.15, 0.85)
            if (self._counter + SYMBOLS.index(sym)) % 2 == 0:
                signal_strength = -signal_strength   # sell

            base = BASE_PRICES[sym]
            # Add small random price drift
            drift = random.randint(-5000, 5000)

            data = {
                "symbol":          sym,
                "signal":          round(signal_strength, 3),
                "confidence":      round(random.uniform(0.55, 0.95), 3),
                "target_qty":      random.randint(50, 300),
                "limit_price_bps": base + drift,
                "generated_at":    datetime.now(timezone.utc).isoformat(),
            }

            path = SIGNAL_DIR / f"{sym}.json"
            path.write_text(json.dumps(data))

        logger.info("FakeSignalWriter: wrote signals for %s (cycle %d)",
                    ", ".join(SYMBOLS), self._counter)


# ─────────────────────────────────────────────────────────────────────────────
#  3. FAKE ALPACA WEBSOCKET SERVER
#     Wing A's alpaca_feed.py is patched to call our on_quote callback
#     directly instead of connecting to a real WebSocket
# ─────────────────────────────────────────────────────────────────────────────

class FakeAlpacaTicker:
    """Calls a quote callback with fake price ticks every N seconds."""

    def __init__(self, on_quote, interval_s: float = 2.0):
        self._on_quote  = on_quote
        self._interval  = interval_s
        self._stop      = threading.Event()
        self._thread    = threading.Thread(target=self._loop, daemon=True)
        # Track current prices to add realistic drift
        self._prices    = dict(BASE_PRICES)

    def start(self):
        self._thread.start()
        logger.info("FakeAlpacaTicker: ticking every %.0fs", self._interval)

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            self._stop.wait(timeout=self._interval)
            if self._stop.is_set():
                break
            for sym in SYMBOLS:
                # Random walk: ±0.1% per tick
                drift = random.uniform(-0.001, 0.001)
                self._prices[sym] = int(self._prices[sym] * (1 + drift))
                mid_bps = self._prices[sym]
                spread  = random.randint(50, 200)   # 0.5–2 cents spread
                bid_usd = (mid_bps - spread // 2) / 10_000.0
                ask_usd = (mid_bps + spread // 2) / 10_000.0
                try:
                    self._on_quote(sym, bid_usd, ask_usd)
                except Exception as e:
                    logger.debug("FakeAlpacaTicker on_quote error: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
#  4. IN-MEMORY DATABASE
#     Replaces psycopg2 — stores trades and positions in dicts
# ─────────────────────────────────────────────────────────────────────────────

class InMemoryDB:
    """Drop-in replacement for TimescaleDB during development."""

    def __init__(self):
        self.trades:    list = []
        self.positions: dict = {}

    def insert_trade(self, trade: dict):
        self.trades.append(trade)

    def upsert_position(self, symbol: str, position: dict):
        self.positions[symbol] = position

    def print_summary(self):
        print(f"\n{'─'*50}")
        print(f"  DB Summary — {len(self.trades)} trades")
        for sym, pos in self.positions.items():
            print(f"  {sym}: qty={pos.get('net_quantity',0):+d}  "
                  f"pnl=${pos.get('realised_pnl_bps',0)/10_000:.2f}")
        print(f"{'─'*50}\n")


# ─────────────────────────────────────────────────────────────────────────────
#  PATCH LOADER
#  Monkey-patches Wing A's external dependencies at import time.
#  Call this BEFORE importing main.py so patches are in place first.
# ─────────────────────────────────────────────────────────────────────────────

def apply_patches(db: InMemoryDB, fake_ticker: FakeAlpacaTicker):
    """Patch boto3, psycopg2, and alpaca_feed to use local fakes."""
    import unittest.mock as mock

    # ── Patch S3: redirect to local files ────────────────────────────────────
    real_boto3_client = None
    try:
        import boto3
        real_boto3_client = boto3.client
    except ImportError:
        pass

    def fake_s3_client(service, **kwargs):
        if service != "s3":
            return real_boto3_client(service, **kwargs) if real_boto3_client else None

        class FakeS3:
            def list_objects_v2(self, Bucket, Prefix="", **kw):
                files = list(SIGNAL_DIR.glob("*.json"))
                return {"Contents": [{"Key": str(f)} for f in files]}

            def get_object(self, Bucket, Key, **kw):
                path = Path(Key)
                if not path.exists():
                    path = SIGNAL_DIR / path.name
                data = path.read_bytes()

                class Body:
                    def read(self_inner):
                        return data

                return {"Body": Body()}

        return FakeS3()

    import boto3
    boto3.client = fake_s3_client
    logger.info("Patch: boto3.client → FakeS3 (reading from %s)", SIGNAL_DIR)

    # ── Patch psycopg2: replace with in-memory store ──────────────────────────
    try:
        import psycopg2

        class FakeConn:
            autocommit = False

            class FakeCursor:
                def __enter__(self): return self
                def __exit__(self, *a): pass
                def execute(self, sql, params=None):
                    if "INSERT INTO trades" in sql and params:
                        db.insert_trade({
                            "trade_id": params[0] if params else 0,
                            "symbol":   params[1] if len(params) > 1 else "",
                            "price_bps": params[2] if len(params) > 2 else 0,
                            "quantity":  params[3] if len(params) > 3 else 0,
                        })
                    elif "INSERT INTO positions" in sql and params:
                        db.upsert_position(params[0], {
                            "net_quantity":      params[1] if len(params) > 1 else 0,
                            "avg_cost_bps":      params[2] if len(params) > 2 else 0,
                            "realised_pnl_bps":  params[3] if len(params) > 3 else 0,
                            "unrealised_pnl_bps": params[4] if len(params) > 4 else 0,
                        })

            def cursor(self): return self.FakeCursor()
            def commit(self): pass
            def rollback(self): pass

        psycopg2.connect = lambda *a, **kw: FakeConn()
        logger.info("Patch: psycopg2.connect → InMemoryDB")
    except ImportError:
        pass

    # ── Patch AlpacaFeed: replace WebSocket with FakeTicker ──────────────────
    import gateway.alpaca_feed as af_module

    OriginalAlpacaFeed = af_module.AlpacaFeed

    class PatchedAlpacaFeed:
        def __init__(self, on_quote, cfg=None):
            self._ticker = fake_ticker
            # Wire the on_quote callback from main.py into our fake ticker
            self._ticker._on_quote = on_quote

        def start(self):
            self._ticker.start()

        def stop(self):
            self._ticker.stop()

    af_module.AlpacaFeed = PatchedAlpacaFeed
    logger.info("Patch: AlpacaFeed → FakeAlpacaTicker (%.0fs ticks)", 2.0)


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Wing A development server")
    parser.add_argument("--with-wing-a", action="store_true",
                        help="Also launch Wing A's main.py in the same process")
    parser.add_argument("--tick-interval", type=float, default=2.0,
                        help="Seconds between fake Alpaca price ticks (default: 2)")
    parser.add_argument("--signal-interval", type=float, default=15.0,
                        help="Seconds between fake signal file writes (default: 15)")
    args = parser.parse_args()

    print("""
╔══════════════════════════════════════════╗
║  QuantCore Wing A — Development Server  ║
╚══════════════════════════════════════════╝
""")

    # Start mock C++ engine
    engine = MockCppEngine()
    engine.start()

    # Start fake signal writer
    sig_writer = FakeSignalWriter(interval_s=args.signal_interval)
    sig_writer.start()

    # Create in-memory DB and fake ticker (ticker wired in apply_patches)
    db     = InMemoryDB()
    ticker = FakeAlpacaTicker(on_quote=lambda *a: None,
                               interval_s=args.tick_interval)

    if args.with_wing_a:
        # Apply all patches then launch Wing A in-process
        apply_patches(db, ticker)

        # Patch signal ingestor to read from local dir instead of real S3
        os.environ["SIGNAL_BUCKET"] = str(SIGNAL_DIR)

        logger.info("Launching Wing A main.py in-process...")
        print("\n" + "─"*44)
        print("  All services mocked. Wing A starting...")
        print("  Press Ctrl-C to stop.")
        print("─"*44 + "\n")

        try:
            # Import and run Wing A after patches are applied
            import main as wing_a_main
            wing_a_main.main()
        except KeyboardInterrupt:
            pass

    else:
        # Standalone mode — just run the mock services
        # Wing A is started separately: python main.py
        print(f"  Mock C++ engine  : PULL on :{PULL_PORT}  PUB on :{PUB_PORT}")
        print(f"  Signal files     : {SIGNAL_DIR}")
        print(f"  Tick interval    : {args.tick_interval}s")
        print(f"  Signal interval  : {args.signal_interval}s")
        print()
        print("  Now run Wing A in another terminal:")
        print("  cd wing_a && python main.py")
        print()
        print("  Press Ctrl-C to stop.")
        print("─"*44 + "\n")

        # Print DB summary every 30s
        try:
            while True:
                time.sleep(30)
                db.print_summary()
        except KeyboardInterrupt:
            pass

    # Shutdown
    logger.info("Shutting down dev server...")
    engine.stop()
    sig_writer.stop()
    logger.info("Done.")


if __name__ == "__main__":
    main()
