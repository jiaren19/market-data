# market-data

Daily public market-data snapshot for research. Daily bars come from the Yahoo Finance chart API only. Quarterly fundamentals come from the SEC EDGAR companyfacts XBRL API only (not Yahoo). Missing tickers or fundamental fields are listed in `data/_errors.txt` or left blank. No substitute source is used, and no numbers are filled in by hand.

由 Grok Bot 在每個美股交易日收盤後自動抓取並推送。日線只來自 Yahoo Finance，季度財報只來自 SEC EDGAR companyfacts，抓不到的標的會寫進 `data/_errors.txt`，缺值留白，不會用其他來源的數字補上。

## Update schedule / 更新時間

- Full update (`python3 update.py`): daily after the US cash-session close, around **06:30 Asia/Kuala_Lumpur**, Tuesday to Saturday (Monday morning MYT is Sunday in New York, so there is no new US session to fetch).
- 完整更新：每個美股交易日收盤後，約馬來西亞時間 **06:30**（周二至周六）。
- Asia-only update (`python3 update.py --asia`): after the Asian cash close, around **15:00 Asia/Kuala_Lumpur**, Monday to Friday. This overwrites only the 16 Taiwan, Korea, Japan, and `BTC-USD` files; every other CSV is left untouched.
- 亞洲盤更新：亞洲市場收盤後，約馬來西亞時間 **15:00**（周一至周五），只更新台、韓、日標的與 `BTC-USD`。
- The full update also refreshes `data/_fundamentals.csv` when that file is missing or older than 6 hours, so every daily 06:30 MYT run refetches it from EDGAR. A fundamentals-only run is `python3 update.py --fundamentals`. Asia-only runs do not touch fundamentals.
- 完整更新若基本面檔缺失或已超過 6 小時（即每天 06:30 MYT 都會重抓），會一併從 EDGAR 更新 `data/_fundamentals.csv`。也可單獨執行 `python3 update.py --fundamentals`。亞洲盤更新不會改基本面檔。
- Every full update also rebuilds `data/_membership.csv` (index membership history) and re-evaluates `data/_universe.csv` (scan universe) from that day's sources. Re-evaluating daily is cheap because the candidates' Yahoo bars are fetched anyway.
- 每次完整更新都會重建 `data/_membership.csv`（成分股歷史），並用當天的資料重新篩選 `data/_universe.csv`（掃描股票池）。
- Reruns on the same day overwrite the same files and may create a new commit.

## Files / 檔案說明

