"""Kotak Neo broker client for NeoADX — wraps neo-api-client."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from config.settings import (
    EXCHANGE,
    LIVE_TRADE,
    DRY_RUN_LOG,
)
from utils.auth_helper import get_neo_client, refresh_if_needed

logger = logging.getLogger(__name__)


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

    def get_index_ltp(self, stock_code: str = "NIFTY") -> float:
        """Return the last traded price for an index via Kotak Neo quotes."""
        try:
            resp = self.api.quotes(
                instrument_tokens=[{
                    "instrument_token": stock_code,
                    "exchange_segment": "nse_idx",
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
