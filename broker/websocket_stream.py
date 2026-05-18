"""Kotak Neo WebSocket tick streamer for NeoADX."""

from __future__ import annotations

import logging
import time
import threading
from datetime import datetime
from typing import Callable

import pytz

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# Instrument token for Nifty 50 index on Kotak Neo
_NIFTY_TOKEN = [{"instrument_token": "NIFTY", "exchange_segment": "nse_idx"}]


class NeoWebSocketStream:
    """
    Connects to Kotak Neo's WebSocket feed and calls *tick_callback* for
    every Nifty tick received.

    tick_callback signature: ``(ltp: float, timestamp: datetime) -> None``
    """

    def __init__(
        self,
        neo_client,
        tick_callback: Callable[[float, datetime], None],
        reconnect_delay: int = 5,
    ) -> None:
        self._api = neo_client
        self._tick_callback = tick_callback
        self._reconnect_delay = reconnect_delay
        self._running = False

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Open the WebSocket and subscribe to NIFTY ticks (non-blocking)."""
        self._running = True
        self._subscribe()
        logger.info("NeoWebSocketStream started — subscribing to NIFTY ticks.")

    def stop(self) -> None:
        """Disconnect and stop reconnect attempts."""
        self._running = False
        try:
            self._api.un_subscribe(_NIFTY_TOKEN, isIndex=True)
        except Exception:
            pass
        try:
            self._api.close_connection()
        except Exception:
            pass
        logger.info("NeoWebSocketStream stopped.")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _subscribe(self) -> None:
        try:
            self._api.subscribe(
                instrument_tokens=_NIFTY_TOKEN,
                isIndex=True,
                isDepth=False,
                on_message=self._on_message,
                on_open=self._on_open,
                on_close=self._on_close,
                on_error=self._on_error,
            )
        except Exception as exc:
            logger.error("subscribe() failed: %s", exc)
            self._schedule_reconnect()

    def _on_open(self, message) -> None:
        logger.info("WebSocket connected: %s", message)

    def _on_close(self, message) -> None:
        logger.warning("WebSocket closed: %s", message)
        self._schedule_reconnect()

    def _on_error(self, message) -> None:
        logger.error("WebSocket error: %s", message)
        self._schedule_reconnect()

    def _on_message(self, message) -> None:
        logger.debug("WebSocket raw message received: %s", message)
        try:
            ticks = message if isinstance(message, list) else [message]
            for tick in ticks:
                if isinstance(tick, dict):
                    self._process_tick(tick)
                else:
                    logger.warning("Unexpected tick format (not dict): %s", type(tick).__name__)
        except Exception as exc:
            logger.error("_on_message processing error: %s", exc)

    def _process_tick(self, tick: dict) -> None:
        logger.debug("Processing tick: %s", tick)
        # Kotak Neo tick fields: lp = last price, ft = feed time (epoch secs)
        ltp_raw = (
            tick.get("lp")
            or tick.get("ltp")
            or tick.get("last_price")
            or tick.get("c")
        )
        if ltp_raw is None:
            logger.warning("Tick missing price field — keys present: %s", list(tick.keys()))
            return
        try:
            ltp = float(ltp_raw)
        except (TypeError, ValueError):
            logger.warning("Could not convert ltp_raw=%r to float", ltp_raw)
            return
        if ltp <= 0:
            logger.warning("Tick discarded — non-positive ltp: %s", ltp)
            return

        ts_raw = tick.get("ft") or tick.get("ts") or tick.get("timestamp")
        try:
            ts = datetime.fromtimestamp(int(ts_raw), tz=IST) if ts_raw else datetime.now(IST)
        except Exception:
            ts = datetime.now(IST)

        logger.info("Tick received — ltp=%.2f  ts=%s", ltp, ts.strftime("%H:%M:%S"))
        self._tick_callback(ltp, ts)

    def _schedule_reconnect(self) -> None:
        if not self._running:
            return
        logger.info("Scheduling WebSocket reconnect in %ds…", self._reconnect_delay)
        threading.Timer(self._reconnect_delay, self._reconnect).start()

    def _reconnect(self) -> None:
        if not self._running:
            return
        logger.info("Reconnecting WebSocket…")
        self._subscribe()
