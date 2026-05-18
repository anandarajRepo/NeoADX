"""Accumulates live price ticks into closed 1-minute OHLCV candles."""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime
from typing import Callable, List

import pandas as pd
import pytz

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")


class CandleBuffer:
    """
    Thread-safe tick aggregator.

    Call ``on_tick(ltp, timestamp)`` for every incoming price update.
    Each time a 1-minute candle closes a list of registered callbacks is
    invoked with the closed candle dict so downstream logic can react
    immediately without polling.
    """

    def __init__(self, max_candles: int = 300) -> None:
        self._closed: deque = deque(maxlen=max_candles)
        self._current: dict | None = None
        self._lock = threading.Lock()
        self._callbacks: List[Callable[[dict], None]] = []

    # ── Public API ────────────────────────────────────────────────────────────

    def register_candle_close_callback(self, fn: Callable[[dict], None]) -> None:
        """Register *fn* to be called (in a daemon thread) on each candle close."""
        self._callbacks.append(fn)

    def on_tick(self, ltp: float, timestamp: datetime | None = None) -> None:
        """Feed one price tick into the buffer."""
        if ltp <= 0:
            return
        ts = timestamp if timestamp is not None else datetime.now(IST)
        # Floor to minute boundary
        minute_ts = ts.replace(second=0, microsecond=0)

        closed_candle: dict | None = None
        with self._lock:
            if self._current is None:
                self._current = _new_candle(minute_ts, ltp)
            elif minute_ts > self._current["timestamp"]:
                closed_candle = dict(self._current)
                self._closed.append(closed_candle)
                self._current = _new_candle(minute_ts, ltp)
            else:
                _update_candle(self._current, ltp)

        if closed_candle is not None:
            logger.debug(
                "Candle closed | %s O=%.2f H=%.2f L=%.2f C=%.2f",
                closed_candle["timestamp"].strftime("%H:%M"),
                closed_candle["open"], closed_candle["high"],
                closed_candle["low"],  closed_candle["close"],
            )
            self._fire(closed_candle)

    def get_dataframe(self) -> pd.DataFrame:
        """Return a DataFrame of all closed candles (oldest first)."""
        with self._lock:
            rows = list(self._closed)
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df.sort_values("timestamp", inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df

    def candle_count(self) -> int:
        with self._lock:
            return len(self._closed)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _fire(self, candle: dict) -> None:
        for cb in list(self._callbacks):
            threading.Thread(target=_safe_call, args=(cb, candle), daemon=True).start()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _new_candle(ts: datetime, ltp: float) -> dict:
    return {"timestamp": ts, "open": ltp, "high": ltp, "low": ltp, "close": ltp, "volume": 0}


def _update_candle(candle: dict, ltp: float) -> None:
    candle["high"]  = max(candle["high"], ltp)
    candle["low"]   = min(candle["low"],  ltp)
    candle["close"] = ltp


def _safe_call(fn: Callable, arg) -> None:
    try:
        fn(arg)
    except Exception as exc:
        logger.error("Candle-close callback raised: %s", exc)
