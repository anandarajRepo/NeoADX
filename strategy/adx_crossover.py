"""
ADX DI+/DI- Crossover Strategy — Nifty 50 Weekly Options
=========================================================
Instrument : Nifty 50 weekly CE / PE options (Tuesday expiry)
Data       : 1-minute candles built from live WebSocket ticks
Signals    : DI+ crosses above DI- AND ADX >= 30  → Buy CE
             DI- crosses above DI+ AND ADX >= 30  → Buy PE
Exits      : DI reversal crossover  |  EOD square-off at 15:20
No SL      : exits are signal-driven only
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd
import pytz

from broker.kotak_neo import KotakNeoClient
from broker.websocket_stream import NeoWebSocketStream
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
from utils.adx_calculator import calculate_adx
from utils.candle_buffer import CandleBuffer
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
        strategy.run()          # blocking until EOD or stop()
    """

    def __init__(self, live_trade: Optional[bool] = None):
        self.live_trade: bool = live_trade if live_trade is not None else LIVE_TRADE
        if self.live_trade != LIVE_TRADE:
            logger.warning(
                "live_trade overridden at runtime: config=%s  effective=%s",
                LIVE_TRADE, self.live_trade,
            )

        self.client    = KotakNeoClient()
        self.positions: List[Position] = []
        self.trade_count: Dict[str, int] = {}
        self._running  = False
        self._candle_buffer = CandleBuffer(max_candles=300)
        self._ws_stream: Optional[NeoWebSocketStream] = None
        # Event set each time a candle closes so the main thread can wake up
        self._candle_event = threading.Event()
        self._signal_lock  = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def enable_live_trade(self) -> None:
        logger.info("Live trading ENABLED.")
        self.live_trade = True

    def disable_live_trade(self) -> None:
        logger.info("Live trading DISABLED — running in dry-run mode.")
        self.live_trade = False

    def run(self) -> None:
        """Connect to Kotak Neo, start WebSocket, and block until EOD."""
        logger.info("Starting ADX Crossover Strategy | live_trade=%s", self.live_trade)
        self.client.connect()
        self._running = True

        # Register candle-close handler before starting stream
        self._candle_buffer.register_candle_close_callback(self._on_candle_close)

        # Start WebSocket tick stream
        self._ws_stream = NeoWebSocketStream(
            neo_client=self.client.api,
            tick_callback=self._candle_buffer.on_tick,
        )
        self._ws_stream.start()

        try:
            self._event_loop()
        finally:
            self._running = False
            if self._ws_stream:
                self._ws_stream.stop()
            logger.info("Strategy loop exited.")

    def stop(self) -> None:
        """Signal the strategy to stop after the current candle."""
        self._running = False
        self._candle_event.set()   # unblock any waiting

    # ── Core event loop ───────────────────────────────────────────────────────

    def _event_loop(self) -> None:
        """
        Block waiting for candle-close events.  Each event triggers ADX
        recalculation and signal processing.  Exits at SQUAREOFF_TIME.
        """
        logger.info("Event loop started — waiting for live candles from WebSocket.")
        while self._running:
            now_ist = datetime.now(IST)
            if now_ist.strftime("%H:%M") >= SQUAREOFF_TIME:
                self._squareoff_all(reason="EOD square-off")
                logger.info("EOD square-off complete. Stopping strategy.")
                break

            # Wait up to 90 seconds for the next candle-close event
            triggered = self._candle_event.wait(timeout=90)
            self._candle_event.clear()

            if not triggered:
                # Heartbeat: no tick received — log and continue
                logger.debug("No candle closed in last 90s — waiting…")
                continue

            if not self._running:
                break

            # Re-check time after wakeup
            now_ist = datetime.now(IST)
            if now_ist.strftime("%H:%M") >= SQUAREOFF_TIME:
                self._squareoff_all(reason="EOD square-off")
                logger.info("EOD square-off complete. Stopping strategy.")
                break

            n = self._candle_buffer.candle_count()
            if n < ADX_PERIOD * 2:
                logger.debug("Buffered candles (%d) below minimum (%d). Waiting…", n, ADX_PERIOD * 2)
                continue

            df = self._candle_buffer.get_dataframe()
            if df.empty:
                continue

            df = calculate_adx(df, ADX_PERIOD)
            self._process_signals(df, now_ist)

    def _on_candle_close(self, candle: dict) -> None:
        """Called from CandleBuffer in a daemon thread on each candle close."""
        self._candle_event.set()

    # ── Signal processing ─────────────────────────────────────────────────────

    def _process_signals(self, df: pd.DataFrame, now_ist: datetime) -> None:
        now_str = now_ist.strftime("%H:%M")

        with self._signal_lock:
            # Check exit signals for open positions first (no time restriction)
            for pos in [p for p in self.positions if p.is_open]:
                self._check_exit_signal(pos, df)

            if not (ENTRY_START_TIME <= now_str <= ENTRY_CUTOFF_TIME):
                return

            last = df.iloc[-1]
            prev = df.iloc[-2]

            adx_ok     = last["ADX"] >= ADX_THRESHOLD
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
            if prev["DI_minus"] <= prev["DI_plus"] and last["DI_minus"] > last["DI_plus"]:
                self._exit_position(pos, reason="DI reversal (bearish crossover)")
        elif pos.option_type == "PE":
            if prev["DI_plus"] <= prev["DI_minus"] and last["DI_plus"] > last["DI_minus"]:
                self._exit_position(pos, reason="DI reversal (bullish crossover)")

    # ── Trade execution ───────────────────────────────────────────────────────

    def _enter_trade(self, option_type: str, now_ist: datetime) -> None:
        symbol = UNDERLYING
        count  = self.trade_count.get(symbol, 0)
        if count >= MAX_TRADES_PER_DAY:
            logger.info("Max trades per day (%d) reached for %s. Skipping.", MAX_TRADES_PER_DAY, symbol)
            return

        spot = self.client.get_index_ltp(symbol)
        strike, expiry_str, expiry_label = get_option_details(spot)
        quantity = self._calculate_quantity(spot)
        right    = "call" if option_type == "CE" else "put"

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
            entry_price=spot,
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
        pos.exit_price  = 0.0   # updated from order fill in production
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

    @staticmethod
    def _calculate_quantity(spot_price: float) -> int:
        approx_premium = spot_price * 0.01
        lots = max(1, int(CAPITAL_PER_CONTRACT / (approx_premium * NIFTY_LOT_SIZE)))
        return lots * NIFTY_LOT_SIZE

    # ── Reporting ─────────────────────────────────────────────────────────────

    def summary(self) -> pd.DataFrame:
        rows = []
        for p in self.positions:
            rows.append({
                "symbol":      p.symbol,
                "option_type": p.option_type,
                "strike":      p.strike,
                "expiry":      p.expiry,
                "entry_time":  p.entry_time,
                "exit_time":   p.exit_time,
                "exit_reason": p.exit_reason,
                "pnl":         p.pnl,
                "order_id":    p.order_id,
            })
        return pd.DataFrame(rows)
