#!/usr/bin/env python3
"""Fetch daily Yahoo Finance bars and push them to jiaren19/market-data.

Run:
  python3 /workspace/market-data/update.py          # full update
  python3 /workspace/market-data/update.py --asia   # Taiwan, Korea, Japan, BTC-USD only

Auth: reads GITHUB_TOKEN from the environment at runtime. The token is never
written to disk, committed, placed in a git remote URL, or printed.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import stat
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import pandas as pd
except ImportError as exc:
    sys.exit(f"pandas is required to parse Wikipedia constituent tables: {exc}")

ROOT = Path(__file__).resolve().parent
REPO = ROOT / "repo"
DATA = REPO / "data"
ASKPASS = ROOT / ".git-askpass"
EDGAR_CACHE = ROOT / "cache" / "edgar"
GITHUB_REPO = "jiaren19/market-data"
BRANCH = "main"
HISTORY_START = date(2015, 1, 1)
PERIOD1 = int(datetime(2015, 1, 1, tzinfo=timezone.utc).timestamp())
MYT = ZoneInfo("Asia/Kuala_Lumpur")

YAHOO_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
WIKI_UA = (
    "jiaren19-market-data/1.0 "
    "(https://github.com/jiaren19/market-data; daily public market-data snapshot)"
)

WORKERS = 6
MAX_RETRIES = 5
BASE_SLEEP = 0.15  # polite pause after each Yahoo request
WIKI_SPX = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
WIKI_NDX = "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies"
WIKI_SPX_HIST = "https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500"
WIKI_NDX_HIST = "https://en.wikipedia.org/wiki/Historical_components_of_the_Nasdaq-100"
FJA_SP500_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/master/"
    "S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv"
)
# Size guard before any push (GitHub: 100 MB hard / 50 MB warning per file; ~1 GB soft repo).
MAX_FILE_BYTES = 45 * 1024 * 1024
MAX_DATA_BYTES = 900 * 1024 * 1024
# Raw Wikipedia tables from fetch_constituents(), reused for membership history.
_WIKI_TABLES: dict = {}

# Claude's fixed ticker list, in listed groups. Order is for documentation only.
FIXED_TICKERS = [
    # 大盤與對照
    "SPY", "QQQ", "RSP", "IWM", "SOXX", "SMH", "IGV", "BTC-USD",
    # 板塊
    "XLK", "XLV", "XLF", "XLY", "XLP", "XLE", "XLI", "XLB", "XLU", "XLRE", "XLC",
    # 情緒與利率
    "^VIX", "^VIX3M", "^TNX", "DX-Y.NYB",
    # 台灣
    "^TWII", "2330.TW", "2454.TW", "2303.TW", "2408.TW", "3711.TW",
    # 韓國
    "^KS11", "005930.KS", "000660.KS",
    # 日本
    "^N225", "8035.T", "6857.T", "6146.T", "6920.T", "285A.T",
    # 主題 ETF 與焦點個股（與成分股重疊時會去重）
    "AIQ", "BOTZ", "QTUM", "CIBR", "GDX", "SIL", "SLX", "URA",
    "JETS", "IYT", "ITA", "TAN", "OIH", "IHI", "XBI", "KBE",
    "KRE", "XRT", "ITB", "KWEB", "SOCL", "IYZ", "IBIT", "WGMI",
    "MU", "SNDK", "WDC", "STX",
    # 光通訊、電力、半導體設備與測試、Neocloud（與成分股重疊時會去重）
    "COHR", "LITE", "CIEN", "GLW", "AAOI", "FN",
    "BE", "VST", "CEG", "GEV", "ETN", "VRT", "PWR", "NRG",
    "AMAT", "LRCX", "KLAC", "ASML", "TER", "AEHR", "FORM", "ONTO", "ACMR", "CAMT",
    "CRWV", "NBIS", "IREN", "APLD", "CIFR", "WULF", "CORZ",
]

# Theme map written to data/_themes.csv.
# A symbol may appear in more than one row if it belongs to more than one theme;
# existing rows are kept and new themes are appended, never overwritten.
# SMH/SOXX/IGV are already in the original fixed list; they are included here too.
THEMES = [
    ("SMH", "半導體", "Semiconductors"),
    ("SOXX", "半導體", "Semiconductors"),
    ("IGV", "軟體", "Software"),
    ("AIQ", "AI", "AI"),
    ("BOTZ", "機器人", "Robotics"),
    ("QTUM", "量子", "Quantum"),
    ("CIBR", "資安", "Cybersecurity"),
    ("GDX", "金礦", "Gold miners"),
    ("SIL", "銀礦", "Silver miners"),
    ("SLX", "鋼鐵", "Steel"),
    ("URA", "鈾與核能", "Uranium & nuclear"),
    ("JETS", "航空", "Airlines"),
    ("IYT", "運輸", "Transportation"),
    ("ITA", "航太國防", "Aerospace & defense"),
    ("TAN", "太陽能", "Solar"),
    ("OIH", "油服", "Oil services"),
    ("IHI", "醫療器材", "Medical devices"),
    ("XBI", "生技", "Biotech"),
    ("KBE", "銀行", "Banks"),
    ("KRE", "區域銀行", "Regional banks"),
    ("XRT", "零售", "Retail"),
    ("ITB", "建商", "Homebuilders"),
    ("KWEB", "中概網路", "China internet"),
    ("SOCL", "社群", "Social media"),
    ("IYZ", "電信", "Telecom"),
    ("IBIT", "比特幣", "Bitcoin"),
    ("WGMI", "比特幣礦工", "Bitcoin miners"),
    ("MU", "記憶體", "Memory"),
    ("SNDK", "記憶體", "Memory"),
    ("WDC", "記憶體", "Memory"),
    ("STX", "記憶體", "Memory"),
    # 光通訊 / Optical networking
    ("COHR", "光通訊", "Optical networking"),
    ("LITE", "光通訊", "Optical networking"),
    ("CIEN", "光通訊", "Optical networking"),
    ("GLW", "光通訊", "Optical networking"),
    ("AAOI", "光通訊", "Optical networking"),
    ("FN", "光通訊", "Optical networking"),
    # 電力 / Power
    ("BE", "電力", "Power"),
    ("VST", "電力", "Power"),
    ("CEG", "電力", "Power"),
    ("GEV", "電力", "Power"),
    ("ETN", "電力", "Power"),
    ("VRT", "電力", "Power"),
    ("PWR", "電力", "Power"),
    ("NRG", "電力", "Power"),
    # 半導體設備與測試 / Semi equipment & test
    ("AMAT", "半導體設備與測試", "Semi equipment & test"),
    ("LRCX", "半導體設備與測試", "Semi equipment & test"),
    ("KLAC", "半導體設備與測試", "Semi equipment & test"),
    ("ASML", "半導體設備與測試", "Semi equipment & test"),
    ("TER", "半導體設備與測試", "Semi equipment & test"),
    ("AEHR", "半導體設備與測試", "Semi equipment & test"),
    ("FORM", "半導體設備與測試", "Semi equipment & test"),
    ("ONTO", "半導體設備與測試", "Semi equipment & test"),
    ("ACMR", "半導體設備與測試", "Semi equipment & test"),
    ("CAMT", "半導體設備與測試", "Semi equipment & test"),
    # Neocloud
    ("CRWV", "Neocloud", "Neocloud"),
    ("NBIS", "Neocloud", "Neocloud"),
    ("IREN", "Neocloud", "Neocloud"),
    ("APLD", "Neocloud", "Neocloud"),
    ("CIFR", "Neocloud", "Neocloud"),
    ("WULF", "Neocloud", "Neocloud"),
    ("CORZ", "Neocloud", "Neocloud"),
]

# Asia-only mode (--asia): Taiwan, Korea, Japan, and BTC-USD. Full mode still
# fetches these as part of FIXED_TICKERS; this list is the --asia universe.
ASIA_TICKERS = [
    "^TWII", "2330.TW", "2454.TW", "2303.TW", "2408.TW", "3711.TW",
    "^KS11", "005930.KS", "000660.KS",
    "^N225", "8035.T", "6857.T", "6146.T", "6920.T", "285A.T",
    "BTC-USD",
]

# Shared 429/503 backoff so concurrent workers don't keep hammering Yahoo.
_rate_lock = threading.Lock()
_rate_resume = 0.0


def log(msg: str) -> None:
    print(msg, flush=True)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def http_get(url: str, ua: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def wiki_symbol_to_yahoo(raw: str) -> str:
    """Wikipedia uses BRK.B; Yahoo uses BRK-B. Strip footnote markers."""
    s = str(raw).strip().split("[")[0].strip()
    s = s.replace("\xa0", "").strip()
    return s.replace(".", "-")


def fetch_constituents() -> list[dict]:
    """Current S&P 500 and Nasdaq-100 members from Wikipedia.

    Sector: GICS Sector from the S&P 500 list when the name is in the S&P 500.
    Nasdaq-100-only names have no GICS column on Wikipedia, so ICB Industry
    from the Nasdaq-100 list is stored instead (documented in README).
    """
    import io

    spx_html = http_get(WIKI_SPX, WIKI_UA, timeout=45)
    ndx_html = http_get(WIKI_NDX, WIKI_UA, timeout=45)
    spx = pd.read_html(io.BytesIO(spx_html))[0]
    ndx = pd.read_html(io.BytesIO(ndx_html))[0]
    _WIKI_TABLES["spx"] = spx
    _WIKI_TABLES["ndx"] = ndx

    if "Symbol" not in spx.columns or "Security" not in spx.columns:
        raise RuntimeError(f"Unexpected S&P 500 table columns: {list(spx.columns)}")
    ndx_sym_col = "Ticker" if "Ticker" in ndx.columns else "Symbol"
    ndx_name_col = "Company" if "Company" in ndx.columns else "Security"
    if ndx_sym_col not in ndx.columns:
        raise RuntimeError(f"Unexpected Nasdaq-100 table columns: {list(ndx.columns)}")

    spx_gics_col = "GICS Sector" if "GICS Sector" in spx.columns else None
    ndx_icb_col = None
    for c in ndx.columns:
        if "ICB Industry" in str(c) or "GICS Sector" in str(c) or str(c) == "Sector":
            ndx_icb_col = c
            break

    by_symbol: dict[str, dict] = {}

    for _, row in spx.iterrows():
        sym = wiki_symbol_to_yahoo(row["Symbol"])
        if not sym:
            continue
        sector = ""
        if spx_gics_col is not None and pd.notna(row[spx_gics_col]):
            sector = str(row[spx_gics_col]).strip()
        by_symbol[sym] = {
            "symbol": sym,
            "name": str(row["Security"]).strip(),
            "sector": sector,
            "index": "SP500",
            "sector_source": "gics" if sector else "",
        }

    for _, row in ndx.iterrows():
        sym = wiki_symbol_to_yahoo(row[ndx_sym_col])
        if not sym:
            continue
        name = str(row[ndx_name_col]).strip()
        icb = ""
        if ndx_icb_col is not None and pd.notna(row[ndx_icb_col]):
            icb = str(row[ndx_icb_col]).strip()
        if sym in by_symbol:
            by_symbol[sym]["index"] = "both"
            # keep GICS sector and S&P company name
        else:
            by_symbol[sym] = {
                "symbol": sym,
                "name": name,
                "sector": icb,  # ICB stand-in; see README
                "index": "NDX",
                "sector_source": "icb" if icb else "",
            }

    rows = [by_symbol[k] for k in sorted(by_symbol)]
    if len(rows) < 400:
        raise RuntimeError(f"Constituent list too short ({len(rows)}); refusing to proceed")
    return rows


def _wait_for_rate_window() -> None:
    while True:
        with _rate_lock:
            wait = _rate_resume - time.time()
        if wait <= 0:
            return
        time.sleep(min(wait, 1.0))


def _trip_rate_limit(seconds: float) -> None:
    with _rate_lock:
        global _rate_resume
        _rate_resume = max(_rate_resume, time.time() + seconds)


def fetch_yahoo_bars(symbol: str) -> list[dict]:
    """Daily OHLCV + adj_close from 2015-01-01 (or the listing date if later). Raises on failure."""
    return fetch_yahoo(symbol)[0]


def fetch_yahoo(symbol: str) -> tuple[list[dict], dict]:
    """Like fetch_yahoo_bars, also returning Yahoo's chart `meta` (names, first trade date)."""
    period2 = int(time.time()) + 60
    quoted = urllib.parse.quote(symbol, safe="")
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{quoted}"
        f"?period1={PERIOD1}&period2={period2}&interval=1d&events=div,splits"
    )
    last_err: Exception | str = "unknown error"
    body = b""
    for attempt in range(1, MAX_RETRIES + 1):
        _wait_for_rate_window()
        try:
            body = http_get(url, YAHOO_UA, timeout=30)
            time.sleep(BASE_SLEEP)
            break
        except urllib.error.HTTPError as exc:
            last_err = exc
            snippet = ""
            try:
                snippet = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if exc.code in (429, 500, 502, 503, 504) or exc.code == 401:
                backoff = min(60, (2 ** attempt)) + (0.2 * attempt)
                if exc.code == 429:
                    backoff = min(90, 8 * attempt)
                _trip_rate_limit(backoff)
                log(f"  retry {symbol} HTTP {exc.code} attempt {attempt}/{MAX_RETRIES}, backoff {backoff:.0f}s")
                time.sleep(backoff)
                continue
            raise RuntimeError(f"HTTP {exc.code} {exc.reason}" + (f" ({snippet})" if snippet else "")) from exc
        except Exception as exc:
            last_err = exc
            backoff = min(30, 2 ** attempt)
            log(f"  retry {symbol} {type(exc).__name__} attempt {attempt}/{MAX_RETRIES}")
            time.sleep(backoff)
    else:
        raise RuntimeError(f"failed after {MAX_RETRIES} retries: {last_err}")

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"invalid JSON from Yahoo: {exc}") from exc

    chart = payload.get("chart") or {}
    err = chart.get("error")
    result = chart.get("result")
    if not result:
        if err:
            raise RuntimeError(f"Yahoo error: {err}")
        raise RuntimeError("Yahoo returned no result")

    r0 = result[0]
    timestamps = r0.get("timestamp") or []
    if not timestamps:
        raise RuntimeError("Yahoo returned no timestamps")

    indicators = r0.get("indicators") or {}
    quotes = (indicators.get("quote") or [{}])[0]
    adj_list = None
    adj_block = indicators.get("adjclose")
    if adj_block and isinstance(adj_block, list) and adj_block[0]:
        adj_list = adj_block[0].get("adjclose")

    opens = quotes.get("open") or [None] * len(timestamps)
    highs = quotes.get("high") or [None] * len(timestamps)
    lows = quotes.get("low") or [None] * len(timestamps)
    closes = quotes.get("close") or [None] * len(timestamps)
    volumes = quotes.get("volume") or [None] * len(timestamps)
    if adj_list is None:
        adj_list = [None] * len(timestamps)

    gmtoffset = int((r0.get("meta") or {}).get("gmtoffset") or 0)
    rows: list[dict] = []
    n = min(len(timestamps), len(opens), len(highs), len(lows), len(closes), len(volumes), len(adj_list))
    for i in range(n):
        close = closes[i]
        if close is None:
            continue
        d = datetime.fromtimestamp(timestamps[i] + gmtoffset, tz=timezone.utc).date()
        if d < HISTORY_START:
            continue
        rows.append(
            {
                "date": d.isoformat(),
                "open": opens[i],
                "high": highs[i],
                "low": lows[i],
                "close": close,
                "adj_close": adj_list[i],
                "volume": volumes[i],
            }
        )
    if not rows:
        raise RuntimeError("no daily bars on or after 2015-01-01")
    return rows, (r0.get("meta") or {})


