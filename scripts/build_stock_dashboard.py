"""Build a per-stock drill-down dashboard for 00981A.

Generates docs/stock.html — a dark-themed single page that lets users
pick any stock 00981A has ever held and see:
  * 6 KPI cards (close, change%, shares, market value, weight, 累積報酬)
  * Price + estimated cost-basis line chart
  * Shares-held area chart
  * Market-value area chart
  * Daily detail table

URL pattern: docs/stock.html?stock=2330

Usage:
    python build_stock_dashboard.py
"""
import argparse
import bisect
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

from common import HOLDINGS_DB, PRICES_DB, ROOT


def load_all():
    ch = sqlite3.connect(HOLDINGS_DB)
    cp = sqlite3.connect(PRICES_DB)
    holdings = pd.read_sql(
        "SELECT trade_date, stock_code, stock_name, shares, market_value, weight_pct "
        "FROM holdings ORDER BY trade_date, stock_code",
        ch,
    )
    prices = pd.read_sql(
        'SELECT trade_date, ticker, close, "change" AS chg FROM prices',
        cp,
    )
    chips = pd.read_sql(
        "SELECT trade_date, stock_code, institution, net FROM chips",
        cp,
    )
    return holdings, prices, chips


def build_per_stock(stock_code, stock_name, h_df, p_df, c_df,
                    all_disclosure_dates):
    """Build daily series for one stock.

    Strategy:
      - x-axis = trading days from prices (for this stock), starting from
        first day we held the stock onward.
      - shares per day = look up in this stock's holdings; if missing on
        a disclosure date, treat as sold to 0.
      - cost basis = weighted-avg of close on share-increase days
        (selling does not change avg cost). First buy uses that day's
        close as the entry price.
    """
    if h_df.empty or p_df.empty:
        return None

    # build per-stock holdings lookup
    stock_holdings = dict(zip(
        h_df["trade_date"],
        zip(h_df["shares"], h_df["weight_pct"], h_df["market_value"]),
    ))
    first_held = h_df["trade_date"].min()

    def shares_at(d: str) -> tuple:
        """Return (shares, weight, mv) at end of date d. 0/None if not held."""
        # find latest disclosure date <= d
        idx = bisect.bisect_right(all_disclosure_dates, d) - 1
        if idx < 0:
            return (0, None, None)
        disc_d = all_disclosure_dates[idx]
        if disc_d in stock_holdings:
            s, w, mv = stock_holdings[disc_d]
            return (int(s), float(w), float(mv))
        return (0, None, None)

    # chip aggregation per date (合計三大法人)
    chip_lookup = {}
    if not c_df.empty:
        chip_pivot = (c_df.pivot_table(
            index="trade_date", columns="institution", values="net", aggfunc="sum",
        ).fillna(0))
        # storage is in shares; divide by 1000 → 張 (lots) for display
        for d, row in chip_pivot.iterrows():
            foreign = round((row.get("Foreign_Investor", 0)
                             + row.get("Foreign_Dealer_Self", 0)) / 1000)
            trust = round(row.get("Investment_Trust", 0) / 1000)
            dealer = round((row.get("Dealer_self", 0)
                            + row.get("Dealer_Hedging", 0)) / 1000)
            chip_lookup[d] = (int(foreign), int(trust), int(dealer))

    # x-axis: all trading days from prices, on/after first_held
    p_dates = sorted(p_df["trade_date"].tolist())
    p_dates = [d for d in p_dates if d >= first_held]
    if not p_dates:
        return None
    p_lookup = dict(zip(
        p_df["trade_date"],
        zip(p_df["close"], p_df["chg"]),
    ))

    rows = []
    last_shares = 0
    cost_basis = None
    peak_held = 0
    for d in p_dates:
        shares, weight, mv = shares_at(d)
        close, chg = p_lookup.get(d, (None, None))
        close = float(close) if pd.notna(close) else None
        chg = float(chg) if pd.notna(chg) else None

        # cost basis update on share increase
        if close is not None and shares > 0:
            if cost_basis is None or last_shares == 0:
                cost_basis = close
            elif shares > last_shares:
                delta = shares - last_shares
                cost_basis = (cost_basis * last_shares + close * delta) / shares
            # share decrease or unchanged: cost_basis unchanged
        if shares == 0:
            # if fully sold, retain last cost_basis for display but stop updating

            pass
        last_shares = shares
        peak_held = max(peak_held, shares)

        foreign, trust, dealer = chip_lookup.get(d, (None, None, None))

        rows.append({
            "d": d,
            "shares": shares,
            "weight": weight,
            "mv": mv,
            "close": close,
            "chg": chg,
            "cost": round(cost_basis, 2) if cost_basis else None,
            "foreign": foreign,
            "trust": trust,
            "dealer": dealer,
        })

    # build transactions = days where shares changed
    transactions = []
    prev_shares = 0
    for r in rows:
        s = r["shares"]
        if s == prev_shares:
            continue
        delta = s - prev_shares
        if prev_shares == 0 and s > 0:
            action = "建倉"
        elif s == 0 and prev_shares > 0:
            action = "清倉"
        elif delta > 0:
            action = "加碼"
        else:
            action = "減碼"
        # realised return only meaningful on sells (cost vs close at sell day)
        ret_pct = None
        if action in ("減碼", "清倉") and r["close"] and r["cost"]:
            ret_pct = round((r["close"] - r["cost"]) / r["cost"] * 100, 2)
        transactions.append({
            "d": r["d"],
            "action": action,
            "close": r["close"],
            "cost": r["cost"],
            "delta_lots": int(round(delta / 1000)),
            "cum_lots": int(round(s / 1000)),
            "ret_pct": ret_pct,
        })
        prev_shares = s

    # latest snapshot
    latest_held_row = next(
        (r for r in reversed(rows) if r["shares"] > 0),
        rows[-1],
    )

    return {
        "code": stock_code,
        "name": stock_name,
        "first_seen": p_dates[0],
        "last_seen": p_dates[-1],
        "peak_shares": peak_held,
        "rows": rows,
        "txns": transactions,
        "latest": latest_held_row,
        "still_held": rows[-1]["shares"] > 0,
    }


