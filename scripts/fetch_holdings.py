"""Fetch 00981A daily PCF (持股明細) from 統一投信 and store to SQLite.

Usage:
    python fetch_holdings.py --backfill            # backfill since fund launch
    python fetch_holdings.py --date 2026-05-04     # single specific date
    python fetch_holdings.py --days 30             # last N trading days
"""
import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from common import (
    FUND_CODE_INTERNAL, FUND_LAUNCH_DATE, RAW_PCF, LOGS,
    init_holdings_db, iso_to_roc, parse_js_date, trading_days,
)

API_URL = "https://www.ezmoney.com.tw/ETF/Transaction/GetPCF"
INDEX_URL = "https://www.ezmoney.com.tw/ETF/Transaction/PCF"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Content-Type": "application/json; charset=utf-8",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": INDEX_URL,
    "Origin": "https://www.ezmoney.com.tw",
}


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    s.get(INDEX_URL, timeout=20)
    return s


def fetch_pcf(session: requests.Session, d: date) -> dict:
    payload = {
        "fundCode": FUND_CODE_INTERNAL,
        "date": iso_to_roc(d),
        "specificDate": True,
    }
    r = session.post(API_URL, data=json.dumps(payload), timeout=30)
    r.raise_for_status()
    return r.json()


def extract_holdings(payload: dict) -> tuple[list[dict], dict]:
    stock_asset = next(
        (a for a in payload.get("asset", []) if a.get("AssetCode") == "ST"),
        None,
    )
    holdings = []
    if stock_asset and stock_asset.get("Details"):
        tran_date = parse_js_date(stock_asset["Details"][0].get("TranDate"))
        for d in stock_asset["Details"]:
            holdings.append({
                "trade_date": tran_date.isoformat() if tran_date else None,
                "stock_code": (d.get("DetailCode") or "").strip(),
                "stock_name": (d.get("DetailName") or "").strip(),
                "sequence": d.get("Sequence"),
                "shares": int(d.get("Share") or 0),
                "market_value": float(d.get("Amount") or 0),
                "weight_pct": float(d.get("NavRate") or 0),
            })

    pcf_map = {p.get("PCFName"): p.get("Amount") for p in payload.get("pcf", [])}
    meta = {
        "nav_total": pcf_map.get("基金淨資產價值(元)"),
        "units_total": pcf_map.get("已發行受益權單位總數"),
        "units_change": pcf_map.get("與前日已發行單位差異數"),
        "nav_per_unit": pcf_map.get("每受益權單位淨資產價值(元)"),
        "beneficiaries": int(pcf_map.get("受益人數") or 0) or None,
        "stock_value_total": stock_asset.get("Value") if stock_asset else None,
    }
    return holdings, meta


def store(conn, target_date: date, holdings: list[dict], meta: dict):
    now = datetime.now().isoformat(timespec="seconds")
    iso = target_date.isoformat()

    if not holdings:
        conn.execute(
            "INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?,?)",
            (iso, "empty", 0, "no holdings (non-trading day or pre-launch)", now),
        )
        conn.commit()
        return 0

    actual_date = holdings[0]["trade_date"] or iso
    conn.execute("DELETE FROM holdings WHERE trade_date = ?", (actual_date,))
    conn.executemany(
        """INSERT INTO holdings
           (trade_date, stock_code, stock_name, sequence, shares, market_value, weight_pct)
           VALUES (:trade_date,:stock_code,:stock_name,:sequence,:shares,:market_value,:weight_pct)""",
        holdings,
    )
    conn.execute(
        """INSERT OR REPLACE INTO fund_meta
           (trade_date, nav_total, units_total, units_change, nav_per_unit,
            beneficiaries, stock_value_total, fetched_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (actual_date, meta["nav_total"], meta["units_total"], meta["units_change"],
         meta["nav_per_unit"], meta["beneficiaries"], meta["stock_value_total"], now),
    )
    conn.execute(
        "INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?,?)",
        (actual_date, "ok", len(holdings), None, now),
    )
    conn.commit()
    return len(holdings)


def already_fetched(conn, d: date) -> bool:
    iso = d.isoformat()
    row = conn.execute(
        "SELECT status FROM fetch_log WHERE trade_date = ?", (iso,)
    ).fetchone()
    return row is not None and row[0] in ("ok", "empty")


def run(target_dates: list[date], force: bool = False):
    RAW_PCF.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)

    conn = init_holdings_db()
    session = make_session()

    n_total = len(target_dates)
    n_ok = n_empty = n_err = n_skip = 0

    for i, d in enumerate(target_dates, 1):
        if not force and already_fetched(conn, d):
            n_skip += 1
            continue
        try:
            payload = fetch_pcf(session, d)
            raw_path = RAW_PCF / f"{d.isoformat()}.json"
            raw_path.write_text(json.dumps(payload, ensure_ascii=False))
            holdings, meta = extract_holdings(payload)
            n = store(conn, d, holdings, meta)
            tag = "ok" if n else "empty"
            if n:
                n_ok += 1
            else:
                n_empty += 1
            print(f"[{i:>3}/{n_total}] {d} {tag} stocks={n}")
        except Exception as e:
            n_err += 1
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                "INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?,?)",
                (d.isoformat(), "error", 0, str(e), now),
            )
            conn.commit()
            print(f"[{i:>3}/{n_total}] {d} ERROR {e}", file=sys.stderr)
        time.sleep(0.4)

    print(f"\nDone. ok={n_ok} empty={n_empty} skip={n_skip} err={n_err}")


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--backfill", action="store_true",
                   help="Fetch since fund launch up to today")
    g.add_argument("--date", type=str, help="Single date YYYY-MM-DD")
    g.add_argument("--days", type=int, help="Last N trading days")
    ap.add_argument("--force", action="store_true",
                    help="Re-fetch even if already in fetch_log")
    args = ap.parse_args()

    today = date.today()
    if args.backfill:
        targets = trading_days(FUND_LAUNCH_DATE, today)
    elif args.date:
        targets = [date.fromisoformat(args.date)]
    else:
        targets = trading_days(today - timedelta(days=args.days * 2), today)[-args.days:]

    run(targets, force=args.force)


if __name__ == "__main__":
    main()
