# market-data

Daily public market-data snapshot for research. Daily bars come from the Yahoo Finance chart API only. Quarterly fundamentals come from the SEC EDGAR companyfacts XBRL API only (not Yahoo). Missing tickers or fundamental fields are listed in `data/_errors.txt` or left blank. No substitute source is used, and no numbers are filled in by hand.

由 Grok Bot 在每個美股交易日收盤後自動抓取並推送。日線只來自 Yahoo Finance，季度財報只來自 SEC EDGAR companyfacts，抓不到的標的會寫進 `data/_errors.txt`，缺值留白，不會用其他來源的數字補上。

## Update schedule / 更新時間

- Full update (`python3 update.py`): daily after the US cash-session close, around **06:30 Asia/Kuala_Lumpur**, Tuesday to Saturday (Monday morning MYT is Sunday in New York, so there is no new US session to fetch).
- 完整更新：每個美股交易日收盤後，約馬來西亞時間 **06:30**（周二至周六）。
- Asia-only update (`python3 update.py --asia`): after the Asian cash close, around **15:00 Asia/Kuala_Lumpur**, Monday to Friday. This overwrites only the 16 Taiwan, Korea, Japan, and `BTC-USD` files; every other CSV is left untouched.
- 亞洲盤更新：亞洲市場收盤後，約馬來西亞時間 **15:00**（周一至周五），只更新台、韓、日標的與 `BTC-USD`。
- The full update also refreshes `data/_fundamentals.csv` when that file is missing or older than one day. A fundamentals-only run is `python3 update.py --fundamentals`. Asia-only runs do not touch fundamentals.
- 完整更新若基本面檔缺失或已超過一天，會一併從 EDGAR 更新 `data/_fundamentals.csv`。也可單獨執行 `python3 update.py --fundamentals`。亞洲盤更新不會改基本面檔。
- Reruns on the same day overwrite the same files and may create a new commit.

## Files / 檔案說明

| Path | What it is |
| --- | --- |
| `data/<SYMBOL>.csv` | Daily bars for one Yahoo Finance symbol. The filename is the exact symbol, including `^` (for example `data/^VIX.csv`, `data/2330.TW.csv`, `data/BTC-USD.csv`). |
| `data/_constituents.csv` | Current S&P 500 and Nasdaq-100 members: `symbol,name,sector,index`. `index` is `SP500`, `NDX`, or `both`. Symbols are in Yahoo format (`BRK.B` → `BRK-B`). Sector is the Wikipedia GICS Sector when the name is in the S&P 500. Nasdaq-100-only names have no GICS column on Wikipedia, so that row uses the ICB Industry from the Wikipedia Nasdaq-100 list. |
| `data/_themes.csv` | Theme map for selected ETFs and focus stocks: `symbol,theme_zh,theme_en`. A symbol may have more than one row if it belongs to more than one theme; existing rows are kept and a new theme is added as an extra row, not an overwrite. This is a static mapping shipped with the daily update, not inferred from prices. Every theme member also has its own `data/<SYMBOL>.csv`. |
| `data/_updated.txt` | UTC and Asia/Kuala_Lumpur timestamps of the last full run, each ticker's last bar date, and (after an Asia-only run) `asia_updated_utc` / `asia_updated_asia_kuala_lumpur`. An `--asia` run updates only the 16 Asia/BTC last-bar lines and the asia timestamp; other lines are kept. |
| `data/_errors.txt` | Tickers that could not be fetched, with the reason. An `--asia` run updates or clears error lines only for those 16 tickers; unrelated error lines from the last full run are kept. Fundamentals problems are stored as `fundamentals:SYMBOL: reason` and are not cleared by an `--asia` run. |
| `data/_fundamentals.csv` | Quarterly fundamentals from 2015-01-01 onward for every current S&P 500 and Nasdaq-100 constituent, plus the theme stocks listed below. See the Fundamentals section for columns, sources, and what `report_date` means. |
| `update.py` | The script that fetches, writes, commits, and pushes. |
| `fundamentals.py` | SEC EDGAR helpers used by `update.py` to build `_fundamentals.csv`. |