def _fmt_num(val) -> str:
    if val is None:
        return ""
    if isinstance(val, bool):
        return ""
    if isinstance(val, (int,)) and not isinstance(val, bool):
        return str(val)
    try:
        f = float(val)
    except (TypeError, ValueError):
        return ""
    if f != f:  # NaN
        return ""
    if f == int(f) and abs(f) < 1e12:
        # keep a trailing .0? volume should be int; prices like 100.0 stay 100.0? 
        # For volume we handle separately. Here integer-valued floats keep .0 stripped only
        # if they are whole; but 100 and 100.0 are both fine. Use compact format.
        pass
    s = f"{f:.8f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _fmt_volume(val) -> str:
    if val is None:
        return ""
    try:
        f = float(val)
    except (TypeError, ValueError):
        return ""
    if f != f:
        return ""
    return str(int(round(f)))


CSV_HEADER = ["date", "open", "high", "low", "close", "adj_close", "volume"]
STICKY_REL_TOL = 1e-6


def _format_row(r: dict) -> list[str]:
    return [
        r["date"],
        _fmt_num(r["open"]),
        _fmt_num(r["high"]),
        _fmt_num(r["low"]),
        _fmt_num(r["close"]),
        _fmt_num(r["adj_close"]),
        _fmt_volume(r["volume"]),
    ]


