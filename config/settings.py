import os
from dotenv import load_dotenv

load_dotenv()

# ── Broker credentials ──────────────────────────────────────────────────────
BREEZE_API_KEY = os.getenv("BREEZE_API_KEY", "")
BREEZE_API_SECRET = os.getenv("BREEZE_API_SECRET", "")
BREEZE_SESSION_TOKEN = os.getenv("BREEZE_SESSION_TOKEN", "")

# ── Live-trade toggle ────────────────────────────────────────────────────────
# When False the strategy logs signals but does NOT send orders to the broker.
LIVE_TRADE: bool = os.getenv("LIVE_TRADE", "false").lower() == "true"
DRY_RUN_LOG: bool = os.getenv("DRY_RUN_LOG", "true").lower() == "true"

# ── Instrument ───────────────────────────────────────────────────────────────
UNDERLYING = "NIFTY"          # NSE index symbol
EXCHANGE = "NFO"              # Exchange for options
INDEX_EXCHANGE = "NSE"        # Exchange for underlying price feed

# ── Strategy parameters ──────────────────────────────────────────────────────
ADX_PERIOD = 16               # ADX / DI lookback period (candles)
ADX_THRESHOLD = 30.0          # Minimum ADX to take a trade
CANDLE_INTERVAL = "1minute"   # Breeze API interval string

# ── Session timings (IST) ────────────────────────────────────────────────────
MARKET_OPEN_TIME  = "09:15"   # Market opens
ENTRY_START_TIME  = "09:30"   # No entries before this
ENTRY_CUTOFF_TIME = "14:45"   # No new entries after this
SQUAREOFF_TIME    = "15:20"   # Force-close all positions
MARKET_CLOSE_TIME = "15:30"   # Market closes

# ── Risk / sizing ────────────────────────────────────────────────────────────
CAPITAL_PER_CONTRACT = 300_000   # ₹3,00,000 per contract
MAX_TRADES_PER_DAY   = 5         # Max legs per symbol per day
NIFTY_LOT_SIZE       = 75        # Nifty option lot size (verify before use)

# ── Option expiry ────────────────────────────────────────────────────────────
EXPIRY_DAY = 1   # 0=Mon … 6=Sun  (1 = Tuesday for Nifty weekly)
