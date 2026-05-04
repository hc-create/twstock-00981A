"""Build a self-contained interactive HTML dashboard for 00981A.

Usage:
    python build_dashboard.py                # full history
    python build_dashboard.py --window 60    # focus past 60 calendar days
"""
import argparse
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from common import HOLDINGS_DB, PRICES_DB, ROOT


PLOTLY_CDN_FIRST = True  # only first chart embeds plotly.js


def load(window_days: int):
    ch = sqlite3.connect(HOLDINGS_DB)
    cp = sqlite3.connect(PRICES_DB)

    holdings = pd.read_sql(
        "SELECT trade_date, stock_code, stock_name, shares, market_value, weight_pct "
        "FROM holdings", ch, parse_dates=["trade_date"],
    )
    fund_meta = pd.read_sql(
        "SELECT trade_date, nav_total, units_total, units_change, "
        "nav_per_unit, beneficiaries FROM fund_meta",
        ch, parse_dates=["trade_date"],
    )
    prices = pd.read_sql(
        'SELECT trade_date, ticker, close, "change" AS chg FROM prices',
        cp, parse_dates=["trade_date"],
    )
    chips = pd.read_sql(
        "SELECT trade_date, stock_code, institution, net "
        "FROM chips WHERE institution = 'Investment_Trust'",
        cp, parse_dates=["trade_date"],
    )
    return holdings, fund_meta, prices, chips


def latest_top20(holdings: pd.DataFrame) -> pd.DataFrame:
    latest = holdings["trade_date"].max()
    df = holdings[holdings["trade_date"] == latest].copy()
    return df.sort_values("weight_pct", ascending=False).head(20).reset_index(drop=True)


def fig_fund_scale(fund_meta: pd.DataFrame) -> go.Figure:
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(
        go.Scatter(
            x=fund_meta["trade_date"], y=fund_meta["nav_total"] / 1e8,
            name="基金規模(億)", line=dict(width=2.5, color="#1f77b4"),
            hovertemplate="%{x|%Y-%m-%d}<br>規模: %{y:,.0f} 億<extra></extra>",
        ),
        secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(
            x=fund_meta["trade_date"], y=fund_meta["beneficiaries"],
            name="受益人數", line=dict(width=1.8, color="#ff7f0e", dash="dot"),
            hovertemplate="%{x|%Y-%m-%d}<br>受益人: %{y:,.0f}<extra></extra>",
        ),
        secondary_y=True,
    )
    fig.update_yaxes(title_text="基金規模(億)", secondary_y=False)
    fig.update_yaxes(title_text="受益人數", secondary_y=True)
    fig.update_layout(
        title="基金規模演進", hovermode="x unified", height=420,
        margin=dict(l=50, r=50, t=50, b=40),
    )
    return fig


def fig_etf_vs_index(prices: pd.DataFrame, window_days: int) -> go.Figure:
    end = prices["trade_date"].max()
    start = end - pd.Timedelta(days=window_days * 3)
    sub = prices[(prices["ticker"].isin(["00981A", "TAIEX"]))
                 & (prices["trade_date"] >= start)].copy()
    pivot = sub.pivot(index="trade_date", columns="ticker", values="close").dropna()
    pivot = pivot / pivot.iloc[0] * 100

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=pivot.index, y=pivot["00981A"], name="00981A",
        line=dict(width=2.5, color="#d62728"),
        hovertemplate="%{x|%Y-%m-%d}<br>00981A: %{y:.2f}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=pivot.index, y=pivot["TAIEX"], name="加權指數",
        line=dict(width=2, color="#2ca02c"),
        hovertemplate="%{x|%Y-%m-%d}<br>TAIEX: %{y:.2f}<extra></extra>",
    ))
    fig.update_layout(
        title=f"00981A vs 加權指數(基期=100,起點 {pivot.index[0].date()})",
        hovermode="x unified", height=420, yaxis_title="rebased",
        margin=dict(l=50, r=20, t=50, b=40),
    )
    return fig