def _same_within_tol(old: list[str], new: list[str]) -> bool:
    if len(old) != len(new):
        return False
    for a, b in zip(old, new):
        if a == b:
            continue
        if not a or not b:
            return False
        try:
            fa, fb = float(a), float(b)
        except ValueError:
            return False
        if abs(fa - fb) > STICKY_REL_TOL * max(abs(fa), abs(fb), 1e-12):
            return False
    return True


def read_csv_lines(path: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if not path.is_file():
        return out
    with path.open(encoding="utf-8", newline="") as fh:
        rd = csv.reader(fh)
        next(rd, None)
        for row in rd:
            if row:
                out[row[0]] = row
    return out


def write_csv(path: Path, rows: list[dict]) -> str:
    """Write bars. Yahoo re-derives adj_close each request, so the last digits jitter
    (relative ~1e-7) between runs. A cell whose new value is within 1e-6 relative of
    the value already stored keeps the stored text, so git only sees real changes
    (new bars, dividend/split re-adjustments, corrected volumes)."""
    old = read_csv_lines(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(CSV_HEADER)
        for r in rows:
            new = _format_row(r)
            prev = old.get(new[0])
            if prev is not None and prev != new and _same_within_tol(prev, new):
                new = prev
            w.writerow(new)
        # Yahoo sometimes returns a null close for the latest session for a while
        # after the close. Keep bars already stored that are newer than the last
        # bar Yahoo returned this time, instead of deleting them.
        last_new = rows[-1]["date"] if rows else ""
        last_written = last_new
        for d in sorted(k for k in old if k > last_new):
            w.writerow(old[d])
            last_written = d
    tmp.replace(path)
    return last_written


def first_csv_date(path: Path) -> str | None:
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as fh:
        fh.readline()
        line = fh.readline()
    return line.split(",", 1)[0] if line else None


def write_constituents(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["symbol", "name", "sector", "index"])
        for r in rows:
            w.writerow([r["symbol"], r["name"], r["sector"], r["index"]])
    tmp.replace(path)


def write_themes(path: Path) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["symbol", "theme_zh", "theme_en"])
        for symbol, theme_zh, theme_en in THEMES:
            w.writerow([symbol, theme_zh, theme_en])
    tmp.replace(path)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch Yahoo Finance daily bars and push to jiaren19/market-data."
    )
    parser.add_argument(
        "--asia",
        action="store_true",
        help="Fetch only the 16 Taiwan, Korea, Japan, and BTC-USD tickers; leave other CSVs untouched.",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="Build everything in the working copy but do not commit or push (for testing).",
    )
    parser.add_argument(
        "--fundamentals",
        action="store_true",
        help="Refresh data/_fundamentals.csv from SEC EDGAR and push; do not refetch price bars.",
    )
    return parser.parse_args(argv)


def parse_updated(path: Path) -> tuple[list[str], dict[str, str]]:
    """Return (header lines, symbol -> last bar date) from an existing _updated.txt."""
    headers: list[str] = []
    last_dates: dict[str, str] = {}
    if not path.is_file():
        return headers, last_dates
    in_dates = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if line.startswith("# symbol last_bar_date"):
            in_dates = True
            continue
        if in_dates:
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                last_dates[parts[0]] = parts[1]
            continue
        headers.append(line)
    return headers, last_dates


def upsert_header(headers: list[str], key: str, value: str) -> list[str]:
    """Replace or insert a 'key: value' header line, preserving order."""
    prefix = f"{key}:"
    out: list[str] = []
    found = False
    for line in headers:
        if line.startswith(prefix):
            if not found:
                out.append(f"{key}: {value}")
                found = True
            # drop duplicate keys
        else:
            out.append(line)
    if not found:
        # Place asia_* timestamps just after the main updated_* lines if present.
        inserted = False
        result: list[str] = []
        last_updated_idx = -1
        for i, line in enumerate(out):
            if (
                line.startswith("updated_utc:")
                or line.startswith("updated_asia_kuala_lumpur:")
                or line.startswith("asia_updated_")
            ):
                last_updated_idx = i
        if last_updated_idx >= 0:
            result = out[: last_updated_idx + 1] + [f"{key}: {value}"] + out[last_updated_idx + 1 :]
            inserted = True
            out = result
        if not inserted:
            out.append(f"{key}: {value}")
    return out


def format_updated(headers: list[str], last_dates: dict[str, str]) -> str:
    # Drop trailing blanks in the header; we re-add the section break.
    while headers and headers[-1] == "":
        headers = headers[:-1]
    lines = list(headers)
    lines.append("")
    lines.append("# symbol last_bar_date")
    for sym in sorted(last_dates):
        lines.append(f"{sym} {last_dates[sym]}")
    return "\n".join(lines) + "\n"


def parse_errors(path: Path) -> dict[str, str]:
    """Parse existing _errors.txt into {symbol: reason}. Ignores comments and '(none)'."""
    errors: dict[str, str] = {}
    if not path.is_file():
        return errors
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line == "(none)":
            continue
        if ":" in line:
            # Keys may contain ':' (for example 'fundamentals:ASML'); the reason
            # starts after the first ': ' (colon + space).
            if ": " in line:
                key, _, reason = line.partition(": ")
            else:
                key, _, reason = line.partition(":")
            key = key.strip()
            reason = reason.strip()
            if key:
                errors[key] = reason
    return errors


def write_errors_file(path: Path, errors: dict[str, str]) -> None:
    err_lines = ["# Tickers that failed this update. No substitute data was used.", ""]
    if errors:
        for k in sorted(errors):
            err_lines.append(f"{k}: {errors[k]}")
    else:
        err_lines.append("(none)")
    write_text(path, "\n".join(err_lines) + "\n")


