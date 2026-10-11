import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from kalshi import KalshiClient, KalshiConfig
from kalshi.config import DEMO_WS_URL
from kalshi.models.orders import CreateOrderV2Request, CreateOrderV2Response

import alpha_engine
import config


def get_client() -> KalshiClient:
    return KalshiClient(
        key_id=config.KALSHI_KEY_ID,
        private_key_path=config.KALSHI_PRIVATE_KEY_PATH,
        config=KalshiConfig(base_url=config.KALSHI_BASE_URL, ws_base_url=DEMO_WS_URL, allow_unknown_host=True),
    )


@dataclass
class TargetMarket:
    ticker: str
    expiry: datetime
    volume_24h: Decimal
    start_time_unix: int | None = None  # scheduled kickoff, UNIX seconds


def _start_time_unix(ticker: str) -> int | None:
    try:
        kickoff = alpha_engine.kickoff_utc(alpha_engine.build_matchup(ticker))
    except alpha_engine.UnsupportedMatchup:
        return None
    return int(kickoff.timestamp()) if kickoff else None

def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iter_open_markets(client: KalshiClient, series: str):
    # Raw JSON: the demo host omits fields the SDK's Market model requires.
    cursor = None
    while True:
        params = {"status": "open", "series_ticker": series, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = client.markets._get("/markets", params=params)
        yield from data.get("markets", [])
        cursor = data.get("cursor")
        if not cursor:
            return


def get_target_markets(client: KalshiClient) -> list[TargetMarket]:
    """Open NFL markets expiring within 48h, sorted by 24h volume (desc)."""
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=config.EXPIRY_WINDOW_HOURS)
    found: dict[str, TargetMarket] = {}
    for series in config.SERIES_TICKERS:
        for m in _iter_open_markets(client, series):
            ticker = m["ticker"]
            if not any(league in ticker.upper() for league in config.LEAGUES):
                continue
            expiry = _parse_time(m.get("expected_expiration_time") or m["close_time"])
            if now <= expiry <= cutoff:
                volume = Decimal(m.get("volume_24h_fp") or m.get("volume_24h") or 0)
                found[ticker] = TargetMarket(ticker, expiry, volume, _start_time_unix(ticker))
    return sorted(found.values(), key=lambda m: m.volume_24h, reverse=True)


def execute_trade(
    client: KalshiClient,
    ticker: str,
    side: str,
    price: Decimal,
    count: Decimal,
    time_in_force: str = "fill_or_kill",
    expiration_time: int | None = None,
) -> CreateOrderV2Response:
    """Submit a limit order on the YES contract (fill_or_kill by default, so partial books cancel fully).

    side: 'bid' (buy) or 'ask' (sell).

    `price` is the dollar probability (e.g. Decimal('0.57')) used directly as the limit price.
    `expiration_time` (UNIX seconds) makes the exchange cancel the order if still resting then.
    """
    request = CreateOrderV2Request(
        ticker=ticker,
        client_order_id=str(uuid.uuid4()),
        side=side,
        count=count,
        price=price,
        time_in_force=time_in_force,
        self_trade_prevention_type="taker_at_cross",
        expiration_time=expiration_time,
    )
    return client.orders.create_v2(request=request)


def execute_fok_buy(
    client: KalshiClient, ticker: str, price: Decimal, count: Decimal, side: str = "YES"
) -> CreateOrderV2Response:
    """Buy YES at `price`; buy NO at `price` by selling YES at 1 - price."""
    if side == "NO":
        return execute_trade(client, ticker, "ask", Decimal(1) - price, count, "fill_or_kill")
    return execute_trade(client, ticker, "bid", price, count, "fill_or_kill")


def place_resting_sell(
    client: KalshiClient, ticker: str, price: Decimal, count: Decimal, start_time_unix: int, side: str = "YES"
) -> CreateOrderV2Response:
    """Resting GTC exit that the exchange auto-cancels at kickoff; the position then rides to settlement.

    `price` is on the side held; a NO exit is a YES bid at 1 - price.
    """
    if side == "NO":
        return execute_trade(
            client, ticker, "bid", Decimal(1) - price, count, "good_till_canceled", expiration_time=int(start_time_unix)
        )
    return execute_trade(client, ticker, "ask", price, count, "good_till_canceled", expiration_time=int(start_time_unix))
