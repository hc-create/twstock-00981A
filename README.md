# 00981A 主動統一台股增長 — 持股追蹤

## 結構

```
twstock_00981A/
├── scripts/
│   ├── common.py            共用工具（DB、日期轉換）
│   ├── fetch_holdings.py    抓 PCF 持股 + 基金規模 + NAV，存 holdings.sqlite
│   ├── fetch_prices.py      抓 TAIEX/ETF/個股 OHLCV (yfinance)，存 prices.sqlite
│   ├── fetch_chips.py       抓三大法人買賣超 (FinMind)，存 prices.sqlite
│   └── build_report.py      產生含投信籌碼+折溢價的分析報告
├── data/
│   ├── holdings.sqlite       每日 50+ 檔持股、基金規模
│   ├── prices.sqlite         TAIEX、00981A、所有曾入榜個股的日 K
│   └── raw_pcf/              統一投信 API 回傳原始 JSON（每日一檔）
├── reports/                  build_report.py 產出
└── logs/
```

## 資料來源

- **持股**：統一投信 `https://www.ezmoney.com.tw/ETF/Transaction/GetPCF`（POST）
  - 接受歷史日期，可一次回補全部歷史
  - 揭露 lag：當天抓到的是前一個交易日的收盤持倉
- **股價**：Yahoo Finance（yfinance），自動處理 `.TW` / `.TWO` 兩個後綴
- **三大法人**：FinMind `TaiwanStockInstitutionalInvestorsBuySell`（免註冊，自動覆蓋投信/外資/自營商）
- **折溢價**：以 fund_meta.nav_per_unit 與 prices.00981A.close 計算，TranDate 對齊用 forward-fill

## 用法

```bash
# 全量回補（基金掛牌至今）
python3 scripts/fetch_holdings.py --backfill

# 單日抓取（每日排程用）
python3 scripts/fetch_holdings.py --date 2026-05-04

# 抓最新 30 個交易日
python3 scripts/fetch_holdings.py --days 30

# 股價（預設範圍 = fund launch → today）
python3 scripts/fetch_prices.py

# 三大法人籌碼
python3 scripts/fetch_chips.py

# 互動式 HTML 儀表板（雙擊 reports/dashboard_*.html 即可看）
python3 scripts/build_dashboard.py --window 30

# 指定股價範圍
python3 scripts/fetch_prices.py --start 2026-04-01 --end 2026-05-04

# 產生報告（預設觀察 30 天）
python3 scripts/build_report.py --window 30
```

## 後續

- Phase 2：包成 GitHub Actions cron，每天 14:30 + 隔日 08:30 自動跑