def write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def csv_filename(symbol: str) -> str:
    """Use the exact Yahoo symbol as the filename. Git handles ^ and others."""
    return f"{symbol}.csv"


def ensure_askpass() -> Path:
    # Lives outside the git repo so it cannot be committed.
    body = (
        "#!/bin/sh\n"
        "# Runtime helper: prints username or $GITHUB_TOKEN. Do not commit.\n"
        "case \"$1\" in\n"
        "  *[Uu]sername*) printf '%s\\n' \"x-access-token\" ;;\n"
        "  *) printf '%s\\n' \"$GITHUB_TOKEN\" ;;\n"
        "esac\n"
    )
    ASKPASS.write_text(body, encoding="utf-8")
    ASKPASS.chmod(ASKPASS.stat().st_mode | stat.S_IEXEC)
    return ASKPASS


def git_env() -> dict:
    if not os.environ.get("GITHUB_TOKEN"):
        raise SystemExit("GITHUB_TOKEN is not set")
    env = os.environ.copy()
    env["GIT_ASKPASS"] = str(ensure_askpass())
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env["GIT_USERNAME"] = "x-access-token"
    return env


def run_git(args: list[str], env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        env=env or os.environ.copy(),
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def sanitize_remote() -> None:
    proc = run_git(["remote", "get-url", "origin"], check=False)
    url = (proc.stdout or "").strip()
    leaky = any(s in url for s in ("ghp_", "github_pat_", "@github.com", "x-access-token"))
    if leaky or url != f"https://github.com/{GITHUB_REPO}.git":
        run_git(["remote", "set-url", "origin", f"https://github.com/{GITHUB_REPO}.git"])


def ensure_repo() -> None:
    if not (REPO / ".git").is_dir():
        REPO.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--branch", BRANCH, f"https://github.com/{GITHUB_REPO}.git", str(REPO)],
            check=True,
        )
    sanitize_remote()
    run_git(["config", "--local", "user.name", "jiaren19"])
    run_git(["config", "--local", "user.email", "jiaren19@yahoo.com"])
    run_git(["config", "--local", "credential.helper", ""])
    run_git(["config", "--local", "http.postBuffer", "524288000"])
    # Sync with origin before writing, so a rerun stays idempotent.
    env = git_env()
    fetch = run_git(["fetch", "origin", BRANCH], env=env, check=False)
    if fetch.returncode != 0:
        # fetch of a public repo should work without auth; retry with auth just in case
        log("git fetch failed; retrying")
        run_git(["fetch", "origin", BRANCH], env=env, check=True)
    run_git(["checkout", BRANCH], check=False)
    # hard reset only if origin/main exists
    has_origin = run_git(["rev-parse", "--verify", f"origin/{BRANCH}"], check=False)
    if has_origin.returncode == 0:
        run_git(["reset", "--hard", f"origin/{BRANCH}"])


NO_PUSH = False


def commit_and_push(message: str) -> str:
    if NO_PUSH:
        log("--no-push: skipping commit and push")
        return "(not committed)"
    env = git_env()
    run_git(["add", "-A"])
    status = run_git(["status", "--porcelain"])
    if not status.stdout.strip():
        log("No file changes; nothing to commit.")
        sha = run_git(["rev-parse", "HEAD"]).stdout.strip()
        return sha
    run_git(["commit", "-m", message])
    push = run_git(["push", "origin", BRANCH], env=env, check=False)
    if push.returncode != 0:
        # Do not print stderr verbatim in case a hook echoes something sensitive;
        # print a short status only, then one rebase retry.
        log("git push failed, trying fetch + rebase + push")
        run_git(["fetch", "origin", BRANCH], env=env)
        rebase = run_git(["rebase", f"origin/{BRANCH}"], check=False)
        if rebase.returncode != 0:
            run_git(["rebase", "--abort"], check=False)
            raise RuntimeError("git push/rebase failed")
        run_git(["push", "origin", BRANCH], env=env, check=True)
    sha = run_git(["rev-parse", "HEAD"]).stdout.strip()
    return sha


README = r"""# market-data

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
"""


def write_readme() -> None:
    (REPO / "README.md").write_text(README, encoding="utf-8")


def write_gitignore() -> None:
    (REPO / ".gitignore").write_text(
        ".DS_Store\n__pycache__/\n*.pyc\n*.tmp\ncache/\n",
        encoding="utf-8",
    )



def _import_fundamentals():
    """Load the sibling fundamentals.py whether this file lives at ROOT or in the repo copy."""
    script_dir = Path(__file__).resolve().parent
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    import fundamentals as fund
    return fund


def fundamentals_stale(max_age_s: float = 6 * 3600) -> bool:
    if os.environ.get("MARKET_DATA_FORCE_FUNDAMENTALS") == "1":
        return True
    path = DATA / "_fundamentals.csv"
    if not path.is_file():
        return True
    headers, _ = parse_updated(DATA / "_updated.txt")
    ts = None
    for line in headers:
        if line.startswith("fundamentals_updated_utc:"):
            raw = line.split(":", 1)[1].strip().replace(" UTC", "")
            try:
                ts = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                ts = None
            break
    if ts is None:
        # Fall back to the CSV mtime if a previous run did not stamp _updated.txt.
        ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return (now_utc() - ts).total_seconds() > max_age_s


def load_constituent_symbols() -> list[str]:
    path = DATA / "_constituents.csv"
    out: list[str] = []
    if not path.is_file():
        return out
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            sym = (row.get("symbol") or "").strip()
            if sym:
                out.append(sym)
    return out


def default_fundamental_extras() -> list[str]:
    """Equities with a price file beyond the constituents: _universe.csv names and
    removed members whose price file passed the same-company check."""
    out: set[str] = set()
    for r in read_universe_file(DATA / "_universe.csv"):
        out.add(r["symbol"])
    reused = {k.split(":", 1)[1] for k in parse_errors(DATA / "_errors.txt") if k.startswith("ticker_reuse:")}
    for s_ in read_membership_file(DATA / "_membership.csv"):
        if s_["date_removed"] and (DATA / csv_filename(s_["symbol"])).is_file() and s_["symbol"] not in reused:
            out.add(s_["symbol"])
    fixed_non_equity = set(FIXED_TICKERS) - set(_import_fundamentals().THEME_FUNDAMENTAL_TICKERS)
    return sorted(out - fixed_non_equity)