| Path | What it is |
| --- | --- |
| `data/<SYMBOL>.csv` | Daily bars for one Yahoo Finance symbol. The filename is the exact symbol, including `^` (for example `data/^VIX.csv`, `data/2330.TW.csv`, `data/BTC-USD.csv`). Covers the fixed tickers, current index members, every `_universe.csv` name, and removed index members that Yahoo still serves (see below). Files are never deleted: a name that leaves the universe or an index keeps its file and is still refreshed; if a refresh fails, the last good file stays and its last bar date in `_updated.txt` shows how old it is. |
| `data/_constituents.csv` | Current S&P 500 and Nasdaq-100 members: `symbol,name,sector,index`. `index` is `SP500`, `NDX`, or `both`. Symbols are in Yahoo format (`BRK.B` → `BRK-B`). Sector is the Wikipedia GICS Sector when the name is in the S&P 500. Nasdaq-100-only names have no GICS column on Wikipedia, so that row uses the ICB Industry from the Wikipedia Nasdaq-100 list. |
| `data/_membership.csv` | S&P 500 and Nasdaq-100 membership spells since 2015: `symbol,index,date_added,date_removed,name,note`. One row per spell. See "Index membership history". |
| `data/_universe.csv` | Today's scan universe: `symbol,name,sector,market_cap,avg_dollar_volume_63d`. See "Scan universe". |
| `data/_themes.csv` | Theme map for selected ETFs and focus stocks: `symbol,theme_zh,theme_en`. A symbol may have more than one row if it belongs to more than one theme; existing rows are kept and a new theme is added as an extra row, not an overwrite. This is a static mapping shipped with the daily update, not inferred from prices. Every theme member also has its own `data/<SYMBOL>.csv`. |
| `data/_updated.txt` | UTC and Asia/Kuala_Lumpur timestamps of the last full run, each ticker's last bar date, and (after an Asia-only run) `asia_updated_utc` / `asia_updated_asia_kuala_lumpur`. An `--asia` run updates only the 16 Asia/BTC last-bar lines and the asia timestamp; other lines are kept. |
| `data/_errors.txt` | Problems from the last run, one `key: reason` line each. A bare `SYMBOL:` key is a fixed ticker or index member that failed (these make the run exit non-zero). Informational keys: `removed:SYMBOL` (removed index member Yahoo no longer serves), `ticker_reuse:SYMBOL` (ticker now used by a different listing, or the name no longer matches; no data saved for the removed company), `universe_candidate:SYMBOL` (screener name Yahoo could not serve), `stale:SYMBOL` (refresh failed, old file kept), `membership:NN` (spell the change tables could not close), `fundamentals:SYMBOL`. An `--asia` run updates or clears error lines only for its 16 tickers; other lines are kept. |
| `data/_fundamentals.csv` | Quarterly fundamentals from 2015-01-01 onward for every current S&P 500 and Nasdaq-100 constituent, plus the theme stocks listed below. See the Fundamentals section for columns, sources, and what `report_date` means. |
| `update.py` | The script that fetches, writes, commits, and pushes. |
| `fundamentals.py` | SEC EDGAR helpers used by `update.py` to build `_fundamentals.csv`. |
| `membership.py` | Rebuilds index membership spells from the Wikipedia change tables. |
| `universe.py` | Nasdaq screener download, security-type filter, and 63-day dollar-volume rule. |

