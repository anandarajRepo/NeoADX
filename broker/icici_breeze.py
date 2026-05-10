"""Thin wrapper around breeze-connect for historical data and order placement."""

import logging
from datetime import datetime, date
from typing import Optional

import pandas as pd
from breeze_connect import BreezeConnect

from config.settings import (
    BREEZE_API_KEY,
    BREEZE_API_SECRET,
    BREEZE_SESSION_TOKEN,
    CANDLE_INTERVAL,
    EXCHANGE,
    INDEX_EXCHANGE,
    LIVE_TRADE,
    DRY_RUN_LOG,
)

logger = logging.getLogger(__name__)


class BreezeClient:
    """Manages a single BreezeConnect session."""

    def __init__(self):
        self._api: Optional[BreezeConnect] = None

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Initialise session using credentials from environment / .env."""
        self._api = BreezeConnect(api_key=BREEZE_API_KEY)
        self._api.generate_session(
            api_secret=BREEZE_API_SECRET,
            session_token=BREEZE_SESSION_TOKEN,
        )
        logger.info("Breeze session established.")

    @property
    def api(self) -> BreezeConnect:
        if self._api is None:
            raise RuntimeError("BreezeClient not connected. Call connect() first.")
        return self._api

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
        """Fetch OHLCV candles and return a cleaned DataFrame."""
        params = dict(
            interval=interval,
            from_date=from_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            to_date=to_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            stock_code=stock_code,
            exchange_code=exchange,
        )
        if right:
            params.update(
                right=right,
                strike_price=strike_price,
                expiry_date=expiry_date,
                product_type=product_type,
            )

        resp = self.api.get_historical_data_v2(**params)
        if not resp or resp.get("Status") != 200:
            logger.warning("No candle data returned for %s: %s", stock_code, resp)
            return pd.DataFrame()

        df = pd.DataFrame(resp["Success"])
        df.rename(
            columns={
                "datetime": "timestamp",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "volume",
            },
            inplace=True,
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df.sort_values("timestamp", inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df

    def get_index_ltp(self, stock_code: str = "NIFTY") -> float:
        """Return the last traded price for an index."""
        resp = self.api.get_quotes(
            stock_code=stock_code,
            exchange_code=INDEX_EXCHANGE,
            expiry_date="",
            product_type="cash",
            right="",
            strike_price="",
        )
        if resp and resp.get("Status") == 200 and resp["Success"]:
            return float(resp["Success"][0]["ltp"])
        raise ValueError(f"Could not fetch LTP for {stock_code}")

    # ── Order management ──────────────────────────────────────────────────────

    def place_order(
        self,
        stock_code: str,
        expiry_date: str,
        strike_price: str,
        right: str,          # "call" or "put"
        action: str,         # "buy" or "sell"
        quantity: int,
        price: float = 0,
        order_type: str = "market",
        product: str = "options",
    ) -> Optional[str]:
        """
        Place an order via Breeze.

        When LIVE_TRADE is False the order is only logged (dry-run mode).
        Returns the order id string, or None in dry-run mode.
        """
        order_details = (
            f"[{action.upper()} {right.upper()} {stock_code} "
            f"Strike={strike_price} Expiry={expiry_date} Qty={quantity}]"
        )

        if not LIVE_TRADE:
            if DRY_RUN_LOG:
                logger.info("DRY-RUN — simulated order: %s", order_details)
            return None

        resp = self.api.place_order(
            stock_code=stock_code,
            exchange_code=EXCHANGE,
            product=product,
            action=action,
            order_type=order_type,
            stoploss="0",
            quantity=str(quantity),
            price=str(price),
            validity="day",
            validity_date=date.today().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            disclosed_quantity="0",
            expiry_date=expiry_date,
            right=right,
            strike_price=strike_price,
            user_remark="NeoADX_Strategy",
        )
        if resp and resp.get("Status") == 200:
            order_id = resp["Success"][0].get("order_id", "unknown")
            logger.info("Order placed %s  order_id=%s", order_details, order_id)
            return order_id

        logger.error("Order FAILED %s  response=%s", order_details, resp)
        return None
