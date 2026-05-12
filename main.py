"""
NeoADX — entry point

Commands:
    python main.py auth                  # authenticate and cache session token
    python main.py run                   # run strategy (dry-run by default)
    python main.py run --live            # run strategy in live-trading mode
    python main.py run --dry-run         # run strategy in dry-run mode

Run `auth` once before market open to complete the interactive TOTP + MPIN
flow and persist the session token.  `run` will reuse the cached token and
start immediately without any prompts.
"""

import argparse
import logging
import sys

from strategy.adx_crossover import ADXCrossoverStrategy

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("neoadx.log"),
    ],
)
logger = logging.getLogger("main")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="NeoADX — ADX DI+/DI- Crossover strategy for Nifty 50 weekly options"
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.required = True

    # ── auth ──────────────────────────────────────────────────────────────────
    subparsers.add_parser(
        "auth",
        help="Authenticate with Kotak Neo and cache the session token",
    )

    # ── run ───────────────────────────────────────────────────────────────────
    run_parser = subparsers.add_parser(
        "run",
        help="Run the ADX crossover strategy",
    )
    mode_group = run_parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--live",
        action="store_true",
        help="Enable live order placement (overrides LIVE_TRADE in .env)",
    )
    mode_group.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Force dry-run mode — log signals only, no real orders",
    )

    return parser.parse_args()


def cmd_auth() -> None:
    """Authenticate with Kotak Neo and persist the session token."""
    from broker.kotak_neo import KotakNeoClient
    client = KotakNeoClient()
    client.connect()
    logger.info("Authentication successful — session token cached.")


def cmd_run(live_trade: "bool | None") -> None:
    """Run the ADX crossover strategy."""
    strategy = ADXCrossoverStrategy(live_trade=live_trade)
    logger.info("NeoADX starting | live_trade=%s", strategy.live_trade)

    try:
        strategy.run()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received — squaring off open positions…")
        strategy._squareoff_all(reason="Manual interrupt")
    finally:
        summary = strategy.summary()
        if not summary.empty:
            logger.info("\n%s", summary.to_string(index=False))
        else:
            logger.info("No trades executed today.")


def main() -> None:
    args = parse_args()

    if args.command == "auth":
        cmd_auth()

    elif args.command == "run":
        if args.live:
            live_trade: "bool | None" = True
        elif args.dry_run:
            live_trade = False
        else:
            live_trade = None   # fall back to LIVE_TRADE in config/settings.py / .env
        cmd_run(live_trade)


if __name__ == "__main__":
    main()
