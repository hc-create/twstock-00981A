"""Build a markdown report for 00981A: top holdings, month-over-month
position changes, and daily price action vs TAIEX.

Usage:
    python build_report.py                # latest holding date, past 30 calendar days
    python build_report.py --window 60    # past 60 calendar days
"""
import argparse
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from common import HOLDINGS_DB, PRICES_DB, ROOT


def q(conn, sql, *params):
    return conn.execute(sql, params).fetchall()


def fmt_int(n):
    if n is None:
        return "-"
    return f"{int(n):>14,}"


def fmt_signed(n, fmt="+,.0f", placeholder="-"):
    if n is None:
        return placeholder
    return format(n, fmt)


def build(window_days: int = 30):
    conn_h = sqlite3.connect(HOLDINGS_DB)
    conn_p = sqlite3.connect(PRICES_DB)

    latest = q(conn_h, "SELECT MAX(trade_date) FROM holdings")[0][0]
    latest_dt = date.fromisoformat(latest)
    start_dt = latest_dt - timedelta(days=window_days)

    earliest_in_window = q(conn_h,
        "SELECT MIN(trade_date) FROM holdings WHERE trade_date >= ?",
        start_dt.isoformat(),
    )[0][0]
    start = earliest_in_window or latest

    n_days = q(conn_h,
        "SELECT COUNT(DISTINCT trade_date) FROM holdings WHERE trade_date BETWEEN ? AND ?",
        start, latest,
    )[0][0]

    # ---- Top 20 holdings (latest) ----
    top20 = q(conn_h, """
        SELECT stock_code, stock_name, weight_pct, shares, market_value
          FROM holdings
         WHERE trade_date = ?
         ORDER BY weight_pct DESC
         LIMIT 20
    """, latest)

    # ---- Position changes for current Top 20 vs window start ----
    changes = []
    for code, name, w_now, sh_now, mv_now in top20:
        first_in_window = q(conn_h, """
            SELECT trade_date, shares, weight_pct
              FROM holdings
             WHERE stock_code = ? AND trade_date BETWEEN ? AND ?
             ORDER BY trade_date ASC LIMIT 1
        """, code, start, latest)
        if first_in_window:
            d0, sh0, w0 = first_in_window[0]
            sh_diff = sh_now - sh0
            w_diff = w_now - w0
            status = "持平"
            if sh_diff > 0:
                status = "加碼"
            elif sh_diff < 0:
                status = "減碼"
        else:
            d0 = "-"
            sh0 = w0 = sh_diff = w_diff = None
            status = "新增"
        changes.append({
            "code": code, "name": name,
            "weight_now": w_now, "shares_now": sh_now,
            "weight_then": w0, "shares_then": sh0,
            "shares_diff": sh_diff, "weight_diff": w_diff,
            "status": status, "since": d0,
        })

    # ---- Stocks that exited Top 20 during window ----
    top20_codes_now = {r[0] for r in top20}
    earliest_top20 = q(conn_h, """
        SELECT stock_code, stock_name, weight_pct
          FROM holdings
         WHERE trade_date = ?
         ORDER BY weight_pct DESC LIMIT 20
    """, start)
    exited = [(c, n, w) for c, n, w in earliest_top20 if c not in top20_codes_now]

    new_in_top20 = [c for c in changes if c["status"] == "新增"
                    or (c["since"] != "-" and c["since"] != start)]

    # ---- Stocks completely removed from portfolio during window ----
    removed = q(conn_h, """
        SELECT DISTINCT h1.stock_code, h1.stock_name
          FROM holdings h1
         WHERE h1.trade_date = ?
           AND NOT EXISTS (
               SELECT 1 FROM holdings h2
                WHERE h2.stock_code = h1.stock_code
                  AND h2.trade_date = ?
           )
    """, start, latest)

    # ---- Stocks newly added during window ----
    added = q(conn_h, """
        SELECT DISTINCT h1.stock_code, h1.stock_name, h1.weight_pct
          FROM holdings h1
         WHERE h1.trade_date = ?
           AND NOT EXISTS (
               SELECT 1 FROM holdings h2
                WHERE h2.stock_code = h1.stock_code
                  AND h2.trade_date = ?
           )
    """, latest, start)

    # ---- Daily price action for window (with NAV + premium/discount) ----
    # PCF API TranDate has occasional alignment quirks (weekend timestamps,
    # 1-day lag). Forward-fill: for any trading day without NAV, use the
    # most recent prior fund_meta entry.
    nav_series = q(conn_h,
        "SELECT trade_date, nav_per_unit FROM fund_meta "
        "WHERE nav_per_unit IS NOT NULL ORDER BY trade_date",
    )

    def nav_at(d: str):
        last = None
        for td, nav in nav_series:
            if td <= d:
                last = nav
            else:
                break
        return last
    price_rows = q(conn_p, """
        SELECT trade_date,
               MAX(CASE WHEN ticker='TAIEX'  THEN close END),
               MAX(CASE WHEN ticker='TAIEX'  THEN "change" END),
               MAX(CASE WHEN ticker='00981A' THEN close END),
               MAX(CASE WHEN ticker='00981A' THEN "change" END)
          FROM prices
         WHERE ticker IN ('TAIEX','00981A')
           AND trade_date BETWEEN ? AND ?
         GROUP BY trade_date
         ORDER BY trade_date DESC
    """, start, latest)

    # ---- Top 20 individual price action vs ETF over window ----
    top20_perf = []
    for c in changes:
        rows = q(conn_p, """
            SELECT trade_date, close FROM prices
             WHERE ticker = ? AND trade_date BETWEEN ? AND ?
             ORDER BY trade_date ASC
        """, c["code"], start, latest)
        if len(rows) >= 2:
            ret = (rows[-1][1] - rows[0][1]) / rows[0][1] * 100
            last_chg = q(conn_p, """
                SELECT "change" FROM prices
                 WHERE ticker = ? AND trade_date = ?
            """, c["code"], latest)
            last_chg = last_chg[0][0] if last_chg else None
        else:
            ret = None
            last_chg = None

        # 投信 net buy/sell — period total + last day, in lots (張)
        period_net = q(conn_p, """
            SELECT SUM(net) FROM chips
             WHERE stock_code = ? AND institution = 'Investment_Trust'
               AND trade_date BETWEEN ? AND ?
        """, c["code"], start, latest)
        period_net = period_net[0][0] if period_net and period_net[0][0] is not None else 0
        last_net = q(conn_p, """
            SELECT net FROM chips
             WHERE stock_code = ? AND institution = 'Investment_Trust'
               AND trade_date = ?
        """, c["code"], latest)
        last_net = last_net[0][0] if last_net else None

        top20_perf.append((c, ret, last_chg, period_net, last_net))

    # ---- Fund meta change ----
    meta_rows = q(conn_h, """
        SELECT trade_date, nav_total, units_total, nav_per_unit, beneficiaries
          FROM fund_meta
         WHERE trade_date IN (?, ?)
         ORDER BY trade_date
    """, start, latest)

    # ---- Build markdown ----
    out = []
    out.append(f"# 00981A 主動統一台股增長 — 持股與市場分析\n")
    out.append(f"- **資料截止**：{latest}（API 回傳的最新 TranDate）")
    out.append(f"- **觀察窗**：{start} → {latest}（共 {n_days} 個交易日）")
    out.append(f"- **報告產生**：{datetime.now().isoformat(timespec='seconds')}")
    out.append("")

    # Fund overview
    out.append("## 一、基金規模變化")
    out.append("")
    out.append("| 日期 | 基金淨值(億) | 已發行單位(億) | 單位淨值 | 受益人數 |")
    out.append("|---|---:|---:|---:|---:|")
    for r in meta_rows:
        d, nav, units, npu, ben = r
        out.append(f"| {d} | {nav/1e8:.1f} | {units/1e8:.2f} | {npu:.2f} | {ben:,} |")
    if len(meta_rows) == 2:
        nav_diff = meta_rows[1][1] - meta_rows[0][1]
        nav_pct = nav_diff / meta_rows[0][1] * 100
        units_diff = meta_rows[1][2] - meta_rows[0][2]
        out.append(f"\n→ 規模 {fmt_signed(nav_diff/1e8, '+,.1f')} 億（{nav_pct:+.1f}%），"
                   f"受益單位 {fmt_signed(units_diff/1e8, '+,.2f')} 億單位\n")

    # Top 20
    out.append("## 二、目前 Top 20 持股 + 期間調整")
    out.append("")
    out.append("| # | 代號 | 名稱 | 權重% | 期初權重% | 權重變化 | 股數變化 | ETF動作 | 期間漲跌 | 當日漲跌 | 投信期間買賣超(張) | 投信當日(張) |")
    out.append("|--:|---|---|--:|--:|--:|--:|---|--:|--:|--:|--:|")
    for i, (c, ret, last_chg, period_net, last_net) in enumerate(top20_perf, 1):
        x = c
        wnow = f"{x['weight_now']:.2f}"
        wthen = f"{x['weight_then']:.2f}" if x['weight_then'] is not None else "—"
        wdiff = f"{x['weight_diff']:+.2f}" if x['weight_diff'] is not None else "新"
        shdiff = f"{x['shares_diff']:+,}" if x['shares_diff'] is not None else "—"
        ret_s = f"{ret:+.1f}%" if ret is not None else "—"
        chg_s = f"{last_chg:+.2f}%" if last_chg is not None else "—"
        period_lots = f"{period_net/1000:+,.0f}" if period_net else "0"
        last_lots = f"{last_net/1000:+,.0f}" if last_net else "—"
        out.append(
            f"| {i} | {x['code']} | {x['name']} | {wnow} | {wthen} | {wdiff} | "
            f"{shdiff} | {x['status']} | {ret_s} | {chg_s} | {period_lots} | {last_lots} |"
        )
    out.append("")

    # Largest add / trim
    sorted_by_share = sorted(changes,
                             key=lambda x: (x["shares_diff"] or 0), reverse=True)
    biggest_add = [c for c in sorted_by_share if (c["shares_diff"] or 0) > 0][:5]
    biggest_trim = [c for c in reversed(sorted_by_share)
                    if (c["shares_diff"] or 0) < 0][:5]

    out.append("### Top 20 中加碼最多")
    out.append("")
    if biggest_add:
        out.append("| 代號 | 名稱 | 加碼股數 | 期初→現在 權重 |")
        out.append("|---|---|--:|---|")
        for c in biggest_add:
            out.append(f"| {c['code']} | {c['name']} | {c['shares_diff']:+,} | "
                       f"{c['weight_then']:.2f}% → {c['weight_now']:.2f}% |")
    else:
        out.append("（無）")
    out.append("")

    out.append("### Top 20 中減碼最多")
    out.append("")
    if biggest_trim:
        out.append("| 代號 | 名稱 | 減碼股數 | 期初→現在 權重 |")
        out.append("|---|---|--:|---|")
        for c in biggest_trim:
            out.append(f"| {c['code']} | {c['name']} | {c['shares_diff']:+,} | "
                       f"{c['weight_then']:.2f}% → {c['weight_now']:.2f}% |")
    else:
        out.append("（無）")
    out.append("")

    # Entries / exits
    out.append("## 三、整段觀察窗的進出名單")
    out.append("")
    out.append(f"### 期間新進(整段觀察窗內全新持股) — {len(added)} 檔")
    out.append("")
    if added:
        out.append("| 代號 | 名稱 | 目前權重% |")
        out.append("|---|---|--:|")
        for code, name, w in sorted(added, key=lambda r: -r[2]):
            out.append(f"| {code} | {name} | {w:.2f} |")
    else:
        out.append("（無)")
    out.append("")

    out.append(f"### 期間出清(整段觀察窗內全部賣光) — {len(removed)} 檔")
    out.append("")
    if removed:
        out.append("| 代號 | 名稱 |")
        out.append("|---|---|")
        for code, name in removed:
            out.append(f"| {code} | {name} |")
    else:
        out.append("（無）")
    out.append("")

    out.append(f"### 跌出 Top 20 — {len(exited)} 檔")
    out.append("")
    if exited:
        out.append("| 代號 | 名稱 | 期初權重% |")
        out.append("|---|---|--:|")
        for code, name, w in exited:
            out.append(f"| {code} | {name} | {w:.2f} |")
    else:
        out.append("（無）")
    out.append("")

    # Daily action
    out.append("## 四、觀察窗內每日大盤 vs 00981A(含折溢價)")
    out.append("")
    out.append("| 日期 | TAIEX 收盤 | TAIEX 漲跌 | 00981A 收盤 | 00981A 漲跌 | 超額 | NAV/單位 | 折溢價% |")
    out.append("|---|--:|--:|--:|--:|--:|--:|--:|")
    for r in price_rows:
        d, t, tc, e, ec = r
        if t is None or e is None:
            continue
        excess = (ec or 0) - (tc or 0)
        nav = nav_at(d)
        if nav and e:
            premium = (e - nav) / nav * 100
            nav_s = f"{nav:.2f}"
            prem_s = f"{premium:+.2f}%"
        else:
            nav_s = "—"
            prem_s = "—"
        out.append(
            f"| {d} | {t:,.0f} | {tc:+.2f}% | {e:.2f} | {ec:+.2f}% | "
            f"{excess:+.2f} | {nav_s} | {prem_s} |"
        )
    out.append("")

    out.append("---")
    out.append(f"_Generated by `build_report.py` — data from 統一投信 PCF API + Yahoo Finance_")

    report = "\n".join(out)
    out_path = ROOT / "reports" / f"{latest}_summary.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    print(f"Wrote {out_path}")
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=30,
                    help="Look-back window in calendar days (default 30)")
    args = ap.parse_args()
    build(window_days=args.window)


if __name__ == "__main__":
    main()