def refresh_fundamentals(force: bool = False, extra_symbols: list[str] | None = None) -> dict:
    """Rebuild data/_fundamentals.csv from SEC EDGAR. Returns stats and per-symbol errors.

    extra_symbols: universe / removed-member / previously fetched equities that have
    a price file. When None (fundamentals-only run) they are read from data/.
    """
    fund = _import_fundamentals()
    if extra_symbols is None:
        extra_symbols = default_fundamental_extras()
    symbols = fund.fundamentals_universe(load_constituent_symbols(), extra_symbols)
    log(f"fundamentals: {len(symbols)} symbols  cache={EDGAR_CACHE}")
    client = fund.EdgarClient(EDGAR_CACHE, min_interval=0.115, log=log)
    try:
        cik_map = client.company_tickers(force=force)
    except Exception as exc:
        raise RuntimeError(f"SEC company_tickers download failed: {exc}") from exc
    log(f"fundamentals: SEC ticker map {len(cik_map)} names")

    rows_all: list[dict] = []
    errors: dict[str, str] = {}
    ok_n = 0
    lock = threading.Lock()
    done = 0

    def worker(sym: str) -> None:
        nonlocal done, ok_n
        cik = cik_map.get(sym) or cik_map.get(sym.replace(".", "-")) or cik_map.get(sym.replace("-", "."))
        symbol, rows, err = fund.fetch_symbol(client, sym, cik, force=force)
        with lock:
            if rows:
                rows_all.extend(rows)
                ok_n += 1
            if err:
                errors[sym] = err
            done += 1
            if done % 100 == 0 or done == len(symbols):
                log(f"  fundamentals progress {done}/{len(symbols)}  ok={ok_n}  flagged={len(errors)}")

    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = [pool.submit(worker, s) for s in symbols]
        for f in as_completed(futs):
            _ = f.result()

    rows_all.sort(key=lambda r: (r["symbol"], r["fiscal_quarter_end"]))
    fund.write_fundamentals_csv(DATA / "_fundamentals.csv", rows_all)

    n_ann = sum(1 for r in rows_all if r.get("date_type") in ("8-K", "6-K"))
    n_fb = sum(1 for r in rows_all if r.get("date_type") == "filing_fallback")
    n_blank = sum(1 for r in rows_all if not r.get("report_date"))
    n_der = sum(1 for r in rows_all if "derived_q4" in (r.get("note") or ""))
    failed = [s for s in symbols if s in errors and not any(r["symbol"] == s for r in rows_all)]
    stats = {
        "symbols": len(symbols),
        "symbols_ok": ok_n,
        "symbols_failed": len(failed),
        "rows": len(rows_all),
        "report_date_8k_6k": n_ann,
        "report_date_filing_fallback": n_fb,
        "report_date_blank": n_blank,
        "derived_q4_rows": n_der,
        "errors": errors,
        "failed": failed,
    }
    log(
        f"fundamentals written: {stats['rows']} rows  {ok_n}/{len(symbols)} symbols  "
        f"failed={len(failed)}  report_date 8-K/6-K={n_ann} fallback={n_fb} blank={n_blank}"
    )
    return stats


def stamp_fundamentals_updated(stats: dict, when: datetime | None = None) -> None:
    """Merge fundamentals timestamps and coverage into _updated.txt; keep price lines."""
    when = when or now_utc()
    myt = when.astimezone(MYT)
    utc_s = when.strftime("%Y-%m-%d %H:%M:%S") + " UTC"
    myt_s = myt.strftime("%Y-%m-%d %H:%M:%S") + " Asia/Kuala_Lumpur"
    headers, dates = parse_updated(DATA / "_updated.txt")
    if not headers:
        headers = [
            f"updated_utc: {utc_s}",
            f"updated_asia_kuala_lumpur: {myt_s}",
            "source: Yahoo Finance chart API",
            "history_start: 2015-01-01",
        ]
    headers = upsert_header(headers, "fundamentals_updated_utc", utc_s)
    headers = upsert_header(headers, "fundamentals_updated_asia_kuala_lumpur", myt_s)
    headers = upsert_header(
        headers,
        "fundamentals_source",
        "SEC EDGAR companyfacts XBRL (values) + 8-K Item 2.02 / 6-K filing dates (report_date)",
    )
    headers = upsert_header(headers, "fundamentals_symbols_ok", str(stats.get("symbols_ok", 0)))
    headers = upsert_header(headers, "fundamentals_symbols_failed", str(stats.get("symbols_failed", 0)))
    headers = upsert_header(headers, "fundamentals_rows", str(stats.get("rows", 0)))
    write_text(DATA / "_updated.txt", format_updated(headers, dates))


def merge_fundamentals_errors(fund_errors: dict[str, str]) -> None:
    """Write fundamentals errors as 'fundamentals:SYMBOL: reason' without dropping price errors."""
    merged = parse_errors(DATA / "_errors.txt")
    # Drop previous fundamentals:* lines, then rewrite from this run.
    for k in [k for k in merged if k.startswith("fundamentals:")]:
        merged.pop(k, None)
    for sym, reason in fund_errors.items():
        merged[f"fundamentals:{sym}"] = reason
    write_errors_file(DATA / "_errors.txt", merged)


def _copy_pipeline_into_repo() -> None:
    """Copy the public pipeline files into the git repo so the recipe is versioned."""
    dest_script = REPO / "update.py"
    src = Path(__file__).resolve()
    if src != dest_script.resolve():
        dest_script.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        dest_script.chmod(dest_script.stat().st_mode | stat.S_IEXEC)
    # fundamentals.py lives next to this script at ROOT; also ship it in the repo.
    # Sibling modules live next to this script at ROOT; also ship them in the repo.
    for name in ("fundamentals.py", "membership.py", "universe.py"):
        src_mod = src.parent / name
        dest_mod = REPO / name
        if src_mod.is_file() and src_mod.resolve() != dest_mod.resolve():
            dest_mod.write_text(src_mod.read_text(encoding="utf-8"), encoding="utf-8")


def _import_sibling(name: str):
    script_dir = Path(__file__).resolve().parent
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    return __import__(name)


MEMBERSHIP_COLUMNS = ["symbol", "index", "date_added", "date_removed", "name", "note"]
UNIVERSE_COLUMNS = ["symbol", "name", "sector", "market_cap", "avg_dollar_volume_63d"]


def _parse_d(s: str):
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def read_membership_file(path: Path) -> list[dict]:
    out = []
    if not path.is_file():
        return out
    with path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            out.append({"symbol": r["symbol"], "index": r["index"], "name": r.get("name", ""),
                        "note": r.get("note", ""), "date_added": _parse_d(r.get("date_added", "")),
                        "date_removed": _parse_d(r.get("date_removed", ""))})
    return out


def build_membership() -> tuple[list[dict], dict]:
    """Rebuild data/_membership.csv from Wikipedia change tables (+ fja05680 check)."""
    import io
    M = _import_sibling("membership")
    spx = _WIKI_TABLES.get("spx")
    ndx = _WIKI_TABLES.get("ndx")
    if spx is None or ndx is None:
        raise RuntimeError("current constituent tables unavailable")
    cur_spx: dict[str, dict] = {}
    for _, r in spx.iterrows():
        sym = M.clean_ticker(r["Symbol"])
        da = pd.to_datetime(str(r.get("Date added", "")), errors="coerce")
        cur_spx[sym] = {"name": str(r["Security"]).strip(),
                        "date_added": None if pd.isna(da) else da.date()}
    sym_col = "Ticker" if "Ticker" in ndx.columns else "Symbol"
    name_col = "Company" if "Company" in ndx.columns else "Security"
    cur_ndx = {M.clean_ticker(r[sym_col]): {"name": str(r[name_col]).strip(), "date_added": None}
               for _, r in ndx.iterrows()}
    es = M.parse_change_table(http_get(WIKI_SPX_HIST, WIKI_UA, timeout=60))
    en = M.parse_change_table(http_get(WIKI_NDX_HIST, WIKI_UA, timeout=60))
    fja = None
    fja_err = ""
    try:
        fja = http_get(FJA_SP500_URL, WIKI_UA, timeout=60)
    except Exception as exc:
        fja_err = str(exc)[:200]
    spx_start = min(e["date"] for e in es)
    ndx_start = min(e["date"] for e in en)
    sp, ws = M.reconstruct("SP500", cur_spx, es, spx_start, M.fja_first_seen(fja))
    nn, wn = M.reconstruct("NDX", cur_ndx, en, ndx_start)
    check = M.crosscheck_fja(sp, fja) if fja else {"error": fja_err or "fja05680 not downloaded"}
    spells = sp + nn
    spells.sort(key=lambda s_: (s_["symbol"], s_["index"], s_["date_added"] or date.min))
    tmp = DATA / "_membership.csv.tmp"
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(MEMBERSHIP_COLUMNS)
        for s_ in spells:
            w.writerow([s_["symbol"], s_["index"],
                        s_["date_added"].isoformat() if s_["date_added"] else "",
                        s_["date_removed"].isoformat() if s_["date_removed"] else "",
                        s_["name"], s_["note"]])
    tmp.replace(DATA / "_membership.csv")
    stats = {
        "rows_sp500": len(sp), "rows_ndx": len(nn),
        "events_sp500": len(es), "events_ndx": len(en),
        "source_start_sp500": spx_start.isoformat(), "source_start_ndx": ndx_start.isoformat(),
        "warnings": ws + wn, "fja": check,
        "removed_spells": sum(1 for s_ in spells if s_["date_removed"]),
    }
    return spells, stats


