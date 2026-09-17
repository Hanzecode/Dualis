# wing_a/config/settings.py
# ─────────────────────────────────────────────────────────────────────────────
#  WING A SETTINGS
#  Single source of truth for every endpoint, limit, and symbol.
#  All other modules import from here — never hardcode values elsewhere.
#  In production: load from environment variables or AWS Secrets Manager.
# ─────────────────────────────────────────────────────────────────────────────

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict
import os

from dotenv import load_dotenv

# Load variables from a .env file at the project root (if present) into the
# process environment BEFORE any os.getenv() calls below run. This means you
# can just run `python main.py` from a plain terminal — no need to export
# TIMESCALE_URL/AWS_*/ALPACA_*/FRED_API_KEY by hand every time.
# Real environment variables (e.g. exported in your shell) still take
# precedence — load_dotenv() never overrides an already-set variable.
load_dotenv()


@dataclass
class ZmqConfig:
    # C++ engine PULL socket — we PUSH orders to this address
    # C++ engine binds (listens); we connect (send)
    push_endpoint: str = "tcp://localhost:5557"

    # C++ engine PUB socket — we SUB to receive fills
    # C++ engine binds; we connect and subscribe
    sub_endpoint: str = "tcp://localhost:5556"

    # Fill subscription topic — must match "fills " prefix in execution_engine.hpp
    fill_topic: str = "fills"

    # Reconnect interval if ZMQ socket drops (milliseconds)
    reconnect_ms: int = 1000


@dataclass
class SignalConfig:
    # Local directory where Wing B writes and Wing A reads signal files
    signal_dir: str = os.getenv(
        "LOCAL_SIGNAL_DIR",
        str(Path(__file__).resolve().parents[2] / "wing-bb" / "signals_out" / "signals")
    )

    # How often to poll the signal directory for new files (seconds)
    poll_interval_s: int = 10

    # Reject signals older than this (seconds) — prevents acting on stale data
    max_signal_age_s: int = 300   # 5 minutes


@dataclass
class AlpacaConfig:
    # Alpaca paper trading WebSocket — use live URL for real money
    ws_url: str = "wss://stream.data.alpaca.markets/v2/iex"
    api_key: str = os.getenv("ALPACA_API_KEY", "")
    api_secret: str = os.getenv("ALPACA_SECRET_KEY", "")
    base_url: str = "https://paper-api.alpaca.markets"


@dataclass
class RiskConfig:
    # Position limits (shares)
    max_position_per_symbol: int = 10_000
    max_order_size: int = 1_000

    # Portfolio limits
    max_total_notional_usd: float = 50_000.0   # $50k total market value
    max_concentration: float = 0.40             # Max 40% in one symbol

    # Loss limits
    max_daily_loss_usd: float = 1_000.0         # Halt if lose more than $1k/day

    # Rate limits
    max_orders_per_second: int = 10

    # Signal thresholds — ignore signals weaker than these
    min_signal_strength: float = 0.10
    min_confidence: float = 0.50


@dataclass
class PortfolioConfig:
    # Target annualised volatility for vol-targeting position sizing
    target_annual_vol: float = 0.15    # 15% annualised vol

    # Lookback window for realised vol estimation (trading days)
    vol_lookback_days: int = 20

    # Maximum weight any single position can take in the portfolio
    max_weight: float = 0.20           # 20% max per position

    # Minimum order size — don't send tiny orders
    min_order_shares: int = 1


@dataclass
class DatabaseConfig:
    # TimescaleDB connection string for trade/position persistence
    timescale_url: str = os.getenv(
        "TIMESCALE_URL",
        "postgresql://quant:quant@localhost:5432/quantcore"
    )

    # PostgreSQL RDS for Wing B factors/predictions (read-only from Wing A)
    rds_url: str = os.getenv(
        "RDS_URL",
        "postgresql://quant:quant@localhost:5432/quantcore_analytics"
    )


@dataclass
class Settings:
    # Universe of tradeable symbols — must match C++ engine's EngineConfig.symbols
    # Extended to cover Wing B's full SECTOR_UNIVERSE (common/config.py in wing-bb)
    # so every Wing B signal has a symbol Wing A can actually trade. TSLA kept
    # from the original Wing A universe even though Wing B doesn't score it.
    symbols: List[str] = field(default_factory=lambda: [
        "AAPL", "MSFT", "TSLA", "NVDA", "GOOGL",   # Technology
        "JNJ", "PFE", "UNH", "ABBV",                # Healthcare
        "JPM", "BAC", "GS", "MS",                   # Financials
        "XOM", "CVX", "COP", "SLB",                 # Energy
    ])

    zmq:       ZmqConfig       = field(default_factory=ZmqConfig)
    signals:   SignalConfig    = field(default_factory=SignalConfig)
    alpaca:    AlpacaConfig    = field(default_factory=AlpacaConfig)
    risk:      RiskConfig      = field(default_factory=RiskConfig)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    db:        DatabaseConfig  = field(default_factory=DatabaseConfig)

    # Synthetic starting cash for the paper ledger — not a real balance
    # anywhere (Alpaca, a bank, etc.), just the number PnLTracker's cash
    # account starts counting down from. See PnLTracker.cash_usd.
    # Kept at $50k to match RiskConfig.max_total_notional_usd below (100%
    # deployment against this balance) rather than resizing the risk limits
    # themselves — see the interview-prep discussion for why that's the
    # simpler of the two ways to keep everything internally consistent.
    starting_cash_usd: float = float(os.getenv("STARTING_CASH_USD", "50000"))

    @property
    def s3(self) -> SignalConfig:
        """Alias for backward compatibility."""
        return self.signals

    # Logging
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    log_file:  str = "logs/wing_a.log"


# Module-level singleton — import this everywhere
settings = Settings()
