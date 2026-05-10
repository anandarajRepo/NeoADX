"""
ADX DI+/DI- Crossover Strategy — Nifty 50 Weekly Options
=========================================================
Instrument : Nifty 50 weekly CE / PE options (Tuesday expiry)
Data       : 1-minute candles via ICICI Breeze API
Signals    : DI+ crosses above DI- AND ADX >= 30  → Buy CE
             DI- crosses above DI+ AND ADX >= 30  → Buy PE
Exits      : DI reversal crossover  |  EOD square-off at 15:20
No SL      : exits are signal-driven only
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
import pytz

from broker.icici_breeze import BreezeClient
from config.settings import (
    ADX_PERIOD,
    ADX_THRESHOLD,
    CAPITAL_PER_CONTRACT,
    CANDLE_INTERVAL,
    ENTRY_CUTOFF_TIME,
    ENTRY_START_TIME,
    LIVE_TRADE,
    MAX_TRADES_PER_DAY,
    NIFTY_LOT_SIZE,
    SQUAREOFF_TIME,
    UNDERLYING,
    INDEX_EXCHANGE,
)
from utils.adx_calculator import calculate_adx, detect_crossover
from utils.options_helper import get_option_details

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class Position:
    symbol:       str
    option_type:  str          # "CE" or "PE"
    strike:       int
    expiry:       str
    entry_price:  float
    quantity:     int
    entry_time:   datetime
    order_id:     Optional[str] = None
    exit_price:   Optional[float] = None
    exit_time:    Optional[datetime] = None
    exit_reason:  str = ""

    @property
    def is_open(self) -> bool:
        return self.exit_price is None

    @property
    def pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        return (self.exit_price - self.entry_price) * self.quantity


# ── Strategy ──────────────────────────────────────────────────────────────────

class ADXCrossoverStrategy:
    """
    Main strategy class.  Typical usage:

        strategy = ADXCrossoverStrategy(live_trade=False)
        strategy.run()          # blocking intraday loop
    """

    def __init__(self, live_trade: Optional[bool] = None):
        """
        Parameters
        ----------
        live_trade:
            Override the LIVE_TRADE setting from config.  Pass True to enable
            real order placement, False for dry-run / simulation.  If None the
            value from config/settings.py (and .env) is used.
        """
        # Allow runtime override of the live-trade toggle
        self.live_trade: bool = live_trade if live_trade is not None else LIVE_TRADE
        if self.live_trade != LIVE_TRADE:
            logger.warning(
                "live_trade overridden at runtime: config=%s  effective=%s",
                LIVE_TRADE,
                self.live_trade,
            )

        self.client    = BreezeClient()
        self.positions: List[Position] = []
        self.trade_count: Dict[str, int] = {}   # symbol → trades today
        self._running  = False

    # ── Public API ────────────────────────────────────────────────────────────

    def enable_live_trade(self) -> None:
        """Dynamically enable live order placement."""
        logger.info("Live trading ENABLED.")
        self.live_trade = True

    def disable_live_trade(self) -> None:
        """Dynamically disable live order placement (dry-run mode)."""
        logger.info("Live trading DISABLED — running in dry-run mode.")
        self.live_trade = False

    def run(self) -> None:
        """Connect to Breeze and start the intraday strategy loop."""
        logger.info(
            "Starting ADX Crossover Strategy | live_trade=%s", self.live_trade
        )
        self.client.connect()
        self._running = True
        try:
            self._intraday_loop()
        finally:
            self._running = False
            logger.info("Strategy loop exited.")

    def stop(self) -> None:
        """Signal the strategy loop to stop after the current iteration."""
        self._running = False

    # ── Core loop ─────────────────────────────────────────────────────────────

    def _intraday_loop(self) -> None:
        """Poll every 60 seconds and process each new 1-minute candle close."""
        while self._running:
            now_ist = datetime.now(IST)
            now_str = now_ist.strftime("%H:%M")

            # Force-close all positions at square-off time
            if now_str >= SQUAREOFF_TIME:
                self._squareoff_all(reason="EOD square-off")
                logger.info("EOD square-off complete. Stopping strategy.")
                break

            # Fetch candles and compute indicators
            df = self._fetch_candles(UNDERLYING, INDEX_EXCHANGE)
            if df.empty or len(df) < ADX_PERIOD * 2:
                logger.debug("Not enough candle data yet (%d rows). Waiting…", len(df))
                time.sleep(60)
                continue

            df = calculate_adx(df, ADX_PERIOD)
            self._process_signals(df, now_ist)

            # Sleep until next minute boundary
            time.sleep(60)

    # ── Signal processing ─────────────────────────────────────────────────────

    def _process_signals(self, df: pd.DataFrame, now_ist: datetime) -> None:
        now_str = now_ist.strftime("%H:%M")

        # Check exit signals for open positions first (no time restriction)
        for pos in [p for p in self.positions if p.is_open]:
            self._check_exit_signal(pos, df)

        # Check entry signals within the allowed window
        if not (ENTRY_START_TIME <= now_str <= ENTRY_CUTOFF_TIME):
            return

        last = df.iloc[-1]
        prev = df.iloc[-2]

        adx_ok   = last["ADX"] >= ADX_THRESHOLD
        bull_cross = prev["DI_plus"] <= prev["DI_minus"] and last["DI_plus"] > last["DI_minus"]
        bear_cross = prev["DI_minus"] <= prev["DI_plus"] and last["DI_minus"] > last["DI_plus"]

        if adx_ok and bull_cross:
            logger.info(
                "Bullish crossover | DI+=%s DI-=%s ADX=%s",
                last["DI_plus"], last["DI_minus"], last["ADX"],
            )
            self._enter_trade("CE", now_ist)

        elif adx_ok and bear_cross:
            logger.info(
                "Bearish crossover | DI+=%s DI-=%s ADX=%s",
                last["DI_plus"], last["DI_minus"], last["ADX"],
            )
            self._enter_trade("PE", now_ist)

    def _check_exit_signal(self, pos: Position, df: pd.DataFrame) -> None:
        """Exit on DI reversal crossover."""
        last = df.iloc[-1]
        prev = df.iloc[-2]

        if pos.option_type == "CE":
            # Exit CE when DI- crosses above DI+
            if prev["DI_minus"] <= prev["DI_plus"] and last["DI_minus"] > last["DI_plus"]:
                self._exit_position(pos, reason="DI reversal (bearish crossover)")
        elif pos.option_type == "PE":
            # Exit PE when DI+ crosses above DI-
            if prev["DI_plus"] <= prev["DI_minus"] and last["DI_plus"] > last["DI_minus"]:
                self._exit_position(pos, reason="DI reversal (bullish crossover)")

    # ── Trade execution ───────────────────────────────────────────────────────

    def _enter_trade(self, option_type: str, now_ist: datetime) -> None:
        symbol = UNDERLYING
        count  = self.trade_count.get(symbol, 0)
        if count >= MAX_TRADES_PER_DAY:
            logger.info(
                "Max trades per day (%d) reached for %s. Skipping.",
                MAX_TRADES_PER_DAY, symbol,
            )
            return

        # Derive ATM strike from current spot
        spot        = self.client.get_index_ltp(symbol)
        strike, expiry_str, expiry_label = get_option_details(spot)
        quantity    = self._calculate_quantity(spot)
        right       = "call" if option_type == "CE" else "put"

        order_id = self.client.place_order(
            stock_code=symbol,
            expiry_date=expiry_str,
            strike_price=str(strike),
            right=right,
            action="buy",
            quantity=quantity,
        )

        pos = Position(
            symbol=symbol,
            option_type=option_type,
            strike=strike,
            expiry=expiry_label,
            entry_price=spot,   # approximation; ideally use option LTP
            quantity=quantity,
            entry_time=now_ist,
            order_id=order_id,
        )
        self.positions.append(pos)
        self.trade_count[symbol] = count + 1

        logger.info(
            "ENTRY | %s %s | Strike=%d | Expiry=%s | Qty=%d | Spot=%.2f | live=%s",
            option_type, symbol, strike, expiry_label, quantity, spot, self.live_trade,
        )

    def _exit_position(self, pos: Position, reason: str) -> None:
        """Square off a single open position."""
        right = "call" if pos.option_type == "CE" else "put"
        self.client.place_order(
            stock_code=pos.symbol,
            expiry_date=pos.expiry,
            strike_price=str(pos.strike),
            right=right,
            action="sell",
            quantity=pos.quantity,
        )
        pos.exit_time   = datetime.now(IST)
        pos.exit_reason = reason
        # exit_price would be updated from order fill; placeholder here
        pos.exit_price  = 0.0
        logger.info(
            "EXIT  | %s %s | Strike=%d | Reason=%s | live=%s",
            pos.option_type, pos.symbol, pos.strike, reason, self.live_trade,
        )

    def _squareoff_all(self, reason: str = "EOD square-off") -> None:
        open_positions = [p for p in self.positions if p.is_open]
        if not open_positions:
            logger.info("No open positions to square off.")
            return
        for pos in open_positions:
            self._exit_position(pos, reason=reason)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _fetch_candles(self, symbol: str, exchange: str) -> pd.DataFrame:
        """Fetch today's 1-minute candles for *symbol*."""
        now_ist   = datetime.now(IST)
        today_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
        return self.client.get_candles(
            stock_code=symbol,
            exchange=exchange,
            from_dt=today_open.astimezone(pytz.utc),
            to_dt=now_ist.astimezone(pytz.utc),
            interval=CANDLE_INTERVAL,
        )

    @staticmethod
    def _calculate_quantity(spot_price: float) -> int:
        """Determine lot quantity based on capital allocation."""
        # Approximate option premium as ~1 % of spot for ATM options;
        # actual premium should be fetched for production use.
        approx_premium = spot_price * 0.01
        lots = max(1, int(CAPITAL_PER_CONTRACT / (approx_premium * NIFTY_LOT_SIZE)))
        return lots * NIFTY_LOT_SIZE

    # ── Reporting ─────────────────────────────────────────────────────────────

    def summary(self) -> pd.DataFrame:
        """Return a DataFrame with today's trade log."""
        rows = []
        for p in self.positions:
            rows.append(
                {
                    "symbol":      p.symbol,
                    "option_type": p.option_type,
                    "strike":      p.strike,
                    "expiry":      p.expiry,
                    "entry_time":  p.entry_time,
                    "exit_time":   p.exit_time,
                    "exit_reason": p.exit_reason,
                    "pnl":         p.pnl,
                    "order_id":    p.order_id,
                }
            )
        return pd.DataFrame(rows)