def build():
    holdings, prices, chips = load_all()
    if holdings.empty:
        raise RuntimeError("holdings table is empty")

    # cast all dates to ISO strings for stable comparison + JSON
    holdings["trade_date"] = holdings["trade_date"].astype(str).str[:10]
    prices["trade_date"] = prices["trade_date"].astype(str).str[:10]
    chips["trade_date"] = chips["trade_date"].astype(str).str[:10]

    latest_names = (holdings.sort_values("trade_date")
                    .groupby("stock_code")["stock_name"].last().to_dict())
    all_disclosure_dates = sorted(holdings["trade_date"].unique().tolist())
    last_disclosure = all_disclosure_dates[-1]

    stock_codes = sorted(holdings["stock_code"].unique())
    stocks = []
    for code in stock_codes:
        h = holdings[holdings["stock_code"] == code]
        p = prices[prices["ticker"] == code]
        c = chips[chips["stock_code"] == code]
        s = build_per_stock(code, latest_names[code], h, p, c,
                            all_disclosure_dates)
        if s is not None:
            stocks.append(s)

    # sort: still-held by current weight desc; then exited at bottom
    def sort_key(s):
        latest_w = s["latest"]["weight"] if s["still_held"] else None
        return (
            0 if s["still_held"] else 1,
            -(latest_w if latest_w else 0),
            s["code"],
        )
    stocks.sort(key=sort_key)

    # sidebar list
    sidebar = []
    for s in stocks:
        latest = s["latest"]
        sidebar.append({
            "code": s["code"],
            "name": s["name"],
            "weight": latest.get("weight"),
            "still_held": s["still_held"],
        })

    # data dict keyed by code (for JS lookup)
    data = {s["code"]: {
        "code": s["code"],
        "name": s["name"],
        "first_seen": s["first_seen"],
        "last_seen": s["last_seen"],
        "still_held": s["still_held"],
        "rows": s["rows"],
        "txns": s["txns"],
    } for s in stocks}

    out_path = ROOT / "docs" / "stock.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html = render_html(sidebar, data, last_disclosure)
    out_path.write_text(html)
    size_kb = out_path.stat().st_size / 1024
    print(f"Wrote {out_path} ({size_kb:,.0f} KB, {len(stocks)} stocks)")