def fig_premium(prices: pd.DataFrame, fund_meta: pd.DataFrame) -> go.Figure:
    fm = fund_meta[["trade_date", "nav_per_unit"]].dropna().sort_values("trade_date")
    etf = prices[prices["ticker"] == "00981A"][["trade_date", "close"]]
    merged = pd.merge_asof(
        etf.sort_values("trade_date"), fm,
        on="trade_date", direction="backward",
    ).dropna()
    merged["premium_pct"] = (merged["close"] - merged["nav_per_unit"]) / merged["nav_per_unit"] * 100

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=merged["trade_date"], y=merged["premium_pct"],
        name="折溢價%",
        marker_color=["#d62728" if v < 0 else "#2ca02c" for v in merged["premium_pct"]],
        hovertemplate="%{x|%Y-%m-%d}<br>折溢價: %{y:+.2f}%<extra></extra>",
    ))
    fig.add_hline(y=0, line=dict(color="black", width=1))
    fig.update_layout(
        title="00981A 折溢價走勢(綠=溢價,紅=折價)",
        height=380, yaxis_title="折溢價 %",
        margin=dict(l=50, r=20, t=50, b=40),
    )
    return fig


def fig_top20_treemap(top20: pd.DataFrame) -> go.Figure:
    df = top20.copy()
    df["label"] = df["stock_code"] + " " + df["stock_name"] + "<br>" + df["weight_pct"].round(2).astype(str) + "%"
    fig = go.Figure(go.Treemap(
        labels=df["label"], parents=[""] * len(df), values=df["weight_pct"],
        textinfo="label", hovertemplate="%{label}<extra></extra>",
        marker=dict(
            colors=df["weight_pct"], colorscale="Blues",
            colorbar=dict(title="權重%"),
        ),
    ))
    fig.update_layout(
        title=f"Top 20 持股權重(資料截止 {top20['trade_date'].iloc[0].date()})",
        height=520, margin=dict(l=10, r=10, t=50, b=10),
    )
    return fig


def fig_returns_heatmap(prices: pd.DataFrame, top20: pd.DataFrame, window_days: int) -> go.Figure:
    end = prices["trade_date"].max()
    start = end - pd.Timedelta(days=window_days)
    codes = top20["stock_code"].tolist()
    names = dict(zip(top20["stock_code"], top20["stock_name"]))

    sub = prices[(prices["ticker"].isin(codes))
                 & (prices["trade_date"] >= start)].copy()
    pivot = sub.pivot(index="ticker", columns="trade_date", values="chg")
    pivot = pivot.reindex(codes).dropna(how="all")

    pivot.index = [f"{c} {names[c]}" for c in pivot.index]
    z = pivot.values
    x = [d.strftime("%m-%d") for d in pivot.columns]
    y = list(pivot.index)

    fig = go.Figure(go.Heatmap(
        z=z, x=x, y=y, colorscale="RdYlGn", zmid=0,
        zmin=-7, zmax=7,
        hovertemplate="%{y}<br>%{x}<br>漲跌: %{z:+.2f}%<extra></extra>",
        colorbar=dict(title="漲跌%"),
    ))
    fig.update_layout(
        title=f"Top 20 個股每日漲跌熱圖(近 {window_days} 天)",
        height=600, margin=dict(l=20, r=20, t=50, b=40),
        yaxis=dict(autorange="reversed"),
    )
    return fig


def fig_weight_evolution(holdings: pd.DataFrame, top20: pd.DataFrame) -> go.Figure:
    codes = top20["stock_code"].tolist()
    names = dict(zip(top20["stock_code"], top20["stock_name"]))
    sub = holdings[holdings["stock_code"].isin(codes)].copy()
    pivot = sub.pivot_table(
        index="trade_date", columns="stock_code", values="weight_pct", aggfunc="last",
    ).fillna(0)
    pivot = pivot.reindex(columns=codes)

    fig = go.Figure()
    for code in codes:
        fig.add_trace(go.Scatter(
            x=pivot.index, y=pivot[code],
            name=f"{code} {names[code]}", mode="lines",
            stackgroup="one", line=dict(width=0.5),
            hovertemplate="%{x|%Y-%m-%d}<br>%{fullData.name}<br>%{y:.2f}%<extra></extra>",
        ))
    fig.update_layout(
        title="Top 20 持股權重隨時間演變(堆疊)",
        height=520, yaxis_title="累積權重 %", hovermode="x unified",
        margin=dict(l=50, r=20, t=50, b=40),
        legend=dict(orientation="v", x=1.02, y=1, font=dict(size=10)),
    )
    return fig


