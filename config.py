import os
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name} (set it in .env)")
    return value


KALSHI_KEY_ID = _required("KALSHI_KEY_ID")
KALSHI_PRIVATE_KEY_PATH = _required("KALSHI_PRIVATE_KEY_PATH")
if not Path(KALSHI_PRIVATE_KEY_PATH).is_file():
    raise RuntimeError("KALSHI_PRIVATE_KEY_PATH does not point to an existing file")

KALSHI_BASE_URL = "https://external-api.demo.kalshi.co/trade-api/v2"

BANKROLL = Decimal("1000")
MAX_RISK_PER_TRADE = Decimal("100")  # hard cap per position (10% of bankroll)
MIN_RISK_PER_TRADE = Decimal("5")  # skip trades whose edge-based size is below this
KELLY_FRACTION = Decimal("0.25")  # fraction of full Kelly staked; lower = smaller, safer positions
MAX_TOTAL_EXPOSURE = Decimal("500")  # cap on cumulative capital deployed, per the trade log
LOOP_INTERVAL_SECONDS = int(os.getenv("LOOP_INTERVAL_SECONDS", "0"))  # 0 = run once
# No orders are submitted unless DRY_RUN=false.
DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"

LEAGUES = ("NFL",)
SERIES_TICKERS = ("KXNFLGAME",)
EXPIRY_WINDOW_HOURS = 48

MIN_EDGE = Decimal(os.getenv("MIN_EDGE", "0"))  # extra buffer on top of the bid/ask spread