def render_html(sidebar, data, latest_date):
    sidebar_json = json.dumps(sidebar, ensure_ascii=False)
    data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    generated = datetime.now().isoformat(timespec="seconds")

    return f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<title>00981A — 個股深度分析</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  :root {{
    --bg: #0a0e27;
    --bg-card: #161a3a;
    --bg-card-hi: #1f2547;
    --border: #2a3258;
    --text: #d0d5e8;
    --text-dim: #7a82a8;
    --text-strong: #ffffff;
    --accent: #4d7cfe;
    --green: #22c55e;
    --red: #ef4444;
    --purple: #a855f7;
    --teal: #14b8a6;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{
    margin: 0; padding: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, "Helvetica Neue", "PingFang TC", Arial, sans-serif;
    font-size: 13px;
  }}
  .layout {{ display: flex; min-height: 100vh; }}
  .sidebar {{
    width: 230px; background: var(--bg-card); border-right: 1px solid var(--border);
    flex-shrink: 0; overflow-y: auto; max-height: 100vh; position: sticky; top: 0;
  }}
  .sidebar-header {{
    padding: 14px 16px; font-weight: 600; color: var(--text-strong);
    border-bottom: 1px solid var(--border); font-size: 12px;
    text-transform: uppercase; letter-spacing: .8px;
  }}
  .sidebar-section {{
    padding: 8px 12px 4px; color: var(--text-dim); font-size: 10px;
    text-transform: uppercase; letter-spacing: .5px;
  }}
  .sb-item {{
    display: flex; justify-content: space-between; align-items: center;
    padding: 7px 14px; cursor: pointer; border-left: 3px solid transparent;
    color: var(--text); transition: background .12s;
  }}
  .sb-item:hover {{ background: var(--bg-card-hi); }}
  .sb-item.active {{
    background: var(--bg-card-hi); border-left-color: var(--accent);
    color: var(--text-strong);
  }}
  .sb-item.exited {{ color: var(--text-dim); }}
  .sb-code {{ font-weight: 600; font-size: 12px; min-width: 55px; }}
  .sb-name {{ flex: 1; padding: 0 6px; font-size: 11px;
              white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .sb-weight {{ font-size: 10px; color: var(--text-dim); }}

  .main {{ flex: 1; padding: 20px 28px; max-width: calc(100vw - 230px); }}
  .top-bar {{
    display: flex; align-items: baseline; justify-content: space-between;
    margin-bottom: 18px;
  }}
  .top-bar h1 {{
    margin: 0; font-size: 22px; color: var(--text-strong); font-weight: 600;
  }}
  .top-bar h1 .stock-code {{ color: var(--accent); margin-right: 8px; }}
  .top-bar .meta {{ color: var(--text-dim); font-size: 12px; }}
  .top-bar .meta a {{ color: var(--accent); text-decoration: none; margin-left: 12px; }}
  .top-bar .meta a:hover {{ text-decoration: underline; }}

  .kpi-row {{
    display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px;
    margin-bottom: 18px;
  }}
  .kpi {{
    background: var(--bg-card); border-radius: 8px; padding: 12px 14px;
    border: 1px solid var(--border);
  }}
  .kpi-label {{
    color: var(--text-dim); font-size: 11px; margin-bottom: 6px;
    text-transform: uppercase; letter-spacing: .5px;
  }}
  .kpi-value {{ color: var(--text-strong); font-size: 18px; font-weight: 600; }}
  .kpi-value.up {{ color: var(--green); }}
  .kpi-value.down {{ color: var(--red); }}

  .card {{
    background: var(--bg-card); border-radius: 8px; padding: 14px 16px;
    border: 1px solid var(--border); margin-bottom: 14px;
  }}
  .card h2 {{
    margin: 0 0 10px 0; font-size: 13px; color: var(--text-strong); font-weight: 600;
  }}
  .card .chart {{ width: 100%; }}

  table.detail {{
    width: 100%; border-collapse: collapse; font-size: 12px;
  }}
  table.detail th, table.detail td {{
    padding: 7px 10px; text-align: right;
    border-bottom: 1px solid var(--border);
  }}
  table.detail th {{
    color: var(--text-dim); font-weight: 500; text-transform: uppercase;
    font-size: 10px; letter-spacing: .5px; text-align: right;
    background: var(--bg-card-hi); position: sticky; top: 0;
  }}
  table.detail th:first-child, table.detail td:first-child {{ text-align: left; }}
  table.detail td.up {{ color: var(--green); }}
  table.detail td.down {{ color: var(--red); }}
  table.detail tbody tr:hover {{ background: var(--bg-card-hi); }}
  .table-wrap {{ max-height: 480px; overflow-y: auto; }}
  .pill {{
    display: inline-block; padding: 2px 10px; border-radius: 11px;
    font-size: 11px; font-weight: 600; color: #fff; min-width: 38px; text-align: center;
  }}
  .pill.buy {{ background: #16a34a; }}
  .pill.open {{ background: #0d9488; }}
  .pill.sell {{ background: #dc2626; }}
  .pill.close {{ background: #b91c1c; }}
  footer {{
    color: var(--text-dim); font-size: 11px; text-align: center;
    margin: 24px 0 12px;
  }}
  .empty {{
    background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px;
    padding: 40px; text-align: center; color: var(--text-dim);
  }}
</style>
</head>
<body>
<div class="layout">

<aside class="sidebar">
  <div class="sidebar-header">00981A 持股清單</div>
  <div id="sidebar-list"></div>
</aside>

<main class="main">
  <div class="top-bar">
    <h1 id="title">—</h1>
    <div class="meta">
      資料截止 {latest_date}
      <a href="index.html">← 回總覽</a>
    </div>
  </div>
  <div id="kpi-row" class="kpi-row"></div>
  <div class="card"><h2>股價與估算成本走勢</h2><div id="chart-price" class="chart"></div></div>
  <div class="card"><h2>基金持股股數</h2><div id="chart-shares" class="chart"></div></div>
  <div class="card"><h2>基金持股市值</h2><div id="chart-value" class="chart"></div></div>
  <div class="card"><h2>📋 歷史交易紀錄</h2><div class="table-wrap"><table class="detail" id="txns"></table></div></div>
  <div class="card"><h2>每日明細</h2><div class="table-wrap"><table class="detail" id="detail"></table></div></div>
  <footer>Generated {generated} · 成本基準為估算(以加碼日當日收盤加權平均) · Data: 統一投信 PCF API · Yahoo Finance · FinMind</footer>
</main>

</div>

<script>
const SIDEBAR = {sidebar_json};
const DATA = {data_json};

const COLORS = {{
  text: '#d0d5e8', textDim: '#7a82a8', grid: '#2a3258', bg: '#0a0e27',
  cardBg: '#161a3a', accent: '#4d7cfe', green: '#22c55e', red: '#ef4444',
  purple: '#a855f7', teal: '#14b8a6', yellow: '#fbbf24',
}};

const PLOT_LAYOUT = {{
  paper_bgcolor: COLORS.cardBg, plot_bgcolor: COLORS.cardBg,
  font: {{ color: COLORS.text, size: 11 }},
  margin: {{ l: 56, r: 24, t: 12, b: 36 }},
  height: 280, hovermode: 'x unified',
  xaxis: {{ gridcolor: COLORS.grid, color: COLORS.textDim, showspikes: true }},
  yaxis: {{ gridcolor: COLORS.grid, color: COLORS.textDim, zerolinecolor: COLORS.grid }},
  showlegend: true,
  legend: {{ orientation: 'h', y: -0.15, x: 0, font: {{ size: 11 }} }},
}};
const PLOT_CONFIG = {{ displayModeBar: false, responsive: true }};

function fmtInt(n) {{ return n == null ? '—' : Number(n).toLocaleString('en-US'); }}
function fmtKShares(n) {{ return n == null ? '—' : (n/1000).toFixed(0) + 'K'; }}
function fmtMoney(n) {{
  if (n == null) return '—';
  if (Math.abs(n) >= 1e9) return (n/1e9).toFixed(2) + 'B';
  if (Math.abs(n) >= 1e6) return (n/1e6).toFixed(1) + 'M';
  return Number(n).toLocaleString('en-US');
}}
function fmtPct(n, decimals=2) {{ return n == null ? '—' : n.toFixed(decimals) + '%'; }}
function fmtSigned(n) {{
  if (n == null) return '—';
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
}}
function classForChg(n) {{
  if (n == null) return '';
  return n > 0 ? 'up' : (n < 0 ? 'down' : '');
}}

function renderSidebar(currentCode) {{
  const heldEntries = SIDEBAR.filter(s => s.still_held);
  const exitedEntries = SIDEBAR.filter(s => !s.still_held);

  const wrap = document.getElementById('sidebar-list');
  let html = '<div class="sidebar-section">目前持有 (' + heldEntries.length + ')</div>';
  for (const s of heldEntries) {{
    const cls = s.code === currentCode ? 'active' : '';
    const w = s.weight != null ? s.weight.toFixed(2) + '%' : '';
    html += `<div class="sb-item ${{cls}}" data-code="${{s.code}}">
      <span class="sb-code">${{s.code}}</span>
      <span class="sb-name">${{s.name}}</span>
      <span class="sb-weight">${{w}}</span>
    </div>`;
  }}
  if (exitedEntries.length) {{
    html += '<div class="sidebar-section">已出清 (' + exitedEntries.length + ')</div>';
    for (const s of exitedEntries) {{
      const cls = s.code === currentCode ? 'active exited' : 'exited';
      html += `<div class="sb-item ${{cls}}" data-code="${{s.code}}">
        <span class="sb-code">${{s.code}}</span>
        <span class="sb-name">${{s.name}}</span>
        <span class="sb-weight">—</span>
      </div>`;
    }}
  }}
  wrap.innerHTML = html;
  wrap.querySelectorAll('.sb-item').forEach(el => {{
    el.addEventListener('click', () => switchStock(el.dataset.code));
  }});
}}

function switchStock(code) {{
  const url = new URL(window.location);
  url.searchParams.set('stock', code);
  window.history.pushState({{}}, '', url);
  render(code);
}}

function render(code) {{
  const stock = DATA[code];
  if (!stock) {{
    document.getElementById('title').innerHTML = '找不到股票 ' + code;
    document.getElementById('kpi-row').innerHTML =
      '<div class="empty" style="grid-column: 1/-1;">資料中沒有此股票代號</div>';
    return;
  }}

  document.getElementById('title').innerHTML =
    `<span class="stock-code">${{stock.code}}</span>${{stock.name}}` +
    (stock.still_held ? '' : ' <span style="color:var(--text-dim);font-size:14px;">(已出清)</span>');
  renderSidebar(code);

  // KPIs from latest row that had price
  const rows = stock.rows;
  const lastWithPrice = [...rows].reverse().find(r => r.close != null) || rows[rows.length-1];
  const latestHeld = [...rows].reverse().find(r => r.shares > 0) || rows[rows.length-1];
  const close = lastWithPrice.close;
  const chg = lastWithPrice.chg;
  const shares = latestHeld.shares;
  const mv = latestHeld.mv;
  const weight = latestHeld.weight;
  const cost = latestHeld.cost;
  const totalReturn = (close != null && cost) ? (close - cost) / cost * 100 : null;

  const kpis = [
    ['收盤價', close != null ? '$' + close.toFixed(2) : '—', ''],
    ['日漲跌', fmtSigned(chg), classForChg(chg)],
    ['持股股數', fmtInt(shares), ''],
    ['持股市值', mv != null ? '$' + fmtMoney(mv) : '—', ''],
    ['持股佔比', fmtPct(weight), ''],
    ['累積報酬(估)', fmtSigned(totalReturn), classForChg(totalReturn)],
  ];
  document.getElementById('kpi-row').innerHTML = kpis.map(([k, v, c]) =>
    `<div class="kpi"><div class="kpi-label">${{k}}</div><div class="kpi-value ${{c}}">${{v}}</div></div>`
  ).join('');

  const dates = rows.map(r => r.d);

  // Chart 1: price + cost basis + transaction markers
  const txns = stock.txns || [];
  function txnsOf(action) {{
    return txns.filter(t => t.action === action);
  }}
  function markerTrace(name, action, color, symbol, size) {{
    const items = txnsOf(action);
    return {{
      x: items.map(t => t.d), y: items.map(t => t.close), name: name,
      type: 'scatter', mode: 'markers',
      marker: {{ color: color, size: size, symbol: symbol,
                 line: {{ width: 1, color: '#0a0e27' }} }},
      customdata: items.map(t => [t.delta_lots, t.cum_lots, t.cost]),
      hovertemplate: '%{{x}}<br>' + name +
        ' %{{customdata[0]:+,d}} 張<br>' +
        '股價 $%{{y:.2f}} · 累積 %{{customdata[1]:,d}} 張<br>' +
        '成本 $%{{customdata[2]:.2f}}<extra></extra>',
    }};
  }}
  Plotly.newPlot('chart-price', [
    {{
      x: dates, y: rows.map(r => r.close), name: 'Price',
      type: 'scatter', mode: 'lines',
      line: {{ color: COLORS.accent, width: 2 }},
      hovertemplate: '%{{x}}<br>收盤: $%{{y:.2f}}<extra></extra>',
    }},
    markerTrace('加碼', '加碼', COLORS.green, 'circle', 8),
    markerTrace('建倉', '建倉', COLORS.green, 'circle-open', 13),
    {{
      x: dates, y: rows.map(r => r.cost), name: '成本',
      type: 'scatter', mode: 'lines',
      line: {{ color: COLORS.yellow, width: 1.6, dash: 'dot' }},
      hovertemplate: '%{{x}}<br>估算成本: $%{{y:.2f}}<extra></extra>',
    }},
    markerTrace('清倉', '清倉', '#b91c1c', 'circle-open', 13),
    markerTrace('減碼', '減碼', COLORS.red, 'circle', 8),
  ], PLOT_LAYOUT, PLOT_CONFIG);

  // Chart 2: shares (purple area)
  Plotly.newPlot('chart-shares', [
    {{
      x: dates, y: rows.map(r => r.shares), name: '持股股數',
      type: 'scatter', mode: 'lines', fill: 'tozeroy',
      line: {{ color: COLORS.purple, width: 1.6 }},
      fillcolor: 'rgba(168,85,247,0.22)',
      hovertemplate: '%{{x}}<br>%{{y:,.0f}} 股<extra></extra>',
    }},
  ], {{ ...PLOT_LAYOUT, showlegend: false }}, PLOT_CONFIG);

  // Chart 3: market value (teal area)
  Plotly.newPlot('chart-value', [
    {{
      x: dates, y: rows.map(r => r.mv), name: '持股市值',
      type: 'scatter', mode: 'lines', fill: 'tozeroy',
      line: {{ color: COLORS.teal, width: 1.6 }},
      fillcolor: 'rgba(20,184,166,0.20)',
      hovertemplate: '%{{x}}<br>$%{{y:,.0f}}<extra></extra>',
    }},
  ], {{ ...PLOT_LAYOUT, showlegend: false }}, PLOT_CONFIG);

  // Transactions table — only days where shares changed
  const txnTbl = document.getElementById('txns');
  if (txns.length === 0) {{
    txnTbl.innerHTML = '<tbody><tr><td style="text-align:center;color:var(--text-dim);padding:20px">尚無交易紀錄</td></tr></tbody>';
  }} else {{
    let txHtml = `<thead><tr>
      <th>日期</th><th style="text-align:center">操作</th>
      <th>股價</th><th>持股成本</th>
      <th>張數變化</th><th>累積張數</th><th>報酬率</th>
    </tr></thead><tbody>`;
    const pillCls = {{ '加碼': 'buy', '建倉': 'open', '減碼': 'sell', '清倉': 'close' }};
    for (const t of [...txns].reverse()) {{
      const deltaCls = t.delta_lots > 0 ? 'up' : (t.delta_lots < 0 ? 'down' : '');
      txHtml += `<tr>
        <td>${{t.d}}</td>
        <td style="text-align:center"><span class="pill ${{pillCls[t.action]}}">${{t.action}}</span></td>
        <td>${{t.close != null ? '$' + t.close.toFixed(2) : '—'}}</td>
        <td>${{t.cost != null ? '$' + t.cost.toFixed(2) : '—'}}</td>
        <td class="${{deltaCls}}">${{t.delta_lots > 0 ? '+' : ''}}${{fmtInt(t.delta_lots)}}</td>
        <td>${{fmtInt(t.cum_lots)}}</td>
        <td class="${{classForChg(t.ret_pct)}}">${{t.ret_pct != null ? fmtSigned(t.ret_pct) : '—'}}</td>
      </tr>`;
    }}
    txHtml += '</tbody>';
    txnTbl.innerHTML = txHtml;
  }}

  // Detail table
  const tbl = document.getElementById('detail');
  let html = `<thead><tr>
    <th>日期</th><th>收盤</th><th>漲跌幅</th>
    <th>持股股數</th><th>持股市值</th><th>佔比</th>
    <th>外資(張)</th><th>投信(張)</th><th>自營(張)</th>
  </tr></thead><tbody>`;
  for (const r of [...rows].reverse()) {{
    html += `<tr>
      <td>${{r.d}}</td>
      <td>${{r.close != null ? r.close.toFixed(2) : '—'}}</td>
      <td class="${{classForChg(r.chg)}}">${{fmtSigned(r.chg)}}</td>
      <td>${{fmtInt(r.shares)}}</td>
      <td>${{r.mv != null ? fmtMoney(r.mv) : '—'}}</td>
      <td>${{fmtPct(r.weight)}}</td>
      <td class="${{classForChg(r.foreign)}}">${{fmtInt(r.foreign)}}</td>
      <td class="${{classForChg(r.trust)}}">${{fmtInt(r.trust)}}</td>
      <td class="${{classForChg(r.dealer)}}">${{fmtInt(r.dealer)}}</td>
    </tr>`;
  }}
  html += '</tbody>';
  tbl.innerHTML = html;
}}

// init
const params = new URLSearchParams(window.location.search);
const initialCode = params.get('stock')
  || (SIDEBAR.find(s => s.still_held) || SIDEBAR[0] || {{}}).code;
if (initialCode) render(initialCode);

window.addEventListener('popstate', () => {{
  const c = new URLSearchParams(window.location.search).get('stock');
  if (c) render(c);
}});
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.parse_args()
    build()


if __name__ == "__main__":
    main()
