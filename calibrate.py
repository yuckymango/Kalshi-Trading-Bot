import math
from collections import defaultdict

import kalshi_client
import trade_log


def _result(client, ticker: str) -> int | None:
    m = client.markets._get(f"/markets/{ticker}")
    m = m.get("market", m)
    return {"yes": 1, "no": 0}.get(m.get("result"))


def main() -> None:
    client = kalshi_client.get_client()
    first: dict[str, dict] = {}
    for r in trade_log.read_all_rows():
        first.setdefault(r["ticker"], r)  # earliest prediction per market

    scored = []
    for ticker, r in first.items():
        if not r["ask"] or not r["model_prob"]:
            continue
        outcome = _result(client, ticker)
        if outcome is not None:
            scored.append((float(r["model_prob"]), float(r["ask"]), outcome))
    if not scored:
        print("No settled markets logged yet.")
        return

    n = len(scored)
    brier_model = sum((p - y) ** 2 for p, _, y in scored) / n
    brier_market = sum((q - y) ** 2 for _, q, y in scored) / n
    logloss = -sum(math.log(max(1e-6, p if y else 1 - p)) for p, _, y in scored) / n
    print(f"settled={n}  brier_model={brier_model:.4f}  brier_market_ask={brier_market:.4f}  logloss={logloss:.4f}")

    bins = defaultdict(list)
    for p, _, y in scored:
        bins[min(int(p * 10), 9)].append((p, y))
    print("bin        n   mean_pred  actual_rate")
    for b in sorted(bins):
        v = bins[b]
        print(f"{b / 10:.1f}-{(b + 1) / 10:.1f}  {len(v):3d}  {sum(p for p, _ in v) / len(v):9.3f}  {sum(y for _, y in v) / len(v):11.3f}")


if __name__ == "__main__":
    main()