def read_universe_file(path: Path) -> list[dict]:
    out = []
    if not path.is_file():
        return out
    with path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                mc = int(float(r.get("market_cap") or 0))
            except ValueError:
                mc = 0
            out.append({"symbol": r["symbol"], "name": r.get("name", ""), "sector": r.get("sector", ""),
                        "market_cap": mc})
    return out


def validate_removed(sym: str, rows: list[dict], meta: dict, spells: list[dict]) -> str | None:
    """None if the Yahoo series is plausibly the removed index member; else the reason."""
    M = _import_sibling("membership")
    ref = max(spells, key=lambda s_: s_["date_removed"] or date.min)
    removed = ref["date_removed"]
    first = date.fromisoformat(rows[0]["date"])
    last = date.fromisoformat(rows[-1]["date"])
    ftd = meta.get("firstTradeDate")
    names = [n for n in (meta.get("longName"), meta.get("shortName")) if n]
    wiki_names = sorted({s_["name"] for s_ in spells if s_["name"]})
    label = f"removed {ref['index']} member '{ref['name']}' (removed {removed})"
    if ftd:
        try:
            ftd_d = datetime.fromtimestamp(int(ftd), tz=timezone.utc).date()
            if removed and ftd_d > removed:
                return f"{label}; Yahoo's {sym} first traded {ftd_d}, after the removal: ticker reused"
        except (TypeError, ValueError, OSError):
            pass
    if removed and first > removed:
        return f"{label}; Yahoo series starts {first}, after the removal: ticker reused"
    added = ref["date_added"]
    if added and added >= HISTORY_START and (first - added).days > 30:
        return f"{label}; Yahoo series starts {first}, long after it joined ({added}): different listing"
    if removed and (removed - last).days > 10:
        return f"{label}; Yahoo series ends {last}, well before the removal: not the same series"
    if names and not any(M.names_match(n, w) for n in names for w in wiki_names):
        return f"{label}; Yahoo now names {sym} '{names[0]}' (rename or reused ticker); not saved"
    return None


def check_size_limits() -> dict:
    files = [p for p in REPO.rglob("*") if p.is_file() and ".git" not in p.parts]
    total = sum(p.stat().st_size for p in files)
    largest = max(files, key=lambda p: p.stat().st_size)
    info = {"files": len(files), "bytes": total, "largest": str(largest.relative_to(REPO)),
            "largest_bytes": largest.stat().st_size}
    log(f"size check: {info['files']} files, {total / 1e6:.1f} MB working tree, "
        f"largest {info['largest']} {info['largest_bytes'] / 1e6:.2f} MB")
    if info["largest_bytes"] > MAX_FILE_BYTES or total > MAX_DATA_BYTES:
        raise RuntimeError(f"size guard tripped, not pushing: {info}")
    return info


