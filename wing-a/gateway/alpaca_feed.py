# wing_a/gateway/alpaca_feed.py
# ─────────────────────────────────────────────────────────────────────────────
#  ALPACA WEBSOCKET MARKET DATA FEED
#  Connects to Alpaca's real-time data stream.
#  Receives L2 quote updates (bid/ask) for each symbol.
#  Does two things with each update:
#    1. Forwards price to PortfolioConstructor (for sizing) and RiskGate
#    2. Sends MARKET_DEPTH message to C++ engine via ZMQ PUSH
#       so FlatOrderBook stays current
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import json
import logging
import threading
from typing import Callable, Dict, List, Optional

import websocket   # pip install websocket-client
import zmq

from config.settings import Settings, settings as default_settings

logger = logging.getLogger(__name__)


class AlpacaFeed:
    """
    Connects to Alpaca IEX WebSocket for real-time quote updates.
    Sends MARKET_DEPTH updates to the C++ FlatOrderBook via ZMQ PUSH.
    Also notifies Python-side components (portfolio constructor, risk gate).

    WebSocket message format from Alpaca (quote):
    [{"T":"q","S":"AAPL","bp":189.50,"bs":200,"ap":189.55,"as":150,"t":"..."}]
    T=q is quote, bp=bid price, bs=bid size, ap=ask price, as=ask size
    """

    WS_AUTH_MSG  = lambda self: json.dumps({"action": "auth",
                                             "key":    self._cfg.alpaca.api_key,
                                             "secret": self._cfg.alpaca.api_secret})
    WS_SUB_MSG   = lambda self: json.dumps({"action":  "subscribe",
                                             "quotes":  self._cfg.symbols})

    def __init__(
        self,
        on_quote: Callable[[str, float, float], None],  # (symbol, bid_usd, ask_usd)
        cfg: Settings = default_settings,
    ):
        self._on_quote = on_quote   # → PortfolioConstructor.update_mid_price + RiskGate
        self._cfg = cfg

        # ZMQ PUSH socket to C++ engine — sends MARKET_DEPTH messages
        self._ctx = zmq.Context.instance()
        self._push = self._ctx.socket(zmq.PUSH)
        self._push.setsockopt(zmq.LINGER, 0)
        self._push.setsockopt(zmq.SNDHWM, 5000)   # Large HWM — market data is high volume
        self._push.connect(cfg.zmq.push_endpoint)
        self._zmq_lock = threading.Lock()

        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._run_ws,
            name="alpaca-feed",
            daemon=True,
        )
        self._thread.start()
        logger.info("AlpacaFeed starting — url=%s symbols=%s",
                    self._cfg.alpaca.ws_url, self._cfg.symbols)

    def stop(self) -> None:
        self._running = False
        if self._ws:
            self._ws.close()
        if self._thread:
            self._thread.join(timeout=5)
        self._push.close()
        logger.info("AlpacaFeed stopped")

    # ── Private ───────────────────────────────────────────────────────────────

    def _run_ws(self) -> None:
        self._ws = websocket.WebSocketApp(
            self._cfg.alpaca.ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        # run_forever: blocks until connection closes. Reconnect loop in thread.
        while self._running:
            try:
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                logger.error("AlpacaFeed WS error: %s", e)
            if self._running:
                logger.info("AlpacaFeed reconnecting in 5s...")
                threading.Event().wait(timeout=5)

    def _on_open(self, ws) -> None:
        logger.debug("AlpacaFeed WS connected — authenticating")
        ws.send(self.WS_AUTH_MSG())

    def _on_message(self, ws, raw: str) -> None:
        try:
            messages = json.loads(raw)
        except json.JSONDecodeError:
            return

        for msg in messages:
            msg_type = msg.get("T")
            if msg_type == "success":
                if msg.get("msg") == "authenticated":
                    logger.info("AlpacaFeed authenticated — subscribing to quotes")
                    ws.send(self.WS_SUB_MSG())
            elif msg_type == "q":
                self._handle_quote(msg)

    def _handle_quote(self, msg: dict) -> None:
        """Process one quote update."""
        symbol = msg.get("S", "")
        if symbol not in self._cfg.symbols:
            return

        bid_usd = float(msg.get("bp", 0))
        ask_usd = float(msg.get("ap", 0))
        bid_size = int(msg.get("bs", 0))
        ask_size = int(msg.get("as", 0))

        if bid_usd <= 0 or ask_usd <= 0:
            return

        # Convert to basis points (price × 10,000)
        bid_bps = int(bid_usd * 10_000)
        ask_bps = int(ask_usd * 10_000)
        mid_usd = (bid_usd + ask_usd) / 2.0

        # 1. Notify Python-side components (portfolio, risk gate)
        try:
            self._on_quote(symbol, bid_usd, ask_usd)
        except Exception as e:
            logger.error("on_quote callback error: %s", e)

        # 2. Send MARKET_DEPTH updates to C++ FlatOrderBook
        #    Two messages: one for bid side, one for ask side
        self._send_depth(symbol, "BID", bid_bps, bid_size)
        self._send_depth(symbol, "ASK", ask_bps, ask_size)

    def _send_depth(self, symbol: str, side: str, price_bps: int, qty: int) -> None:
        """Send one MARKET_DEPTH message to the C++ PushPullReceiver."""
        msg = json.dumps({
            "type":      "MARKET_DEPTH",
            "symbol":    symbol,
            "side":      side,       # "BID" or "ASK" — matches push_pull_receiver.hpp
            "price_bps": price_bps,
            "qty":       qty,
        })
        try:
            with self._zmq_lock:
                self._push.send_string(msg, flags=zmq.NOBLOCK)
        except zmq.Again:
            # Drop silently — market data is high frequency, occasional drops are fine
            pass
        except zmq.ZMQError as e:
            logger.error("AlpacaFeed ZMQ PUSH error: %s", e)

    def _on_error(self, ws, error) -> None:
        logger.error("AlpacaFeed WS error: %s", error)

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        logger.info("AlpacaFeed WS closed: %s %s", close_status_code, close_msg)
