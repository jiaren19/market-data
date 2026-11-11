# market-data

Daily public market-data snapshot for research. Source: Yahoo Finance chart API only. Missing tickers are listed in `data/_errors.txt`. No substitute source is used, and no numbers are filled in by hand.

由 Grok Bot 在每個美股交易日收盤後自動抓取並推送。資料只來自 Yahoo Finance，抓不到的標的會寫進 `data/_errors.txt`，不會用其他來源的數字補上。

## Update schedule / 更新時間

- Daily after the US cash-session close, around **06:30 Asia/Kuala_Lumpur**, Tuesday to Saturday (Monday morning MYT is Sunday in New York, so there is no new US session to fetch).
- 每個美股交易日收盤後，約马来西亚时间 **06:30**（周二至周六）更新。
- Reruns on the same day overwrite the same files and may create a new commit.

## Files / 檔案說明

| Path | What it is |
| --- | --- |
| `data/<SYMBOL>.csv` | Daily bars for one Yahoo Finance symbol. The filename is the exact symbol, including `^` (for example `data/^VIX.csv`, `data/2330.TW.csv`, `data/BTC-USD.csv`). |
| `data/_constituents.csv` | Current S&P 500 and Nasdaq-100 members: `symbol,name,sector,index`. `index` is `SP500`, `NDX`, or `both`. Symbols are in Yahoo format (`BRK.B` → `BRK-B`). Sector is the Wikipedia GICS Sector when the name is in the S&P 500. Nasdaq-100-only names have no GICS column on Wikipedia, so that row uses the ICB Industry from the Wikipedia Nasdaq-100 list. |
| `data/_updated.txt` | UTC and Asia/Kuala_Lumpur timestamps of this run, plus each ticker's last bar date. |
| `data/_errors.txt` | Tickers that could not be fetched, with the reason. |
| `update.py` | The script that fetches, writes, commits, and pushes. |

成分股名單來自維基百科：[List of S&P 500 companies](https://en.wikipedia.org/wiki/List_of_S%26P_500_companies)、[List of NASDAQ-100 companies](https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies)。美股代號的點號會改成 Yahoo 格式（`BRK.B` → `BRK-B`）。

## CSV columns / 欄位

`date, open, high, low, close, adj_close, volume`

- `date` is the exchange-local trading date (`YYYY-MM-DD`), from 2019-01-01 onward.
- Prices are unadjusted OHLC; `adj_close` is Yahoo's split/dividend-adjusted close. If Yahoo omits `adj_close` for a bar, that cell is left blank.
- `volume` is left blank when Yahoo does not provide it (some indices).
- Rows with a missing close are dropped. No interpolated or filled values.

## Fixed tickers (always fetched) / 固定標的

- Market / benchmarks: `SPY` `QQQ` `RSP` `IWM` `SOXX` `SMH` `IGV` `BTC-USD`
- US sectors: `XLK` `XLV` `XLF` `XLY` `XLP` `XLE` `XLI` `XLB` `XLU` `XLRE` `XLC`
- Sentiment / rates: `^VIX` `^VIX3M` `^TNX` `DX-Y.NYB`
- Taiwan: `^TWII` `2330.TW` `2454.TW` `2303.TW` `2408.TW` `3711.TW`
- Korea: `^KS11` `005930.KS` `000660.KS`
- Japan: `^N225` `8035.T` `6857.T` `6146.T` `6920.T` `285A.T`
- Plus every current S&P 500 and Nasdaq-100 constituent (deduplicated).

## Raw URLs / 直接下載

```
https://raw.githubusercontent.com/jiaren19/market-data/main/data/<SYMBOL>.csv
```

Examples:

```
https://raw.githubusercontent.com/jiaren19/market-data/main/data/SPY.csv
https://raw.githubusercontent.com/jiaren19/market-data/main/data/2330.TW.csv
https://raw.githubusercontent.com/jiaren19/market-data/main/data/%5EVIX.csv
```

For symbols that start with `^`, encode the caret as `%5E` in the URL (`^VIX` → `%5EVIX.csv`). The filename in the repo itself stays `^VIX.csv`.