成分股名單來自維基百科：[List of S&P 500 companies](https://en.wikipedia.org/wiki/List_of_S%26P_500_companies)、[List of NASDAQ-100 companies](https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies)。美股代號的點號會改成 Yahoo 格式（`BRK.B` → `BRK-B`）。

## CSV columns / 欄位

`date, open, high, low, close, adj_close, volume`

- `date` is the exchange-local trading date (`YYYY-MM-DD`), from 2015-01-01 onward. Names listed after that date start at their first Yahoo bar (the listing date).
- Prices are unadjusted OHLC; `adj_close` is Yahoo's split/dividend-adjusted close. If Yahoo omits `adj_close` for a bar, that cell is left blank.
- `volume` is left blank when Yahoo does not provide it (some indices).
- Rows with a missing close are dropped. No interpolated or filled values.

## Fundamentals / 季度財報

`data/_fundamentals.csv` columns:

`symbol, fiscal_quarter_end, report_date, revenue, gross_profit, eps_diluted, net_income, date_type, note`

Universe: every current S&P 500 and Nasdaq-100 constituent, plus these theme stocks (deduplicated): `MU` `SNDK` `WDC` `STX` `COHR` `LITE` `CIEN` `GLW` `AAOI` `FN` `BE` `VST` `CEG` `GEV` `ETN` `VRT` `PWR` `NRG` `AMAT` `LRCX` `KLAC` `ASML` `TER` `AEHR` `FORM` `ONTO` `ACMR` `CAMT` `CRWV` `NBIS` `IREN` `APLD` `CIFR` `WULF` `CORZ`.

Source (one source per field, never mixed with Yahoo or Nasdaq numbers):

- Values (`revenue`, `gross_profit`, `eps_diluted`, `net_income`) come from the [SEC EDGAR companyfacts XBRL API](https://data.sec.gov/api/xbrl/companyfacts/). For each field a fixed US-GAAP / IFRS tag priority is used (see `fundamentals.py`). Companies that changed tags over time (for example `SalesRevenueNet` before 2018 and `RevenueFromContractWithCustomerExcludingAssessedTax` after) may switch tags at the quarter when the newer tag begins; both tags are still EDGAR companyfacts. A value first appears as reported in the original 10-Q / 10-K / 20-F (as-first-reported, not a later restatement, and not retroactively split-adjusted). Missing values are left blank. Banks usually have no `GrossProfit` tag; that cell stays blank. Diluted EPS is never replaced with basic EPS.
- `fiscal_quarter_end` is the issuer's fiscal period end date (`YYYY-MM-DD`), not a calendar quarter label. Apple's fiscal year ends in September, so a row ending `2025-12-27` is Apple's fiscal Q1, not calendar Q4 in name only.
- `report_date` is the public earnings publication date, not the quarter end. It is the filing date of the 8-K that includes Item 2.02 (Results of Operations and Financial Condition), which is the earnings press-release date on EDGAR, almost always the same calendar day as the announcement. `date_type` is `8-K` in that case. For foreign issuers that file 6-K / 20-F instead of 8-K / 10-Q (for example `ASML`, `CAMT`, `NBIS`, `PDD`), the pipeline tries to use the 6-K filing date when a 6-K can be matched to that quarter (`date_type=6-K`). Many of those issuers do not tag quarterly income-statement numbers in companyfacts, so the symbol is listed under `fundamentals:SYMBOL` in `data/_errors.txt` and no invented numbers are written.
- The 10-Q / 10-K / 20-F `filed` date is later than the press release and is **not** used as `report_date` unless no 8-K Item 2.02 or matching 6-K is found, the periodic report itself was filed within 90 days of the quarter end, and `date_type` is set to `filing_fallback`. If even that is not safe (for example a comparative quarter that only appears later in a post-IPO 10-Q), `report_date` is left blank. Backtests should treat a blank or `filing_fallback` date with care.
- Q4 is often filed only as a full-year total in the 10-K. When a 90-day Q4 fact is missing but Q1–Q3 and the annual total exist for the same tag, `revenue`, `gross_profit`, and `net_income` for that Q4 are derived as annual minus the three quarters and `note` contains `derived_q4_...`. Diluted EPS is **not** derived this way, because annual EPS is not the sum of quarterly EPS when the share count changes; Q4 `eps_diluted` is left blank unless a quarterly EPS fact exists.
- Units are USD whenever EDGAR provides USD. A non-USD amount is still written, and `note` records the unit (for example `revenue_unit:EUR`).
- History starts at fiscal quarter ends on or after 2015-01-01. Names that began reporting later (spin-offs, IPOs) start at their first EDGAR quarterly fact. Pre-listing comparative quarters may have values but a blank `report_date`.

## Fixed tickers (always fetched) / 固定標的

- Market / benchmarks: `SPY` `QQQ` `RSP` `IWM` `SOXX` `SMH` `IGV` `BTC-USD`
- US sectors: `XLK` `XLV` `XLF` `XLY` `XLP` `XLE` `XLI` `XLB` `XLU` `XLRE` `XLC`
- Sentiment / rates: `^VIX` `^VIX3M` `^TNX` `DX-Y.NYB`
- Taiwan: `^TWII` `2330.TW` `2454.TW` `2303.TW` `2408.TW` `3711.TW`
- Korea: `^KS11` `005930.KS` `000660.KS`
- Japan: `^N225` `8035.T` `6857.T` `6146.T` `6920.T` `285A.T`
- Theme ETFs and focus stocks: `AIQ` `BOTZ` `QTUM` `CIBR` `GDX` `SIL` `SLX` `URA` `JETS` `IYT` `ITA` `TAN` `OIH` `IHI` `XBI` `KBE` `KRE` `XRT` `ITB` `KWEB` `SOCL` `IYZ` `IBIT` `WGMI` `MU` `SNDK` `WDC` `STX`
- Optical networking / 光通訊: `COHR` `LITE` `CIEN` `GLW` `AAOI` `FN`
- Power / 電力: `BE` `VST` `CEG` `GEV` `ETN` `VRT` `PWR` `NRG`
- Semi equipment and test / 半導體設備與測試: `AMAT` `LRCX` `KLAC` `ASML` `TER` `AEHR` `FORM` `ONTO` `ACMR` `CAMT`
- Neocloud: `CRWV` `NBIS` `IREN` `APLD` `CIFR` `WULF` `CORZ`
- Plus every current S&P 500 and Nasdaq-100 constituent (deduplicated). Names that also appear in the fixed or theme lists are fetched once.

Theme labels (Chinese and English) are in `data/_themes.csv`. That file covers the theme ETFs, already-fetched `SMH` `SOXX` `IGV`, the memory names `MU` `SNDK` `WDC` `STX`, and the four stock-level themes above (optical networking, power, semi equipment and test, Neocloud). A symbol that sits in more than one theme has one row per theme.

## Raw URLs / 直接下載

```
https://raw.githubusercontent.com/jiaren19/market-data/main/data/<SYMBOL>.csv
```

Examples:

```
https://raw.githubusercontent.com/jiaren19/market-data/main/data/SPY.csv
https://raw.githubusercontent.com/jiaren19/market-data/main/data/2330.TW.csv
https://raw.githubusercontent.com/jiaren19/market-data/main/data/%5EVIX.csv
https://raw.githubusercontent.com/jiaren19/market-data/main/data/_fundamentals.csv
```

For symbols that start with `^`, encode the caret as `%5E` in the URL (`^VIX` → `%5EVIX.csv`). The filename in the repo itself stays `^VIX.csv`.