def run_full(started: datetime) -> int:
    U = _import_sibling("universe")
    M = _import_sibling("membership")
    core_errors: dict[str, str] = {}
    info_errors: dict[str, str] = {}
    constituents: list[dict] = []

    # 1. Current constituents
    try:
        constituents = fetch_constituents()
        n_spx = sum(1 for r in constituents if r["index"] in ("SP500", "both"))
        n_ndx = sum(1 for r in constituents if r["index"] in ("NDX", "both"))
        n_both = sum(1 for r in constituents if r["index"] == "both")
        log(f"constituents: {len(constituents)} unique  SP500={n_spx}  NDX={n_ndx}  both={n_both}")
        write_constituents(DATA / "_constituents.csv", constituents)
    except Exception as exc:
        reason = f"constituent download failed: {exc}"
        log(reason)
        core_errors["_constituents"] = reason
        existing = DATA / "_constituents.csv"
        if existing.is_file():
            with existing.open(encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    if row.get("symbol"):
                        constituents.append({"symbol": row["symbol"], "name": row.get("name", ""),
                                             "sector": row.get("sector", ""), "index": row.get("index", "")})
            log(f"reusing previous _constituents.csv ({len(constituents)} rows)")

    # 2. Membership history
    mem_stats: dict = {}
    try:
        spells, mem_stats = build_membership()
        log(f"membership: SP500 {mem_stats['rows_sp500']} spells, NDX {mem_stats['rows_ndx']} spells, "
            f"{len(mem_stats['warnings'])} warnings; fja check {json.dumps({k: v for k, v in mem_stats['fja'].items() if k not in ('renames', 'worst')})}")
        for i, w in enumerate(mem_stats["warnings"], 1):
            info_errors[f"membership:{i:02d}"] = w
    except Exception as exc:
        log(f"membership rebuild failed: {exc}")
        core_errors["_membership"] = f"membership rebuild failed, previous file kept: {str(exc)[:300]}"
        spells = read_membership_file(DATA / "_membership.csv")

    # 3. Universe candidates
    universe_as_of = now_utc()
    excluded: dict[str, int] = {}
    try:
        cands, excluded = U.screen(U.fetch_screener())
        log(f"universe: {len(cands)} candidates with market cap >= 2B after excluding {excluded}")
    except Exception as exc:
        log(f"Nasdaq screener failed: {exc}")
        core_errors["_universe"] = f"Nasdaq screener failed, previous universe reused: {str(exc)[:300]}"
        cands = read_universe_file(DATA / "_universe.csv")
        universe_as_of = None
    cand_by_sym = {c["symbol"]: c for c in cands}

    # 4. Fetch groups
    core: list[str] = []
    seen: set[str] = set()
    for s_ in FIXED_TICKERS + [r["symbol"] for r in constituents]:
        if s_ and s_ not in seen:
            seen.add(s_)
            core.append(s_)
    cand_syms = [c["symbol"] for c in cands if c["symbol"] not in seen]
    seen.update(cand_syms)
    current_names = {r["symbol"]: r.get("name", "") for r in constituents}
    for c in cands:
        current_names.setdefault(c["symbol"], c["name"])
    removed_spells: dict[str, list[dict]] = {}
    for s_ in spells:
        if s_["date_removed"]:
            removed_spells.setdefault(s_["symbol"], []).append(s_)
    removed_syms: list[str] = []
    keep_removed_cands: set[str] = set()  # removed members still listed, below the universe filter
    for sym, sps in sorted(removed_spells.items()):
        if sym in seen:
            cur = current_names.get(sym, "")
            if sym in cand_by_sym and cur and any(M.names_match(cur, x["name"]) for x in sps if x["name"]):
                keep_removed_cands.add(sym)
            # A current file exists for this ticker; flag it if it is a different company.
            cur = current_names.get(sym, "")
            if cur and not any(M.names_match(cur, x["name"]) for x in sps if x["name"]):
                ref = max(sps, key=lambda x: x["date_removed"])
                info_errors[f"ticker_reuse:{sym}"] = (
                    f"data/{sym}.csv is the current listing '{cur}'; its name does not match the removed "
                    f"{ref['index']} member '{ref['name']}' (removed {ref['date_removed']}). Either a rename or a "
                    "different company reusing the ticker: check before using this file for that spell")
            continue
        removed_syms.append(sym)
        seen.add(sym)
    existing_syms = sorted(p.name[:-4] for p in DATA.glob("*.csv")
                           if not p.name.startswith("_") and p.name[:-4] not in seen)
    roles = [(s_, "core") for s_ in core] + [(s_, "cand") for s_ in cand_syms] + \
            [(s_, "removed") for s_ in removed_syms] + [(s_, "existing") for s_ in existing_syms]
    log(f"tickers to fetch: {len(roles)}  core={len(core)} candidates={len(cand_syms)} "
        f"removed={len(removed_syms)} existing={len(existing_syms)}  (workers={WORKERS})")

    lock = threading.Lock()
    last_dates: dict[str, str] = {}
    adv: dict[str, float] = {}
    qualified: set[str] = set()
    removed_ok: list[str] = []
    removed_fail: dict[str, str] = {}
    done = 0
    fetch_started = time.time()

    def worker(sym: str, role: str) -> None:
        nonlocal done
        path = DATA / csv_filename(sym)
        try:
            try:
                rows, meta = fetch_yahoo(sym)
            except Exception as exc:
                reason = str(exc).replace("\n", " ")[:300]
                with lock:
                    if role == "core":
                        core_errors[sym] = reason
                    elif role == "cand":
                        info_errors[f"universe_candidate:{sym}"] = reason
                    elif role == "removed":
                        removed_fail[sym] = reason
                        ref = max(removed_spells[sym], key=lambda x: x["date_removed"])
                        info_errors[f"removed:{sym}"] = (
                            f"{ref['index']} member '{ref['name']}' removed {ref['date_removed']}: "
                            f"Yahoo has no data ({reason[:120]})")
                    else:
                        info_errors[f"stale:{sym}"] = f"refresh failed, previous file kept: {reason[:200]}"
                return
            old_first = first_csv_date(path)
            if old_first and (date.fromisoformat(rows[0]["date"]) - date.fromisoformat(old_first)).days > 30:
                with lock:
                    info_errors[f"ticker_reuse:{sym}"] = (
                        f"Yahoo series now starts {rows[0]['date']} but data/{sym}.csv starts {old_first}; "
                        "stored file kept, not overwritten")
                    if role == "core":
                        core_errors[sym] = "Yahoo history start moved; file not overwritten (see ticker_reuse)"
                return
            save = True
            if role == "cand":
                a = U.avg_dollar_volume(rows)
                with lock:
                    if a is not None:
                        adv[sym] = a
                    if a is not None and a >= U.MIN_DOLLAR_VOLUME:
                        qualified.add(sym)
                save = (a is not None and a >= U.MIN_DOLLAR_VOLUME) or path.is_file() \
                    or sym in keep_removed_cands
            elif role == "removed":
                why = validate_removed(sym, rows, meta, removed_spells[sym])
                if why:
                    with lock:
                        removed_fail[sym] = why
                        info_errors[f"ticker_reuse:{sym}"] = why
                    save = False
                else:
                    with lock:
                        removed_ok.append(sym)
            if save:
                last_written = write_csv(path, rows)
                with lock:
                    last_dates[sym] = last_written
        finally:
            with lock:
                done += 1
                if done % 100 == 0 or done == len(roles):
                    log(f"  progress {done}/{len(roles)}  saved={len(last_dates)}  core_failed="
                        f"{len([k for k in core_errors if not k.startswith('_')])}  "
                        f"{time.time() - fetch_started:.0f}s")

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = [pool.submit(worker, s_, r_) for s_, r_ in roles]
        for f in as_completed(futs):
            _ = f.result()
    fetch_elapsed = time.time() - fetch_started

    # 5. Universe file (today's qualifiers only). Core names (index members, fixed
    # tickers) that are also screener candidates were fetched as core; score them
    # from the file just written so the universe list is complete.
    for s_ in core:
        if s_ not in cand_by_sym or s_ not in last_dates:
            continue
        lines = read_csv_lines(DATA / csv_filename(s_))
        bars = [{"close": float(r[4]) if r[4] else None, "volume": float(r[6]) if r[6] else None}
                for _, r in sorted(lines.items())]
        a_ = U.avg_dollar_volume(bars)
        if a_ is not None:
            adv[s_] = a_
            if a_ >= U.MIN_DOLLAR_VOLUME:
                qualified.add(s_)
    if cands:
        tmp = DATA / "_universe.csv.tmp"
        with tmp.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(UNIVERSE_COLUMNS)
            for c in cands:
                if c["symbol"] in qualified:
                    w.writerow([c["symbol"], c["name"], c["sector"], c["market_cap"],
                                int(round(adv[c["symbol"]]))])
        tmp.replace(DATA / "_universe.csv")
    n_universe = len(qualified)
    log(f"universe: {n_universe} qualify (market cap >= 2B and 63-day avg dollar volume >= 20M); "
        f"price fetch {fetch_elapsed:.0f}s")

    # 6. _updated.txt
    prev_headers, prev_dates = parse_updated(DATA / "_updated.txt")
    all_dates = {k: v for k, v in prev_dates.items() if (DATA / csv_filename(k)).is_file()}
    all_dates.update(last_dates)
    finished = now_utc()
    myt = finished.astimezone(MYT)
    keep_prefixes = ("asia_updated_", "fundamentals_")
    upd = [f"updated_utc: {finished.strftime('%Y-%m-%d %H:%M:%S')} UTC",
           f"updated_asia_kuala_lumpur: {myt.strftime('%Y-%m-%d %H:%M:%S')} Asia/Kuala_Lumpur"]
    upd += [ln for ln in prev_headers if ln.startswith(keep_prefixes)]
    upd += ["source: Yahoo Finance chart API", "history_start: 2015-01-01",
            f"tickers_ok: {len(last_dates)}",
            f"tickers_failed: {len([k for k in core_errors if not k.startswith('_')])}",
            f"price_files: {len(all_dates)}"]
    if "_constituents" in core_errors:
        upd.append(f"constituents_error: {core_errors['_constituents']}")
    else:
        upd.append(f"constituents: {len(constituents)}")
    if universe_as_of is not None:
        ua = universe_as_of
        upd.append(f"universe_as_of_utc: {ua.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        upd.append(f"universe_as_of_asia_kuala_lumpur: {ua.astimezone(MYT).strftime('%Y-%m-%d %H:%M:%S')} Asia/Kuala_Lumpur")
    else:
        upd.append("universe_as_of_utc: screener failed this run; _universe.csv rescored from previous list")
    upd += ["universe_source: Nasdaq stock screener API (symbol, name, sector, market cap); "
            "63-day average close*volume from Yahoo bars",
            "universe_filter: market_cap >= 2000000000 USD and avg_dollar_volume_63d >= 20000000 USD; "
            "common stock, ADRs and LP common units; preferreds, notes, warrants, rights, units, SPACs, funds excluded",
            f"universe_size: {n_universe}", f"universe_candidates: {len(cands)}"]
    if mem_stats:
        upd += [f"membership_rows_sp500: {mem_stats['rows_sp500']}",
                f"membership_rows_ndx: {mem_stats['rows_ndx']}",
                "membership_source: Wikipedia Historical components of the S&P 500 / of the Nasdaq-100 "
                "+ current constituent lists; fja05680/sp500 cross-check"]
    removed_same_listing = [s_ for s_ in removed_spells if s_ not in removed_syms and s_ in all_dates
                            and f"ticker_reuse:{s_}" not in info_errors]
    upd += [f"removed_members_total: {len(removed_spells)}",
            f"removed_members_fetched_ok: {len(removed_ok)}",
            f"removed_members_already_current_listing: {len(removed_same_listing)}",
            f"removed_members_not_saved: {len(removed_fail)}"]
    write_text(DATA / "_updated.txt", format_updated(upd, all_dates))

    # 7. Errors (core first; informational prefixes do not change the exit code)
    write_errors_file(DATA / "_errors.txt", {**core_errors, **info_errors})

    # 8. Fundamentals for every equity with a price file
    fund_stats = None
    fund_elapsed = 0.0
    if fundamentals_stale():
        fund_started = now_utc()
        log("fundamentals are stale or missing; refreshing from EDGAR")
        extra = sorted(set(qualified) | set(removed_ok) |
                       {s_ for s_ in existing_syms if s_ in all_dates} |
                       {s_ for s_ in removed_spells if s_ in all_dates and f"ticker_reuse:{s_}" not in info_errors})
        try:
            fund_stats = refresh_fundamentals(force=False, extra_symbols=extra)
            stamp_fundamentals_updated(fund_stats)
            merge_fundamentals_errors(fund_stats.get("errors") or {})
        except Exception as exc:
            log(f"fundamentals refresh failed (price data still committed): {exc}")
            merged = parse_errors(DATA / "_errors.txt")
            merged["fundamentals"] = str(exc).replace("\n", " ")[:500]
            write_errors_file(DATA / "_errors.txt", merged)
        fund_elapsed = (now_utc() - fund_started).total_seconds()
    else:
        log("fundamentals CSV is less than 6 hours old; skipping EDGAR refresh")

    elapsed = (now_utc() - started).total_seconds()
    n_core_fail = len([k for k in core_errors if not k.startswith("_")])
    msg = (f"data: daily update {myt.strftime('%Y-%m-%d')} ({len(last_dates)} ok, {n_core_fail} failed; "
           f"universe {n_universe}")
    if fund_stats is not None:
        msg += f"; fundamentals {fund_stats['symbols_ok']} ok, {fund_stats['rows']} rows"
    msg += ")"
    check_size_limits()
    log(f"writing commit: {msg}")
    sha = commit_and_push(msg)
    log(f"commit {sha}")
    log(f"summary: {len(last_dates)} saved, {n_core_fail} core failed, universe {n_universe}, "
        f"removed ok {len(removed_ok)} / not saved {len(removed_fail)}, {elapsed:.0f}s")
    if fund_stats is not None:
        log(f"  fundamentals: {fund_stats['symbols_ok']} ok, {fund_stats['symbols_failed']} failed, "
            f"{fund_stats['rows']} rows, {fund_elapsed:.0f}s")
    for k in sorted(core_errors):
        log(f"  FAIL {k}: {core_errors[k]}")
    for probe in ("SPY", "2330.TW"):
        log(f"  last {probe}: {all_dates.get(probe, '未取得')}")
    return 0 if not core_errors else 1


def main(argv=None) -> int:
    args = parse_args(argv)
    global NO_PUSH
    NO_PUSH = bool(args.no_push)
    asia_mode = bool(args.asia)
    fund_only = bool(args.fundamentals)
    started = now_utc()
    if not os.environ.get("GITHUB_TOKEN"):
        log("ERROR: GITHUB_TOKEN is not set")
        return 2

    if fund_only and asia_mode:
        log("ERROR: --fundamentals and --asia cannot be combined")
        return 2

    mode = "fundamentals" if fund_only else ("asia" if asia_mode else "full")
    log(f"start {started.isoformat()}  repo={GITHUB_REPO}  mode={mode}")
    ensure_repo()
    DATA.mkdir(parents=True, exist_ok=True)
    write_readme()
    write_gitignore()
    write_themes(DATA / "_themes.csv")
    _copy_pipeline_into_repo()

    if fund_only:
        try:
            stats = refresh_fundamentals(force=False)
        except Exception as exc:
            log(f"ERROR: fundamentals refresh failed: {exc}")
            return 1
        stamp_fundamentals_updated(stats)
        merge_fundamentals_errors(stats.get("errors") or {})
        finished = now_utc()
        myt = finished.astimezone(MYT)
        elapsed = (finished - started).total_seconds()
        msg = (
            f"data: fundamentals update {myt.strftime('%Y-%m-%d')} "
            f"({stats['symbols_ok']} ok, {stats['symbols_failed']} failed, {stats['rows']} rows)"
        )
        log(f"writing commit: {msg}")
        sha = commit_and_push(msg)
        log(f"commit {sha}")
        log(f"summary: fundamentals {stats['symbols_ok']} ok, {stats['symbols_failed']} failed, "
            f"{stats['rows']} rows, {elapsed:.0f}s")
        failed = stats.get("failed") or []
        for k in sorted(failed):
            log(f"  FAIL fundamentals:{k}: {stats['errors'].get(k)}")
        return 0 if not failed else 1

    if not asia_mode:
        return run_full(started)

    errors: dict[str, str] = {}
    tickers = list(ASIA_TICKERS)
    log(f"asia mode: fetching {len(tickers)} tickers; other CSVs will be left untouched")

    ok: dict[str, list[dict]] = {}
    lock = threading.Lock()
    done = 0

    def worker(sym: str) -> None:
        nonlocal done
        try:
            rows = fetch_yahoo_bars(sym)
            with lock:
                ok[sym] = rows
        except Exception as exc:
            with lock:
                errors[sym] = str(exc).replace("\n", " ")[:500]
        finally:
            with lock:
                done += 1
                log(f"  progress {done}/{len(tickers)}  ok={len(ok)}  failed={len(errors)}")

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = [pool.submit(worker, s) for s in tickers]
        for f in as_completed(futs):
            _ = f.result()

    failed_tickers = dict(errors)
    # Overwrite only the Asia/BTC CSVs that succeeded. Leave every other
    # CSV (and a failed Asia ticker's existing file) untouched.
    last_dates: dict[str, str] = {}
    for sym, rows in ok.items():
        last_dates[sym] = write_csv(DATA / csv_filename(sym), rows)

    existing_headers, existing_dates = parse_updated(DATA / "_updated.txt")
    existing_dates.update(last_dates)
    finished = now_utc()
    myt = finished.astimezone(MYT)
    utc_s = finished.strftime("%Y-%m-%d %H:%M:%S") + " UTC"
    myt_s = myt.strftime("%Y-%m-%d %H:%M:%S") + " Asia/Kuala_Lumpur"
    headers = existing_headers or [
        "source: Yahoo Finance chart API",
        "history_start: 2015-01-01",
    ]
    headers = upsert_header(headers, "asia_updated_utc", utc_s)
    headers = upsert_header(headers, "asia_updated_asia_kuala_lumpur", myt_s)
    write_text(DATA / "_updated.txt", format_updated(headers, existing_dates))

    merged_errors = parse_errors(DATA / "_errors.txt")
    for sym in ASIA_TICKERS:
        if sym in failed_tickers:
            merged_errors[sym] = failed_tickers[sym]
        else:
            merged_errors.pop(sym, None)
    write_errors_file(DATA / "_errors.txt", merged_errors)

    elapsed = (finished - started).total_seconds()
    msg = f"data: asia update {myt.strftime('%Y-%m-%d')} ({len(ok)} ok, {len(failed_tickers)} failed)"
    log(f"writing commit: {msg}")
    sha = commit_and_push(msg)
    log(f"commit {sha}")
    log(f"summary: {len(ok)} ok, {len(failed_tickers)} failed, {elapsed:.0f}s")
    if failed_tickers:
        for k in sorted(failed_tickers):
            log(f"  FAIL {k}: {failed_tickers[k]}")
    for probe in ("2330.TW", "005930.KS", "8035.T", "BTC-USD"):
        log(f"  last {probe}: {last_dates.get(probe, existing_dates.get(probe, '未取得'))}")
    return 0 if not failed_tickers else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
