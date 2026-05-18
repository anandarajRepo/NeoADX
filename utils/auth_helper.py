"""
NeoADX — Kotak Neo authentication helper.

Handles the two-step login flow:
  Step 2a: POST /tradeApiLogin  — TOTP verification → VIEW_TOKEN + VIEW_SID
  Step 2b: POST /tradeApiValidate — MPIN validation  → TRADING_TOKEN + TRADING_SID + BASE_URL

Access token resolution (in order):
  1. NEO_ACCESS_TOKEN in .env (if set and not a placeholder)
  2. Auto-generated via OAuth using NEO_CONSUMER_KEY + NEO_CONSUMER_SECRET

Trading session values (NEO_TRADING_TOKEN, NEO_TRADING_SID, NEO_BASE_URL) are
persisted back into .env and reused for up to 20 hours.
"""

from __future__ import annotations

import inspect
import logging
import os
import re
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

_ENV_FILE = Path(".env")
_TOKEN_EXPIRY_HOURS = 20

_OAUTH_URL = "https://napi.kotaksecurities.com/oauth2/token"
_LOGIN_URL = "https://mis.kotaksecurities.com/login/1.0/tradeApiLogin"
_VALIDATE_URL = "https://mis.kotaksecurities.com/login/1.0/tradeApiValidate"
_DEFAULT_BASE_URL = "https://gw-napi.kotaksecurities.com"
_NEO_FIN_KEY = "neotradeapi"

_TRANSIENT_STATUS_CODES = {502, 503, 504}
_MAX_TRANSIENT_RETRIES = 4


# ---------------------------------------------------------------------------
# Token cache helpers — persisted in .env
# ---------------------------------------------------------------------------

def _load_cached_token() -> Optional[dict]:
    """Return cached session dict from .env if still valid, else None."""
    trading_token = os.getenv("NEO_TRADING_TOKEN", "").strip()
    trading_sid = os.getenv("NEO_TRADING_SID", "").strip()
    base_url = os.getenv("NEO_BASE_URL", "").strip()
    saved_at_str = os.getenv("NEO_TOKEN_SAVED_AT", "").strip()

    if not trading_token or not saved_at_str:
        return None
    try:
        saved_at = datetime.fromisoformat(saved_at_str)
        if datetime.now() - saved_at < timedelta(hours=_TOKEN_EXPIRY_HOURS):
            logger.info("Using cached Neo session (saved %s)", saved_at.strftime("%H:%M"))
            return {
                "trading_token": trading_token,
                "trading_sid": trading_sid,
                "base_url": base_url,
            }
        logger.info("Cached token expired — re-authenticating")
    except Exception as exc:
        logger.warning("Failed to read cached token from .env: %s", exc)
    return None


def _set_env_var(content: str, key: str, value: str) -> str:
    """Set or update a KEY=value line in .env file content."""
    pattern = re.compile(rf"^{re.escape(key)}\s*=.*$", re.MULTILINE)
    replacement = f"{key}={value}"
    if pattern.search(content):
        return pattern.sub(replacement, content)
    separator = "\n" if content and not content.endswith("\n") else ""
    return content + separator + replacement + "\n"


def _save_token(trading_token: str, trading_sid: str, base_url: str) -> None:
    """Persist trading session values into .env, overriding any previous values."""
    try:
        content = _ENV_FILE.read_text() if _ENV_FILE.exists() else ""
        content = _set_env_var(content, "NEO_TRADING_TOKEN", trading_token)
        content = _set_env_var(content, "NEO_TRADING_SID", trading_sid)
        content = _set_env_var(content, "NEO_BASE_URL", base_url)
        content = _set_env_var(content, "NEO_TOKEN_SAVED_AT", datetime.now().isoformat())
        _ENV_FILE.write_text(content)
        # Reflect changes in current process env so subsequent reads work
        os.environ["NEO_TRADING_TOKEN"] = trading_token
        os.environ["NEO_TRADING_SID"] = trading_sid
        os.environ["NEO_BASE_URL"] = base_url
        os.environ["NEO_TOKEN_SAVED_AT"] = datetime.now().isoformat()
        logger.info("Trading token saved to .env")
    except Exception as exc:
        logger.warning("Could not persist token to .env: %s", exc)


# ---------------------------------------------------------------------------
# OAuth access token generation
# ---------------------------------------------------------------------------

def _get_access_token() -> str:
    """Generate a fresh OAuth access token from consumer key + secret."""
    if not NEO_CONSUMER_KEY or not NEO_CONSUMER_SECRET:
        raise RuntimeError("NEO_CONSUMER_KEY and NEO_CONSUMER_SECRET must be set in .env")
    resp = requests.post(
        _OAUTH_URL,
        data={"grant_type": "client_credentials"},
        auth=(NEO_CONSUMER_KEY, NEO_CONSUMER_SECRET),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"OAuth token generation failed HTTP {resp.status_code}: {resp.text}")
    token = resp.json().get("access_token") or resp.json().get("token")
    if not token:
        raise RuntimeError(f"No access_token in OAuth response: {resp.text}")
    logger.info("OAuth access token generated successfully.")
    return token


def _resolve_access_token() -> str:
    """Return NEO_ACCESS_TOKEN from env, or auto-generate via OAuth if absent/placeholder."""
    raw = (NEO_ACCESS_TOKEN or "").strip()
    if raw and (raw.startswith("your_") or raw.endswith("_here")):
        logger.warning("NEO_ACCESS_TOKEN looks like a placeholder — generating token via OAuth instead.")
        raw = ""
    return raw or _get_access_token()


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

def get_neo_client(force: bool = False):
    """
    Return an authenticated Kotak Neo API client.

    Performs the two-step authentication (TOTP → MPIN) on first run or after
    token expiry, then caches the trading session for subsequent runs.

    Pass force=True to skip the cached token and re-authenticate unconditionally
    (used by the `auth` CLI command).

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

    cached = None if force else _load_cached_token()
    if cached:
        client.access_token = cached["trading_token"]
        client.sid = cached["trading_sid"]
        client.base_url = cached["base_url"] or _DEFAULT_BASE_URL
        if not cached["base_url"]:
            logger.warning("Cached base_url is None — using default: %s", _DEFAULT_BASE_URL)
        # Required for WebSocket subscribe() — SDK checks configuration.edit_token/edit_sid
        client.configuration.edit_token = cached["trading_token"]
        client.configuration.edit_sid = cached["trading_sid"]
        return client

    # ── Resolve access token (env override or auto-generate via OAuth) ────
    access_token = _resolve_access_token()

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
                body = exc.response.text
                if "does not exist" in body or "Consumer key" in body:
                    logger.error(
                        "Access token rejected by Kotak (HTTP 424): %s\n"
                        "Fix: remove NEO_ACCESS_TOKEN from .env so OAuth auto-generates it.",
                        body,
                    )
                    raise
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
    # Required for WebSocket subscribe() — SDK checks configuration.edit_token/edit_sid
    client.configuration.edit_token = trading_token
    client.configuration.edit_sid = trading_sid
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