def fig_position_heatmap(holdings: pd.DataFrame, prices: pd.DataFrame,
                          top20: pd.DataFrame, n_days: int = 10) -> go.Figure:
    """Top 20 daily share-count delta (forward-filled) over last N trading days."""
    trading_days = prices[prices["ticker"] == "00981A"]["trade_date"] \
        .sort_values(ascending=False).head(n_days).sort_values().tolist()
    codes = top20["stock_code"].tolist()
    names = dict(zip(top20["stock_code"], top20["stock_name"]))

    z = []
    for code in codes:
        hist = holdings[holdings["stock_code"] == code] \
            .sort_values("trade_date")[["trade_date", "shares"]]
        hist_pairs = list(zip(hist["trade_date"], hist["shares"]))

        def shares_at(d):
            last = None
            for td, s in hist_pairs:
                if td <= d:
                    last = s
                else:
                    break
            return last

        prev_d = trading_days[0] - pd.Timedelta(days=1)
        running = shares_at(prev_d)
        row = []
        for d in trading_days:
            cur = shares_at(d)
            if cur is None or running is None:
                row.append(None)
            else:
                row.append((cur - running) / 1000)
            running = cur if cur is not None else running
        z.append(row)

    fig = go.Figure(go.Heatmap(
        z=z,
        x=[d.strftime("%m-%d") for d in trading_days],
        y=[f"{c} {names[c]}" for c in codes],
        colorscale="RdBu_r", zmid=0,
        hovertemplate="%{y}<br>%{x}<br>變化: %{z:+,.0f} 千股<extra></extra>",
        colorbar=dict(title="千股"),
    ))
    fig.update_layout(
        title=f"Top 20 經理人加減碼熱圖(近 {n_days} 個交易日,千股)",
        height=600, margin=dict(l=20, r=20, t=50, b=40),
        yaxis=dict(autorange="reversed"),
    )
    return fig


def fig_inst_total_heatmap(chips_all: sqlite3.Connection,
                            prices: pd.DataFrame,
                            top20: pd.DataFrame, n_days: int = 10) -> go.Figure:
    """Top 20 daily 三大法人合計買賣超 over last N trading days, in 張."""
    trading_days = prices[prices["ticker"] == "00981A"]["trade_date"] \
        .sort_values(ascending=False).head(n_days).sort_values().tolist()
    codes = top20["stock_code"].tolist()
    names = dict(zip(top20["stock_code"], top20["stock_name"]))

    placeholders = ",".join("?" * len(codes))
    rows = chips_all.execute(
        f"SELECT trade_date, stock_code, SUM(net) FROM chips "
        f"WHERE stock_code IN ({placeholders}) "
        f"GROUP BY trade_date, stock_code",
        codes,
    ).fetchall()
    df = pd.DataFrame(rows, columns=["trade_date", "stock_code", "net"])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    pivot = df.pivot_table(index="stock_code", columns="trade_date",
                           values="net", aggfunc="sum")
    pivot = pivot.reindex(codes).reindex(columns=trading_days) / 1000

    fig = go.Figure(go.Heatmap(
        z=pivot.values,
        x=[d.strftime("%m-%d") for d in trading_days],
        y=[f"{c} {names[c]}" for c in codes],
        colorscale="RdBu_r", zmid=0,
        hovertemplate="%{y}<br>%{x}<br>三大法人: %{z:+,.0f} 張<extra></extra>",
        colorbar=dict(title="張"),
    ))
    fig.update_layout(
        title=f"Top 20 三大法人合計買賣超熱圖(近 {n_days} 個交易日,張)",
        height=600, margin=dict(l=20, r=20, t=50, b=40),
        yaxis=dict(autorange="reversed"),
    )
    return fig


def fig_chips_bar(chips: pd.DataFrame, top20: pd.DataFrame, window_days: int) -> go.Figure:
    end = chips["trade_date"].max()
    start = end - pd.Timedelta(days=window_days)
    codes = top20["stock_code"].tolist()
    names = dict(zip(top20["stock_code"], top20["stock_name"]))

    sub = chips[(chips["stock_code"].isin(codes))
                & (chips["trade_date"] >= start)]
    agg = sub.groupby("stock_code")["net"].sum().reindex(codes).fillna(0)
    agg_lots = agg / 1000

    label = [f"{c} {names[c]}" for c in agg.index]

    fig = go.Figure(go.Bar(
        y=label, x=agg_lots.values, orientation="h",
        marker_color=["#d62728" if v < 0 else "#2ca02c" for v in agg_lots.values],
        hovertemplate="%{y}<br>投信累計: %{x:+,.0f} 張<extra></extra>",
    ))
    fig.add_vline(x=0, line=dict(color="black", width=1))
    fig.update_layout(
        title=f"Top 20 — 投信買賣超累計(近 {window_days} 天,張)",
        height=600, xaxis_title="淨買賣超(張)",
        margin=dict(l=180, r=20, t=50, b=40),
        yaxis=dict(autorange="reversed"),
    )
    return fig


