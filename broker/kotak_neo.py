"""Kotak Neo broker client for NeoADX — wraps neo-api-client."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import pandas as pd
import requests

from config.settings import (
    CANDLE_INTERVAL,
    EXCHANGE,
    INDEX_EXCHANGE,
    LIVE_TRADE,
    DRY_RUN_LOG,
)
from utils.auth_helper import get_neo_client, refresh_if_needed

logger = logging.getLogger(__name__)

_CHART_BASE_URL = "https://gw-napi.kotaksecurities.com"
_NEO_FIN_KEY = "neotradeapi"

# Map Breeze-style interval strings to Kotak Neo chart resolution values
_INTERVAL_MAP = {
    "1minute": "1",
    "5minute": "5",
    "15minute": "15",
    "30minute": "30",
    "1hour": "60",
    "1day": "1D",
}


def auth() -> None:
    """Authenticate with Kotak Neo and cache the session token."""
    get_neo_client()
    logger.info("Authentication successful — session token cached.")


class KotakNeoClient:
    """Manages a single Kotak Neo API session."""

    def __init__(self):
        self._client = None

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self, force: bool = False) -> None:
        """Authenticate with Kotak Neo (TOTP → MPIN two-step flow) and cache session."""
        self._client = get_neo_client(force=force)
        logger.info("Neo session established.")

    def refresh_session(self) -> None:
        """Re-authenticate if the cached token has expired."""
        if self._client is not None:
            refresh_if_needed(self._client)

    @property
    def api(self):
        if self._client is None:
            raise RuntimeError("KotakNeoClient not connected. Call connect() first.")
        return self._client

    # ── Market data ───────────────────────────────────────────────────────────

    def get_candles(
        self,
        stock_code: str,
        exchange: str,
        from_dt: datetime,
        to_dt: datetime,
        interval: str = CANDLE_INTERVAL,
        right: str = "",
        strike_price: str = "",
        expiry_date: str = "",
        product_type: str = "options",
    ) -> pd.DataFrame:
        """Fetch OHLCV candles from Kotak Neo chart/history endpoint."""
        resolution = _INTERVAL_MAP.get(interval, "1")

        # For options, build the trading symbol; otherwise use index symbol directly.
        if right and strike_price and expiry_date:
            opt_type = "CE" if right.lower() in ("call", "ce") else "PE"
            expiry_fmt = datetime.strptime(expiry_date, "%Y-%m-%dT%H:%M:%S.000Z").strftime("%d%b%y").upper()
            trading_symbol = f"{stock_code}{expiry_fmt}{strike_price}{opt_type}"
            exchange_segment = "nse_fo"
        else:
            trading_symbol = stock_code
            exchange_segment = "nse_cm" if exchange.upper() == "NSE" else exchange.lower()

        url = f"{_CHART_BASE_URL}/charts/1.0/chart/history"
        params = {
            "exchange": exchange_segment,
            "tradingSymbol": trading_symbol,
            "from": int(from_dt.timestamp()),
            "to": int(to_dt.timestamp()),
            "resolution": resolution,
        }
        headers = {
            "Authorization": f"Bearer {self.api.access_token}",
            "sid": getattr(self.api, "sid", "") or "",
            "neo-fin-key": _NEO_FIN_KEY,
            "Content-Type": "application/json",
        }

        try:
            resp = requests.get(url, params=params, headers=headers, timeout=30)
            resp.raise_for_status()
            raw = resp.json()
        except Exception as exc:
            logger.warning("get_candles failed for %s: %s", stock_code, exc)
            return pd.DataFrame()

        data = raw if isinstance(raw, dict) else {}
        if data.get("s") != "ok":
            logger.warning("No candle data for %s: %s", stock_code, data)
            return pd.DataFrame()

        timestamps = data.get("t", [])
        opens = data.get("o", [])
        highs = data.get("h", [])
        lows = data.get("l", [])
        closes = data.get("c", [])
        volumes = data.get("v", [])

        rows = []
        for i, ts in enumerate(timestamps):
            try:
                rows.append({
                    "timestamp": datetime.fromtimestamp(int(ts)),
                    "open": float(opens[i]),
                    "high": float(highs[i]),
                    "low": float(lows[i]),
                    "close": float(closes[i]),
                    "volume": int(volumes[i]) if i < len(volumes) else 0,
                })
            except Exception:
                continue

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df.sort_values("timestamp", inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df

    def get_index_ltp(self, stock_code: str = "NIFTY") -> float:
        """Return the last traded price for an index via Kotak Neo quotes."""
        try:
            resp = self.api.quotes(
                instrument_tokens=[{
                    "instrument_token": stock_code,
                    "exchange_segment": "nse_cm",
                }],
                quote_type="ltp",
            )
            data = resp if isinstance(resp, dict) else (resp[0] if resp else {})
            ltp = float(data.get("ltp", 0) or data.get("last_price", 0))
            if ltp:
                return ltp
        except Exception as exc:
            logger.error("get_index_ltp failed for %s: %s", stock_code, exc)
        raise ValueError(f"Could not fetch LTP for {stock_code}")

    # ── Order management ──────────────────────────────────────────────────────

    def place_order(
        self,
        stock_code: str,
        expiry_date: str,
        strike_price: str,
        right: str,          # "call"/"ce" or "put"/"pe"
        action: str,         # "buy" or "sell"
        quantity: int,
        price: float = 0,
        order_type: str = "market",
        product: str = "options",
    ) -> Optional[str]:
        """
        Place an options order via Kotak Neo.

        When LIVE_TRADE is False the order is only logged (dry-run mode).
        Returns the order id string, or None in dry-run mode.
        """
        opt_type = "CE" if right.lower() in ("call", "ce") else "PE"
        try:
            expiry_fmt = datetime.strptime(expiry_date, "%Y-%m-%dT%H:%M:%S.000Z").strftime("%d%b%y").upper()
        except ValueError:
            expiry_fmt = expiry_date
        trading_symbol = f"{stock_code}{expiry_fmt}{strike_price}{opt_type}"
        transaction_type = action.upper()
        neo_order_type = "MKT" if order_type.lower() == "market" else "L"

        order_details = (
            f"[{transaction_type} {trading_symbol} Qty={quantity}]"
        )

        if not LIVE_TRADE:
            if DRY_RUN_LOG:
                logger.info("DRY-RUN — simulated order: %s", order_details)
            return None

        try:
            resp = self.api.place_order(
                exchange_segment="nse_fo",
                product="NRML",
                price=str(price),
                order_type=neo_order_type,
                quantity=str(quantity),
                validity="DAY",
                trading_symbol=trading_symbol,
                transaction_type=transaction_type,
                amo="NO",
                disclosed_quantity="0",
                market_protection="0",
                pf="N",
                trigger_price="0",
                tag="NeoADX_Strategy",
            )
            order_id = str(resp.get("nOrdNo") or resp.get("order_id", "")) if resp else ""
            if order_id:
                logger.info("Order placed %s  order_id=%s", order_details, order_id)
                return order_id
            logger.error("Order FAILED %s  response=%s", order_details, resp)
        except Exception as exc:
            logger.error("place_order exception %s: %s", order_details, exc)
        return None
