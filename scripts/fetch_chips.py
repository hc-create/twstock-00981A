"""Fetch institutional investors (三大法人, focus on 投信) buy/sell for every
stock that has ever appeared in 00981A's holdings. Source: FinMind.

Usage:
    python fetch_chips.py                              # full universe, since launch
    python fetch_chips.py --start 2026-04-01 --end 2026-05-04
"""
import argparse
import sqlite3
import sys
import time
from datetime import date

import requests

from common import HOLDINGS_DB, FUND_LAUNCH_DATE, init_prices_db


API = "https://api.finmindtrade.com/api/v4/data"


def fetch_one(stock_id: str, start: date, end: date) -> list[dict]:
    params = {
        "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
        "data_id": stock_id,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    r = requests.get(API, params=params, timeout=30)
    r.raise_for_status()
    j = r.json()
    if j.get("status") != 200:
        raise RuntimeError(f"FinMind error: {j}")
    return j.get("data", [])


def store(conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    payload = []
    for r in rows:
        buy = int(r.get("buy") or 0)
        sell = int(r.get("sell") or 0)
        payload.append((
            r["date"], r["stock_id"], r["name"], buy, sell, buy - sell,
        ))
    conn.executemany(
        """INSERT OR REPLACE INTO chips
           (trade_date, stock_code, institution, buy, sell, net)
           VALUES (?,?,?,?,?,?)""",
        payload,
    )
    conn.commit()
    return len(payload)


def run(start: date, end: date):
    conn_h = sqlite3.connect(HOLDINGS_DB)
    conn_p = init_prices_db()
    universe = [r[0] for r in conn_h.execute(
        "SELECT DISTINCT stock_code FROM holdings ORDER BY stock_code"
    )]
    print(f"Universe: {len(universe)} stocks, {start} → {end}")

    n_ok = n_empty = n_err = 0
    for i, code in enumerate(universe, 1):
        try:
            data = fetch_one(code, start, end)
            n = store(conn_p, data)
            tag = "ok" if n else "empty"
            (n_ok if n else n_empty)
            if n:
                n_ok += 1
            else:
                n_empty += 1
            sys.stdout.write(f"\r  [{i:>3}/{len(universe)}] {code} rows={n:>4} {tag}    ")
            sys.stdout.flush()
        except Exception as e:
            n_err += 1
            print(f"\n  [{i}] {code} ERROR: {e}", file=sys.stderr)
        time.sleep(0.6)
    print()
    print(f"Done. ok={n_ok} empty={n_empty} err={n_err}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=str, default=FUND_LAUNCH_DATE.isoformat())
    ap.add_argument("--end", type=str, default=date.today().isoformat())
    args = ap.parse_args()
    run(date.fromisoformat(args.start), date.fromisoformat(args.end))


if __name__ == "__main__":
    main()