成分股名單來自維基百科：[List of S&P 500 companies](https://en.wikipedia.org/wiki/List_of_S%26P_500_companies)、[List of NASDAQ-100 companies](https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies)。美股代號的點號會改成 Yahoo 格式（`BRK.B` → `BRK-B`）。

## CSV columns / 欄位

`date, open, high, low, close, adj_close, volume`

- `date` is the exchange-local trading date (`YYYY-MM-DD`), from 2015-01-01 onward. Names listed after that date start at their first Yahoo bar (the listing date).
- Prices are unadjusted OHLC; `adj_close` is Yahoo's split/dividend-adjusted close. If Yahoo omits `adj_close` for a bar, that cell is left blank.
- `volume` is left blank when Yahoo does not provide it (some indices).
- Rows with a missing close are dropped. No interpolated or filled values.
- Yahoo recomputes `adj_close` on every request, so its last digits jitter (about 1e-7 relative) from run to run. When a newly fetched value is within 1e-6 relative of the stored value, the stored text is kept, so git diffs show only real changes (new bars, dividend or split re-adjustments, corrected volume).
- Yahoo sometimes returns a null close for the most recent session for a few hours. A bar already stored that is newer than Yahoo's latest returned bar is kept rather than deleted.
- `adj_close` 每次抓都會有極小的浮點誤差（約千萬分之一），誤差在百萬分之一以內就沿用原值，避免整個檔案每天被改寫；Yahoo 偶爾暫時把最新一天的收盤給成空值，已存的那一天不會被刪。

## Fundamentals / 季度財報

`data/_fundamentals.csv` columns:

`symbol, fiscal_quarter_end, report_date, revenue, gross_profit, eps_diluted, net_income, date_type, note`

Universe: every current S&P 500 and Nasdaq-100 constituent, every `_universe.csv` name, removed index members whose price file passed the same-company check, plus these theme stocks (deduplicated): `MU` `SNDK` `WDC` `STX` `COHR` `LITE` `CIEN` `GLW` `AAOI` `FN` `BE` `VST` `CEG` `GEV` `ETN` `VRT` `PWR` `NRG` `AMAT` `LRCX` `KLAC` `ASML` `TER` `AEHR` `FORM` `ONTO` `ACMR` `CAMT` `CRWV` `NBIS` `IREN` `APLD` `CIFR` `WULF` `CORZ`.

Source (one source per field, never mixed with Yahoo or Nasdaq numbers):

- Values (`revenue`, `gross_profit`, `eps_diluted`, `net_income`) come from the [SEC EDGAR companyfacts XBRL API](https://data.sec.gov/api/xbrl/companyfacts/). For each field a fixed US-GAAP / IFRS tag priority is used (see `fundamentals.py`). Companies that changed tags over time (for example `SalesRevenueNet` before 2018 and `RevenueFromContractWithCustomerExcludingAssessedTax` after) may switch tags at the quarter when the newer tag begins; both tags are still EDGAR companyfacts. A value first appears as reported in the original 10-Q / 10-K / 20-F (as-first-reported, not a later restatement, and not retroactively split-adjusted). Missing values are left blank. Banks usually have no `GrossProfit` tag; that cell stays blank. Diluted EPS is never replaced with basic EPS.
- `fiscal_quarter_end` is the issuer's fiscal period end date (`YYYY-MM-DD`), not a calendar quarter label. Apple's fiscal year ends in September, so a row ending `2025-12-27` is Apple's fiscal Q1, not calendar Q4 in name only.
- `report_date` is the public earnings publication date, not the quarter end. It is the filing date of the 8-K that includes Item 2.02 (Results of Operations and Financial Condition), which is the earnings press-release date on EDGAR, almost always the same calendar day as the announcement. `date_type` is `8-K` in that case. For foreign issuers that file 6-K / 20-F instead of 8-K / 10-Q (for example `ASML`, `CAMT`, `NBIS`, `PDD`), the pipeline tries to use the 6-K filing date when a 6-K can be matched to that quarter (`date_type=6-K`). Many of those issuers do not tag quarterly income-statement numbers in companyfacts, so the symbol is listed under `fundamentals:SYMBOL` in `data/_errors.txt` and no invented numbers are written.
- The 10-Q / 10-K / 20-F `filed` date is later than the press release and is **not** used as `report_date` unless no 8-K Item 2.02 or matching 6-K is found, the periodic report itself was filed within 90 days of the quarter end, and `date_type` is set to `filing_fallback`. If even that is not safe (for example a comparative quarter that only appears later in a post-IPO 10-Q), `report_date` is left blank. Backtests should treat a blank or `filing_fallback` date with care.
- Q4 is often filed only as a full-year total in the 10-K. When a 90-day Q4 fact is missing but Q1–Q3 and the annual total exist for the same tag, `revenue`, `gross_profit`, and `net_income` for that Q4 are derived as annual minus the three quarters and `note` contains `derived_q4_...`. Diluted EPS is **not** derived this way, because annual EPS is not the sum of quarterly EPS when the share count changes; Q4 `eps_diluted` is left blank unless a quarterly EPS fact exists.
- Units are USD whenever EDGAR provides USD. A non-USD amount is still written, and `note` records the unit (for example `revenue_unit:EUR`).
- Refresh: EDGAR submissions (filing lists, used for `report_date`) are refetched for every symbol on each full run. The much larger companyfacts file is refetched only when a 10-Q, 10-K, 20-F, 40-F, 6-K or 8-K (or amendment) was filed since the day before the cached copy was downloaded, or when the cached copy is older than 30 days. A new quarter therefore appears on the first full run after its filing, without downloading every companyfacts file daily.
- 更新方式：每次完整更新都重抓所有公司的 EDGAR 申報清單；體積大的 companyfacts 只在有新的 10-Q/10-K/20-F/40-F/6-K/8-K 申報（或快取超過 30 天）時才重抓，新財報在申報後的第一次完整更新就會出現。
- Many foreign issuers in the universe (20-F / 40-F filers, ADRs) do not tag quarterly numbers in companyfacts; they appear under `fundamentals:SYMBOL` in `_errors.txt` with no rows. Companies without a ticker in SEC's `company_tickers.json` (for example most delisted removed members) cannot be mapped to a CIK and are listed as `no CIK`.
- History starts at fiscal quarter ends on or after 2015-01-01. Names that began reporting later (spin-offs, IPOs) start at their first EDGAR quarterly fact. Pre-listing comparative quarters may have values but a blank `report_date`.

## Index membership history / 成分股歷史

`data/_membership.csv` columns: `symbol, index, date_added, date_removed, name, note`

- One row per membership spell. `index` is `SP500` or `NDX`. A company that joined, left, and rejoined has two rows. A company in both indexes has a row for each.
- `date_added` / `date_removed` are the effective dates in the source change tables. The company is a member from `date_added` and is no longer a member on `date_removed`. Current members have a blank `date_removed`.
- Coverage: every spell that was active on or after 2015-01-01 (current members, plus everyone removed since 2015-01-01). To get the members on a date D, take rows with `date_added <= D` (or blank) and `date_removed > D` (or blank).
- Sources: [Historical components of the S&P 500](https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500) and [Historical components of the Nasdaq-100](https://en.wikipedia.org/wiki/Historical_components_of_the_Nasdaq-100) (change tables with date, added ticker, removed ticker, reason), walked backwards from today's [S&P 500](https://en.wikipedia.org/wiki/List_of_S%26P_500_companies) and [Nasdaq-100](https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies) lists. The S&P 500 result is cross-checked against the [fja05680/sp500](https://github.com/fja05680/sp500) daily component snapshots (`S&P 500 Historical Components & Changes (Updated).csv`).
- Members since before 2015: for current S&P 500 members with no add event in the change table, `date_added` is the "Date added" column of the current Wikipedia list (`note=date_added_from_current_table`). Where fja05680 already lists that ticker as a member before that date (for example `D`, `SRE`, `TROW`), the Wikipedia date is contradicted, `date_added` is left blank, and `note` says so. Removed members whose add event is not in the table, and current Nasdaq-100 members (the Nasdaq-100 change table starts on 2007-02-01 and the current list has no date column), have a blank `date_added` and `note=date_added_not_in_source_table`. Treat a blank `date_added` as "member since before the source starts".
- Tickers: Yahoo format (`BRK.B` → `BRK-B`). Current members use today's ticker for the whole spell (for example Meta's spell is `META` from 2013-12-23, even though it traded as `FB` until 2022), so the symbol matches its `data/<SYMBOL>.csv`. Removed members use the ticker in the change table at removal. When a company changed ticker during a spell and the table added it under the old ticker, the spell is matched by company name and `note` records `added_as:OLD`. Add events that cannot be matched to any later removal or current member are listed as `membership:NN` in `_errors.txt` instead of creating a spell with an invented end date.
- Known gaps: Wikipedia calls its S&P 500 list "selected changes"; a few additions and removals are missing, so a reconstructed date can hold a few names more or fewer than the real index (checked on 2026-11-12 against fja05680: the member count differed by a median of 2 and at most 4 names per date since 2015). Same-day share-class and ticker swaps are not split into separate spells. Nasdaq-100 has no second source to cross-check.

成分股歷史：每一段「在指數內的期間」一列，`date_removed` 空白代表目前仍是成分股。來源是維基百科兩個指數的歷史成分股變動表，從今天的成分股往回推；S&P 500 另外用 fja05680/sp500 的每日成分股快照交叉比對。找不到加入日期就留白並在 `note` 說明，不會自己補日期。現有成分股整段都用今天的代號（例如 Meta 從 2013 年起都記 `META`），已剔除的公司用剔除當時的代號。

## Removed members' prices / 已剔除成分股的價格

- For every ticker removed from either index since 2015-01-01, the full run asks Yahoo for daily bars in the same CSV format. If the ticker is still a current listing under the same company name (for example it is in the universe), that file serves both.
- A removed ticker is saved only when Yahoo's series is plausibly the same company: its first trade date is not after the removal, it starts no more than 30 days after a post-2015 `date_added`, it does not end more than 10 days before the removal, and Yahoo's company name matches the change-table name. Otherwise nothing is saved and `_errors.txt` gets a `ticker_reuse:SYMBOL` line (ticker now used by another company, or renamed so the names no longer match). Removed tickers Yahoo no longer serves (most acquisitions and delistings) are listed as `removed:SYMBOL`.
- This means backtests still carry survivorship bias for companies that were acquired or delisted: their prices are not available from Yahoo.
- 2015 年後被剔除的代號，Yahoo 還有資料而且確認是同一家公司才存檔；代號已被別家公司沿用或改名對不上，就寫進 `_errors.txt` 的 `ticker_reuse:`，不存錯誤資料。被併購或下市、Yahoo 已沒有資料的寫在 `removed:`。

## Scan universe / 掃描股票池

`data/_universe.csv` columns: `symbol, name, sector, market_cap, avg_dollar_volume_63d`

- Candidates, `name`, `sector`, and `market_cap` (USD) come from the Nasdaq stock screener API (`api.nasdaq.com/api/screener/stocks`, all NASDAQ, NYSE and NYSE American listings), downloaded on each full run. `sector` is Nasdaq's sector label, not GICS. The download time is `universe_as_of_*` in `_updated.txt`.
- Filter: `market_cap >= 2,000,000,000` USD, and `avg_dollar_volume_63d >= 20,000,000` USD, where `avg_dollar_volume_63d` is the mean of `close × volume` over the last 63 Yahoo daily bars, computed here from the bars (not taken from Nasdaq). Names with fewer than 63 bars (recent listings) do not qualify yet.
- Security types: common stock, ADRs/ADSs, and LP common units are kept. Preferreds and depositary shares of preferreds, notes/debentures/bonds, warrants, rights, units (including tangible equity and corporate units), SPACs (blank-check industry or "Acquisition Corp" names), ETFs and closed-end funds are excluded by name and industry, and symbols with suffixes such as `.WS`. Class shares are separate rows (`BRK-A`, `BRK-B`, `GOOG`, `GOOGL`). Nasdaq symbols are converted to Yahoo format (`BRK/B` → `BRK-B`).
- The list is today's qualifiers only. Fixed tickers and current index members are always fetched whether or not they qualify. A name that drops out keeps its price file and is still refreshed, so `data/` holds more symbols than `_universe.csv`.
- If the screener download fails, the previous list is rescored with the new bars and `_errors.txt` has a `_universe` line.

掃描股票池：候選名單、公司名稱、產業（Nasdaq 分類，不是 GICS）和市值來自 Nasdaq 選股 API；條件是市值至少 20 億美元，而且最近 63 個交易日的平均成交金額（收盤價 × 成交量，用 Yahoo 日線自己算）至少 2,000 萬美元。排除特別股、債券、權證、認股權、單位、SPAC、ETF 與封閉式基金。每次完整更新都重新篩選；掉出名單的股票檔案保留並繼續更新。

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
https://raw.githubusercontent.com/jiaren19/market-data/main/data/_membership.csv
https://raw.githubusercontent.com/jiaren19/market-data/main/data/_universe.csv
```

For symbols that start with `^`, encode the caret as `%5E` in the URL (`^VIX` → `%5EVIX.csv`). The filename in the repo itself stays `^VIX.csv`.
