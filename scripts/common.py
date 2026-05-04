import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW_PCF = DATA / "raw_pcf"
LOGS = ROOT / "logs"
HOLDINGS_DB = DATA / "holdings.sqlite"
PRICES_DB = DATA / "prices.sqlite"

FUND_CODE_INTERNAL = "49YTW"
FUND_CODE_PUBLIC = "00981A"
FUND_LAUNCH_DATE = date(2025, 6, 2)


def iso_to_roc(d: date) -> str:
    return f"{d.year - 1911:03d}/{d.month:02d}/{d.day:02d}"


def parse_js_date(js_date: str) -> date | None:
    if not js_date:
        return None
    m = re.match(r"/Date\((\d+)\)/", js_date)
    if not m:
        return None
    return datetime.utcfromtimestamp(int(m.group(1)) / 1000).date()


def trading_days(start: date, end: date) -> list[date]:
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def init_holdings_db(path: Path = HOLDINGS_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS holdings (
        trade_date  TEXT NOT NULL,
        stock_code  TEXT NOT NULL,
        stock_name  TEXT NOT NULL,
        sequence    INTEGER,
        shares      INTEGER NOT NULL,
        market_value REAL NOT NULL,
        weight_pct  REAL NOT NULL,
        PRIMARY KEY (trade_date, stock_code)
    );
    CREATE INDEX IF NOT EXISTS idx_holdings_code ON holdings(stock_code);
    CREATE INDEX IF NOT EXISTS idx_holdings_date ON holdings(trade_date);

    CREATE TABLE IF NOT EXISTS fund_meta (
        trade_date    TEXT PRIMARY KEY,
        nav_total     REAL,
        units_total   REAL,
        units_change  REAL,
        nav_per_unit  REAL,
        beneficiaries INTEGER,
        stock_value_total REAL,
        fetched_at    TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS fetch_log (
        trade_date TEXT PRIMARY KEY,
        status     TEXT NOT NULL,   -- ok | empty | error
        n_stocks   INTEGER,
        message    TEXT,
        fetched_at TEXT NOT NULL
    );
    """)
    return conn


def init_prices_db(path: Path = PRICES_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS prices (
        trade_date TEXT NOT NULL,
        ticker     TEXT NOT NULL,
        open  REAL, high REAL, low REAL, close REAL,
        volume REAL,
        change REAL,
        PRIMARY KEY (trade_date, ticker)
    );
    CREATE INDEX IF NOT EXISTS idx_prices_ticker ON prices(ticker);

    CREATE TABLE IF NOT EXISTS chips (
        trade_date  TEXT NOT NULL,
        stock_code  TEXT NOT NULL,
        institution TEXT NOT NULL,
        buy         INTEGER,
        sell        INTEGER,
        net         INTEGER,
        PRIMARY KEY (trade_date, stock_code, institution)
    );
    CREATE INDEX IF NOT EXISTS idx_chips_code ON chips(stock_code);
    CREATE INDEX IF NOT EXISTS idx_chips_date ON chips(trade_date);
    """)
    return conn
