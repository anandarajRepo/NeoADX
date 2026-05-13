"""
NeoADX — Kotak Neo authentication helper.

Handles the two-step login flow:
  Step 2a: POST /tradeApiLogin  — TOTP verification → VIEW_TOKEN + VIEW_SID
  Step 2b: POST /tradeApiValidate — MPIN validation  → TRADING_TOKEN + TRADING_SID + BASE_URL

Credentials required (via env or interactive prompt):
  NEO_ACCESS_TOKEN  Developer API access token (Authorization header in auth requests)
  NEO_MOBILE        Registered mobile with country code, e.g. +91XXXXXXXXXX
  NEO_UCC           5-character client code from the developer portal
  NEO_MPIN          6-digit MPIN (optional; prompted interactively if absent)

Trading session values (TRADING_TOKEN, TRADING_SID, BASE_URL) are persisted to
.neo_token.json and reused for up to 20 hours.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

from config.settings import (
    NEO_CONSUMER_KEY,
    NEO_CONSUMER_SECRET,
    NEO_ACCESS_TOKEN,
    NEO_UCC,
    NEO_ENVIRONMENT,
)

logger = logging.getLogger(__name__)

_TOKEN_FILE = Path(".neo_token.json")
_TOKEN_EXPIRY_HOURS = 20

_LOGIN_URL = "https://mis.kotaksecurities.com/login/1.0/tradeApiLogin"
_VALIDATE_URL = "https://mis.kotaksecurities.com/login/1.0/tradeApiValidate"
_DEFAULT_BASE_URL = "https://gw-napi.kotaksecurities.com"
_NEO_FIN_KEY = "neotradeapi"

_TRANSIENT_STATUS_CODES = {502, 503, 504}
_MAX_TRANSIENT_RETRIES = 4


# ---------------------------------------------------------------------------
# Token cache helpers
# ---------------------------------------------------------------------------

def _load_cached_token() -> Optional[dict]:
    """Return cached session dict if still valid, else None."""
    if not _TOKEN_FILE.exists():
        return None
    try:
        data = json.loads(_TOKEN_FILE.read_text())
        saved_at = datetime.fromisoformat(data["saved_at"])
        if datetime.now() - saved_at < timedelta(hours=_TOKEN_EXPIRY_HOURS):
            logger.info("Using cached Neo session (saved %s)", saved_at.strftime("%H:%M"))
            return {
                "trading_token": data["trading_token"],
                "trading_sid": data["trading_sid"],
                "base_url": data["base_url"],
            }
        logger.info("Cached token expired — re-authenticating")
    except Exception as exc:
        logger.warning("Failed to read cached token: %s", exc)
    return None


def _save_token(trading_token: str, trading_sid: str, base_url: str) -> None:
    try:
        _TOKEN_FILE.write_text(json.dumps({
            "trading_token": trading_token,
            "trading_sid": trading_sid,
            "base_url": base_url,
            "saved_at": datetime.now().isoformat(),
        }))
    except Exception as exc:
        logger.warning("Could not persist token: %s", exc)


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _post_with_retry(url: str, headers: dict, payload: dict, timeout: int = 30) -> requests.Response:
    """POST with exponential-backoff retry for transient 5xx errors."""
    delay = 2
    for attempt in range(1, _MAX_TRANSIENT_RETRIES + 1):
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if resp.status_code not in _TRANSIENT_STATUS_CODES:
            return resp
        logger.warning(
            "Transient HTTP %s from %s (attempt %d/%d) — retrying in %ds…",
            resp.status_code, url, attempt, _MAX_TRANSIENT_RETRIES, delay,
        )
        if attempt < _MAX_TRANSIENT_RETRIES:
            time.sleep(delay)
            delay *= 2
    return resp


# ---------------------------------------------------------------------------
# Step 2a: TOTP login
# ---------------------------------------------------------------------------

def _do_totp_login(mobile: str, ucc: str, totp: str, access_token: str) -> tuple[str, str]:
    """POST /tradeApiLogin — returns (view_token, view_sid)."""
    auth_header = access_token if access_token.startswith("Bearer ") else access_token
    resp = _post_with_retry(
        _LOGIN_URL,
        headers={
            "Authorization": auth_header,
            "neo-fin-key": _NEO_FIN_KEY,
            "Content-Type": "application/json",
        },
        payload={"mobileNumber": mobile, "ucc": ucc, "totp": totp},
    )
    if not resp.ok:
        try:
            err_body = resp.json()
        except Exception:
            err_body = resp.text
        logger.error("TOTP login HTTP %s — response: %s", resp.status_code, err_body)
        resp.raise_for_status()
    data = resp.json().get("data", {})
    if data.get("status") != "success":
        raise RuntimeError(f"TOTP login failed: {data}")
    logger.info("TOTP login successful (kType=%s)", data.get("kType"))
    return data["token"], data["sid"]


# ---------------------------------------------------------------------------
# Step 2b: MPIN validation
# ---------------------------------------------------------------------------

def _do_mpin_validate(
    mpin: str,
    view_token: str,
    view_sid: str,
    access_token: str,
) -> tuple[str, str, str]:
    """POST /tradeApiValidate — returns (trading_token, trading_sid, base_url)."""
    auth_header = access_token if access_token.startswith("Bearer ") else access_token
    resp = _post_with_retry(
        _VALIDATE_URL,
        headers={
            "Authorization": auth_header,
            "neo-fin-key": _NEO_FIN_KEY,
            "sid": view_sid,
            "Auth": view_token,
            "Content-Type": "application/json",
        },
        payload={"mpin": mpin},
    )
    if not resp.ok:
        try:
            err_body = resp.json()
        except Exception:
            err_body = resp.text
        logger.error("MPIN validate HTTP %s — response: %s", resp.status_code, err_body)
        resp.raise_for_status()
    data = resp.json().get("data", {})
    if data.get("status") != "success":
        raise RuntimeError(f"MPIN validation failed: {data}")
    base_url = data.get("baseUrl") or _DEFAULT_BASE_URL
    if not data.get("baseUrl"):
        logger.warning("baseUrl missing in MPIN response — using default: %s", base_url)
    logger.info("MPIN validation successful (kType=%s)", data.get("kType"))
    return data["token"], data["sid"], base_url


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_neo_client():
    """
    Return an authenticated Kotak Neo API client.

    Performs the two-step authentication (TOTP → MPIN) on first run or after
    token expiry, then caches the trading session for subsequent runs.

    Returns a neo_api_client.NeoAPI instance with access_token, sid, and
    base_url set from the authenticated trading session.
    """
    try:
        import neo_api_client
    except ImportError as exc:
        raise RuntimeError(
            "neo-api-client not installed. Run: pip install neo-api-client"
        ) from exc

    all_kwargs = {
        "consumer_key": NEO_CONSUMER_KEY,
        "consumer_secret": NEO_CONSUMER_SECRET,
        "environment": NEO_ENVIRONMENT,
        "access_token": None,
        "neo_fin_key": None,
    }
    supported = inspect.signature(neo_api_client.NeoAPI.__init__).parameters
    client = neo_api_client.NeoAPI(**{k: v for k, v in all_kwargs.items() if k in supported})

    cached = _load_cached_token()
    if cached:
        client.access_token = cached["trading_token"]
        client.sid = cached["trading_sid"]
        client.base_url = cached["base_url"] or _DEFAULT_BASE_URL
        if not cached["base_url"]:
            logger.warning("Cached base_url is None — using default: %s", _DEFAULT_BASE_URL)
        return client

    # ── Validate access token ─────────────────────────────────────────────
    access_token = (NEO_ACCESS_TOKEN or "").strip()
    if not access_token:
        raise RuntimeError(
            "NEO_ACCESS_TOKEN is not set. Add it to your .env file.\n"
            "Get your token from: https://developer.kotaksecurities.com"
        )

    # ── Gather credentials ────────────────────────────────────────────────
    mobile = os.getenv("NEO_MOBILE", "").strip()
    ucc = (NEO_UCC or os.getenv("NEO_UCC", "")).strip()
    mpin = os.getenv("NEO_MPIN", "").strip()

    if not mobile:
        mobile = input("Registered mobile number (+91XXXXXXXXXX): ").strip()
    if not ucc:
        ucc = input("5-character client code (UCC): ").strip()

    # ── Step 2a: TOTP login (retry up to 3 times for expired codes) ──────
    view_token = view_sid = ""
    for attempt in range(1, 4):
        totp = input("TOTP from authenticator app: ").strip()
        logger.info("Step 2a: TOTP login… (attempt %d)", attempt)
        try:
            view_token, view_sid = _do_totp_login(mobile, ucc, totp, access_token)
            break
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 424 and attempt < 3:
                print("TOTP rejected (expired or invalid) — please enter the next code.")
                continue
            raise

    # ── Step 2b: MPIN validation (retry up to 3 times for wrong MPIN) ────
    for mpin_attempt in range(1, 4):
        if not mpin:
            mpin = input("6-digit MPIN: ").strip()
        logger.info("Step 2b: MPIN validation… (attempt %d)", mpin_attempt)
        try:
            trading_token, trading_sid, base_url = _do_mpin_validate(
                mpin, view_token, view_sid, access_token
            )
            break
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 424 and mpin_attempt < 3:
                print("MPIN rejected — please re-enter your 6-digit MPIN.")
                mpin = ""
                continue
            raise

    _save_token(trading_token, trading_sid, base_url)
    logger.info("Authentication successful. Trading session cached.")

    client.access_token = trading_token
    client.sid = trading_sid
    client.base_url = base_url
    return client


def refresh_if_needed(client) -> None:
    """Re-authenticate if token is nearing expiry. Call at the start of each trading day."""
    cached = _load_cached_token()
    if cached is None:
        logger.info("Token refresh required")
        new_client = get_neo_client()
        client.access_token = new_client.access_token
        client.sid = new_client.sid
        client.base_url = new_client.base_url
