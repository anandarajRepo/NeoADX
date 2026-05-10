"""
NeoADX — entry point
Usage:
    python main.py                     # dry-run (reads LIVE_TRADE from .env)
    python main.py --live              # enable live trading
    python main.py --dry-run           # force dry-run regardless of .env
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
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--live",
        action="store_true",
        help="Enable live order placement (overrides LIVE_TRADE in .env)",
    )
    group.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Force dry-run mode — log signals only, no real orders",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Determine effective live_trade flag
    if args.live:
        live_trade: bool | None = True
    elif args.dry_run:
        live_trade = False
    else:
        live_trade = None   # fall back to config/settings.py / .env

    strategy = ADXCrossoverStrategy(live_trade=live_trade)
    logger.info(
        "NeoADX starting | live_trade=%s",
        strategy.live_trade,
    )

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


if __name__ == "__main__":
    main()
