import logging
import time
from decimal import ROUND_DOWN, Decimal

from kalshi.errors import KalshiConflictError

import alpha_engine
import config
import kalshi_client
import strategy
import trade_log

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

TICK = Decimal("0.01")


def _event(ticker: str) -> str:
    return ticker.rsplit("-", 1)[0]


def _score(client, markets) -> list[dict]:
    """Model + signal for each market; every evaluation is logged for calibration."""
    candidates = []
    for market in markets:
        ticker = market.ticker
        try:
            matchup = alpha_engine.build_matchup(ticker)
            true_prob = alpha_engine.calculate_true_probability(matchup)
            signal = strategy.evaluate(client, ticker, true_prob, market.start_time_unix)
        except alpha_engine.UnsupportedMatchup as e:
            log.debug("skip %s: %s", ticker, e)
            continue
        except Exception:
            log.exception("failed to evaluate %s", ticker)
            continue

        # Target is our side's probability rounded down to a tick, so it never exceeds fair value.
        side_prob = Decimal(str(true_prob)) if signal.side == "YES" else 1 - Decimal(str(true_prob))
        target = side_prob.quantize(TICK, rounding=ROUND_DOWN)
        buy = signal.action == "BUY" and signal.entry is not None and target > signal.entry
        log.info("%s p=%.3f bid=%s ask=%s ev=%s -> %s %s", ticker, true_prob, signal.bid, signal.ask, signal.ev, signal.action, signal.side)
        row = dict(
            ticker=ticker, event=_event(ticker), model_prob=f"{true_prob:.4f}", bid=signal.bid, ask=signal.ask,
            edge=signal.edge, ev=signal.ev, action="BUY" if buy else "HOLD", count=int(signal.count),
            target=target, dry_run=config.DRY_RUN, side=signal.side, entry=signal.entry,
        )
        trade_log.log_row(**row)
        if buy:
            candidates.append({"ticker": ticker, "signal": signal, "target": target, "row": row})
    return candidates


def run_once(client) -> None:
    markets = kalshi_client.get_target_markets(client)
    log.info("%d target markets (dry_run=%s)", len(markets), config.DRY_RUN)

    # One side per game (best EV), skipping games we already hold: never buy both teams.
    best: dict[str, dict] = {}
    for c in _score(client, markets):
        ev = _event(c["ticker"])
        if ev not in best or c["signal"].ev > best[ev]["signal"].ev:
            best[ev] = c
    held = trade_log.events_with_positions()
    deployed = Decimal(str(trade_log.deployed_capital()))

    for ev, c in sorted(best.items(), key=lambda kv: kv[1]["signal"].ev, reverse=True):
        ticker, signal, target = c["ticker"], c["signal"], c["target"]
        cost = signal.count * signal.entry
        if ev in held:
            log.info("%s skipped: already hold a position in %s", ticker, ev)
            continue
        if deployed + cost > config.MAX_TOTAL_EXPOSURE:
            log.info("%s skipped: total exposure cap %s reached", ticker, config.MAX_TOTAL_EXPOSURE)
            continue
        if config.DRY_RUN:
            log.info("DRY RUN: would buy %s %s x%s @ %s then sell @ %s", signal.side, ticker, int(signal.count), signal.entry, target)
            deployed += cost
            continue

        row = dict(c["row"])
        try:
            buy = kalshi_client.execute_fok_buy(client, ticker, signal.entry, signal.count, signal.side)
            row.update(buy_order_id=buy.order_id, filled=buy.fill_count)
            if buy.fill_count <= 0:
                row["note"] = "FOK not filled"
                log.info("%s FOK buy not filled", ticker)
            else:
                deployed += buy.fill_count * signal.entry
                held.add(ev)
                try:
                    sell = kalshi_client.place_resting_sell(
                        client, ticker, target, buy.fill_count, signal.start_time_unix, signal.side
                    )
                    row["sell_order_id"] = sell.order_id
                    log.info("%s bought %s %s @ %s; resting sell %s @ %s", ticker, buy.fill_count, signal.side, signal.entry, sell.order_id, target)
                except Exception:
                    row["note"] = "SELL FAILED - position unprotected"
                    log.exception("%s bought but resting sell failed", ticker)
        except KalshiConflictError as e:
            row["note"] = "FOK not filled" if "fill_or_kill" in str(e) else "order conflict"
            log.info("%s %s", ticker, row["note"])
        except Exception:
            row["note"] = "order error"
            log.exception("order flow failed for %s", ticker)
        trade_log.log_row(**row)


def run() -> None:
    client = kalshi_client.get_client()
    print("Connected to Kalshi Demo Environment")
    while True:
        try:
            run_once(client)
        except Exception:
            log.exception("cycle failed")
        if config.LOOP_INTERVAL_SECONDS <= 0:
            return
        time.sleep(config.LOOP_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
