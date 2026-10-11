import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from kalshi import KalshiClient

import config

FEE_RATE = Decimal("0.07")  # Kalshi taker fee approx. 0.07 * p * (1 - p) per contract


@dataclass
class Signal:
    action: str  # "BUY" or "HOLD"
    bid: Decimal | None
    ask: Decimal | None
    spread: Decimal | None
    edge: Decimal | None
    ev: Decimal | None = None  # expected profit per contract after fees
    count: Decimal = Decimal(0)  # contracts to buy, sized to MAX_RISK_PER_TRADE
    start_time_unix: int | None = None  # kickoff; the resting sell expires then
    side: str = "YES"  # contract bought: "YES" or "NO"
    entry: Decimal | None = None  # price paid per contract on the side bought


def size_position(entry: Decimal, edge: Decimal) -> Decimal:
    """Whole contracts from fractional Kelly: stake = BANKROLL * KELLY_FRACTION * edge / (1 - entry).

    `edge` is net of spread, so sizing is conservative. Capped at MAX_RISK_PER_TRADE; 0 if below MIN_RISK_PER_TRADE.
    """
    if edge <= 0 or entry <= 0 or entry >= 1:
        return Decimal(0)
    stake = min(config.BANKROLL * config.KELLY_FRACTION * edge / (1 - entry), config.MAX_RISK_PER_TRADE)
    if stake < config.MIN_RISK_PER_TRADE:
        return Decimal(0)
    return (stake / entry).to_integral_value(rounding=ROUND_DOWN)


def get_bid_ask(client: KalshiClient, ticker: str) -> tuple[Decimal | None, Decimal | None, Decimal, Decimal]:
    """Best YES bid, ask (dollars = implied probability) and contracts resting at the ask and bid.

    YES ask = 1 - best NO bid; a buy at the ask can only fill against that NO level.
    Buying NO at 1 - bid fills against the best YES bid level.
    """
    book = client.markets.orderbook(ticker)
    best_yes = max(book.yes, key=lambda lvl: lvl.price, default=None)
    best_no = max(book.no, key=lambda lvl: lvl.price, default=None)
    bid = best_yes.price if best_yes else None
    bid_size = best_yes.quantity if best_yes else Decimal(0)
    if best_no is None:
        return bid, None, Decimal(0), bid_size
    return bid, Decimal(1) - best_no.price, best_no.quantity, bid_size


def evaluate(client: KalshiClient, ticker: str, true_prob: float, start_time_unix: int | None = None) -> Signal:
    """BUY YES if true_prob > 0.5 and > ask + spread; BUY NO if true_prob < 0.5 and 1 - true_prob > NO ask + spread.

    Positions are held through kickoff, so we only back the side the model favors (> 50%).
    HOLD if kickoff is unknown or past: the sell's expiration_time couldn't be set.
    """
    bid, ask, ask_size, bid_size = get_bid_ask(client, ticker)
    if bid is None or ask is None or ask <= bid - Decimal("0.0001"):
        return Signal("HOLD", bid, ask, None, None, start_time_unix=start_time_unix)
    spread = (ask - bid) + config.MIN_EDGE
    p = Decimal(str(true_prob))
    live = start_time_unix is not None and start_time_unix > time.time()

    if p < Decimal("0.5"):
        side, prob, entry, depth = "NO", 1 - p, 1 - bid, bid_size
    else:
        side, prob, entry, depth = "YES", p, ask, ask_size
    edge = prob - (entry + spread)
    fee = (FEE_RATE * entry * (1 - entry)).quantize(Decimal("0.0001"))
    ev = prob * (1 - entry) - (1 - prob) * entry - fee
    # Shrink to the depth at the entry price so the fill-or-kill order can fill completely.
    count = min(size_position(entry, edge), depth.to_integral_value(rounding=ROUND_DOWN))
    buy = prob > Decimal("0.5") and edge > 0 and ev > 0 and count >= 1 and live
    return Signal("BUY" if buy else "HOLD", bid, ask, spread, edge, ev, count, start_time_unix, side, entry)
