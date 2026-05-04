"""Fetch daily prices for TAIEX, 00981A, and every stock that has ever
appeared in the ETF holdings. Stores into prices.sqlite.

Usage:
    python fetch_prices.py                  # default: covers full holdings range
    python fetch_prices.py --start 2026-04-01 --end 2026-05-04
"""
import argparse
import sqlite3
import sys
import time
from datetime import date, timedelta

import pandas as pd
import yfinance as yf

from common import HOLDINGS_DB, init_prices_db, FUND_LAUNCH_DATE


SPECIAL = {
    "TAIEX":  "^TWII",
    "00981A": "00981A.TW",
}


def get_universe(conn_holdings) -> list[str]:
    rows = conn_holdings.execute(
        "SELECT DISTINCT stock_code FROM holdings ORDER BY stock_code"
    ).fetchall()
    return [r[0] for r in rows]


def yf_download(ticker: str, start: date, end: date) -> pd.DataFrame:
    df = yf.download(
        ticker, start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        progress=False, auto_adjust=False, threads=False,
    )
    if df is None or df.empty:
        return pd.DataFrame()
    if hasattr(df.columns, "get_level_values"):
        df.columns = df.columns.get_level_values(0)
    return df


def fetch_one_stock(code: str, start: date, end: date) -> tuple[str, pd.DataFrame]:
    for suffix in (".TW", ".TWO"):
        df = yf_download(code + suffix, start, end)
        if not df.empty:
            return suffix, df
    return "", pd.DataFrame()


def store(conn, ticker: str, df: pd.DataFrame):
    if df.empty:
        return 0
    rows = []
    for ts, r in df.iterrows():
        try:
            close = float(r["Close"])
        except Exception:
            continue
        if pd.isna(close):
            continue
        prev = None
        rows.append((
            ts.date().isoformat(), ticker,
            float(r["Open"]) if not pd.isna(r.get("Open", float("nan"))) else None,
            float(r["High"]) if not pd.isna(r.get("High", float("nan"))) else None,
            float(r["Low"]) if not pd.isna(r.get("Low", float("nan"))) else None,
            close,
            float(r["Volume"]) if not pd.isna(r.get("Volume", float("nan"))) else None,
            None,
        ))
    if not rows:
        return 0
    conn.executemany(
        """INSERT OR REPLACE INTO prices
           (trade_date, ticker, open, high, low, close, volume, change)
           VALUES (?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()
    return len(rows)


def add_change_pct(conn):
    conn.execute("""
    UPDATE prices
       SET change = ROUND(
           (close - (
               SELECT prev.close FROM prices prev
                WHERE prev.ticker = prices.ticker
                  AND prev.trade_date < prices.trade_date
                ORDER BY prev.trade_date DESC LIMIT 1
           )) / (
               SELECT prev.close FROM prices prev
                WHERE prev.ticker = prices.ticker
                  AND prev.trade_date < prices.trade_date
                ORDER BY prev.trade_date DESC LIMIT 1
           ) * 100, 4)
     WHERE EXISTS (
         SELECT 1 FROM prices prev
          WHERE prev.ticker = prices.ticker
            AND prev.trade_date < prices.trade_date
     );
    """)
    conn.commit()


def run(start: date, end: date):
    conn_h = sqlite3.connect(HOLDINGS_DB)
    conn_p = init_prices_db()

    universe = get_universe(conn_h)
    print(f"Universe: TAIEX, 00981A, + {len(universe)} component stocks")

    n_ok = n_skip = 0

    for label, ticker in SPECIAL.items():
        df = yf_download(ticker, start, end)
        n = store(conn_p, label, df)
        print(f"  [{label:8s}] {ticker:14s} rows={n}")
        n_ok += 1 if n else 0
        time.sleep(0.2)

    for i, code in enumerate(universe, 1):
        suffix, df = fetch_one_stock(code, start, end)
        n = store(conn_p, code, df) if not df.empty else 0
        if n:
            n_ok += 1
        else:
            n_skip += 1
        sys.stdout.write(f"\r  [{i:>3}/{len(universe)}] {code}{suffix:4s} rows={n}    ")
        sys.stdout.flush()
        time.sleep(0.2)
    print()

    print("Computing day-over-day change...")
    add_change_pct(conn_p)
    print(f"Done. ok={n_ok} skip={n_skip}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=str, default=FUND_LAUNCH_DATE.isoformat())
    ap.add_argument("--end", type=str, default=date.today().isoformat())
    args = ap.parse_args()
    run(date.fromisoformat(args.start), date.fromisoformat(args.end))


if __name__ == "__main__":
    main()
