import csv
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
SIGNALS = DATA_DIR / "signals.csv"
FIELDS = [
    "ts", "ticker", "event", "model_prob", "bid", "ask", "edge", "ev",
    "action", "count", "target", "dry_run", "buy_order_id", "filled", "sell_order_id", "note", "side", "entry",
]


def log_row(**row) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    new = not SIGNALS.exists()
    row["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with SIGNALS.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, restval="")
        if new:
            w.writeheader()
        w.writerow(row)


def read_rows() -> list[dict]:
    if not SIGNALS.exists():
        return []
    with SIGNALS.open(newline="") as f:
        return list(csv.DictReader(f))


def read_all_rows() -> list[dict]:
    """Rows from every run, including archived signals_*.csv, for calibration."""
    rows = []
    for path in sorted(DATA_DIR.glob("signals*.csv")):
        with path.open(newline="") as f:
            rows.extend(csv.DictReader(f))
    return rows


def events_with_positions() -> set[str]:
    """Events where a live (non-dry-run) buy has filled."""
    return {r["event"] for r in read_rows() if r["dry_run"] == "False" and r["filled"] not in ("", "0", "0.00")}


def deployed_capital() -> float:
    """Capital spent on live buys recorded in the log (ignores settlements)."""
    return sum(
        float(r["filled"]) * float(r.get("entry") or r["ask"])
        for r in read_rows() if r["dry_run"] == "False" and r["filled"]
    )