def fig_units_change(fund_meta: pd.DataFrame) -> go.Figure:
    df = fund_meta.dropna(subset=["units_change"])
    fig = go.Figure(go.Bar(
        x=df["trade_date"], y=df["units_change"] / 1e6,
        marker_color=["#2ca02c" if v >= 0 else "#d62728" for v in df["units_change"]],
        hovertemplate="%{x|%Y-%m-%d}<br>單位數變化: %{y:+,.1f} 百萬<extra></extra>",
    ))
    fig.add_hline(y=0, line=dict(color="black", width=1))
    fig.update_layout(
        title="ETF 申購/贖回(已發行單位日變化,百萬)",
        height=380, yaxis_title="變化(百萬單位)",
        margin=dict(l=50, r=20, t=50, b=40),
    )
    return fig


def render(figures: list[tuple[str, go.Figure]], out_path: Path,
           subtitle: str):
    parts = []
    parts.append(f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<title>00981A Dashboard</title>
<style>
  body {{ font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
         max-width: 1400px; margin: 0 auto; padding: 20px;
         background: #f6f7f9; color: #222; }}
  h1 {{ color: #111; margin-bottom: 4px; }}
  .sub {{ color: #666; margin-bottom: 24px; font-size: 14px; }}
  .card {{ background: white; border-radius: 10px; padding: 16px;
           margin-bottom: 22px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }}
  .card h2 {{ margin: 0 0 8px 0; font-size: 15px; color: #555;
              text-transform: uppercase; letter-spacing: .5px; }}
  footer {{ color: #999; font-size: 12px; text-align: center; margin-top: 40px; }}
</style>
</head>
<body>
<h1>00981A 主動統一台股增長 — 互動報告</h1>
<div class="sub">{subtitle}</div>
""")
    for i, (caption, fig) in enumerate(figures):
        include_js = "cdn" if i == 0 else False
        html = fig.to_html(
            include_plotlyjs=include_js, full_html=False,
            config={"displayModeBar": False, "responsive": True},
        )
        parts.append(f'<div class="card"><h2>{caption}</h2>{html}</div>')
    parts.append("""
<footer>Data: 統一投信 PCF API · Yahoo Finance · FinMind &nbsp;|&nbsp;
Generated by build_dashboard.py</footer>
</body></html>
""")
    out_path.write_text("\n".join(parts))


def build(window_days: int):
    holdings, fund_meta, prices, chips = load(window_days)
    top20 = latest_top20(holdings)
    chips_conn = sqlite3.connect(PRICES_DB)

    latest = holdings["trade_date"].max().date()
    figures = [
        ("一、基金規模演進", fig_fund_scale(fund_meta)),
        ("二、ETF 折溢價", fig_premium(prices, fund_meta)),
        ("三、申購贖回(單位數變化)", fig_units_change(fund_meta)),
        ("四、00981A vs 加權指數", fig_etf_vs_index(prices, window_days)),
        ("五、Top 20 持股權重(最新)", fig_top20_treemap(top20)),
        ("六、Top 20 持股權重隨時間演變", fig_weight_evolution(holdings, top20)),
        ("七、Top 20 個股每日漲跌熱圖", fig_returns_heatmap(prices, top20, window_days)),
        ("八、Top 20 — 投信買賣超累計", fig_chips_bar(chips, top20, window_days)),
        ("九、Top 20 經理人加減碼(近 10 交易日)",
            fig_position_heatmap(holdings, prices, top20, n_days=10)),
        ("十、Top 20 三大法人合計買賣超(近 10 交易日)",
            fig_inst_total_heatmap(chips_conn, prices, top20, n_days=10)),
    ]

    subtitle = (f"資料截止 {latest} · 觀察窗 {window_days} 天 · "
                f"產生時間 {datetime.now().isoformat(timespec='seconds')}")

    # archive copy with date in filename
    out = ROOT / "reports" / f"dashboard_{latest}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    render(figures, out, subtitle=subtitle)
    print(f"Wrote {out}")

    # stable URL for GitHub Pages
    pages_out = ROOT / "docs" / "index.html"
    pages_out.parent.mkdir(parents=True, exist_ok=True)
    render(figures, pages_out, subtitle=subtitle)
    print(f"Wrote {pages_out}")

    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=30)
    args = ap.parse_args()
    build(args.window)


if __name__ == "__main__":
    main()
