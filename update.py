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
GITHUB_REPO = "jiaren19/market-data"
BRANCH = "main"
HISTORY_START = date(2019, 1, 1)
PERIOD1 = int(datetime(2019, 1, 1, tzinfo=timezone.utc).timestamp())
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

WORKERS = 4
MAX_RETRIES = 5
BASE_SLEEP = 0.15  # polite pause after each Yahoo request
WIKI_SPX = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
WIKI_NDX = "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies"

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
]

# Theme map written to data/_themes.csv (one row per symbol).
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
    """Daily OHLCV + adj_close from 2019-01-01. Raises on failure."""
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
        raise RuntimeError("no daily bars on or after 2019-01-01")
    return rows


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


def write_csv(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["date", "open", "high", "low", "close", "adj_close", "volume"])
        for r in rows:
            w.writerow(
                [
                    r["date"],
                    _fmt_num(r["open"]),
                    _fmt_num(r["high"]),
                    _fmt_num(r["low"]),
                    _fmt_num(r["close"]),
                    _fmt_num(r["adj_close"]),
                    _fmt_volume(r["volume"]),
                ]
            )
    tmp.replace(path)


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


def commit_and_push(message: str) -> str:
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

Daily public market-data snapshot for research. Source: Yahoo Finance chart API only. Missing tickers are listed in `data/_errors.txt`. No substitute source is used, and no numbers are filled in by hand.

由 Grok Bot 在每個美股交易日收盤後自動抓取並推送。資料只來自 Yahoo Finance，抓不到的標的會寫進 `data/_errors.txt`，不會用其他來源的數字補上。

## Update schedule / 更新時間

- Full update (`python3 update.py`): daily after the US cash-session close, around **06:30 Asia/Kuala_Lumpur**, Tuesday to Saturday (Monday morning MYT is Sunday in New York, so there is no new US session to fetch).
- 完整更新：每個美股交易日收盤後，約馬來西亞時間 **06:30**（周二至周六）。
- Asia-only update (`python3 update.py --asia`): after the Asian cash close, around **15:00 Asia/Kuala_Lumpur**, Monday to Friday. This overwrites only the 16 Taiwan, Korea, Japan, and `BTC-USD` files; every other CSV is left untouched.
- 亞洲盤更新：亞洲市場收盤後，約馬來西亞時間 **15:00**（周一至周五），只更新台、韓、日標的與 `BTC-USD`。
- Reruns on the same day overwrite the same files and may create a new commit.

## Files / 檔案說明

| Path | What it is |
| --- | --- |
| `data/<SYMBOL>.csv` | Daily bars for one Yahoo Finance symbol. The filename is the exact symbol, including `^` (for example `data/^VIX.csv`, `data/2330.TW.csv`, `data/BTC-USD.csv`). |
| `data/_constituents.csv` | Current S&P 500 and Nasdaq-100 members: `symbol,name,sector,index`. `index` is `SP500`, `NDX`, or `both`. Symbols are in Yahoo format (`BRK.B` → `BRK-B`). Sector is the Wikipedia GICS Sector when the name is in the S&P 500. Nasdaq-100-only names have no GICS column on Wikipedia, so that row uses the ICB Industry from the Wikipedia Nasdaq-100 list. |
| `data/_themes.csv` | Theme map for selected ETFs and focus stocks: `symbol,theme_zh,theme_en`. One row per symbol. This is a static mapping shipped with the daily update, not inferred from prices. |
| `data/_updated.txt` | UTC and Asia/Kuala_Lumpur timestamps of the last full run, each ticker's last bar date, and (after an Asia-only run) `asia_updated_utc` / `asia_updated_asia_kuala_lumpur`. An `--asia` run updates only the 16 Asia/BTC last-bar lines and the asia timestamp; other lines are kept. |
| `data/_errors.txt` | Tickers that could not be fetched, with the reason. An `--asia` run updates or clears error lines only for those 16 tickers; unrelated error lines from the last full run are kept. |
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
- Theme ETFs and focus stocks: `AIQ` `BOTZ` `QTUM` `CIBR` `GDX` `SIL` `SLX` `URA` `JETS` `IYT` `ITA` `TAN` `OIH` `IHI` `XBI` `KBE` `KRE` `XRT` `ITB` `KWEB` `SOCL` `IYZ` `IBIT` `WGMI` `MU` `SNDK` `WDC` `STX`
- Plus every current S&P 500 and Nasdaq-100 constituent (deduplicated). Names that also appear in the fixed or theme lists are fetched once.

Theme labels (Chinese and English) for the theme ETFs, plus already-fetched `SMH` `SOXX` `IGV`, are in `data/_themes.csv`.

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
"""


def write_readme() -> None:
    (REPO / "README.md").write_text(README, encoding="utf-8")


def write_gitignore() -> None:
    (REPO / ".gitignore").write_text(
        ".DS_Store\n__pycache__/\n*.pyc\n*.tmp\n",
        encoding="utf-8",
    )


def main(argv=None) -> int:
    args = parse_args(argv)
    asia_mode = bool(args.asia)
    started = now_utc()
    if not os.environ.get("GITHUB_TOKEN"):
        log("ERROR: GITHUB_TOKEN is not set")
        return 2

    mode = "asia" if asia_mode else "full"
    log(f"start {started.isoformat()}  repo={GITHUB_REPO}  mode={mode}")
    ensure_repo()
    DATA.mkdir(parents=True, exist_ok=True)
    write_readme()
    write_gitignore()
    write_themes(DATA / "_themes.csv")

    # Copy this script into the repo so the data recipe is public and versioned.
    dest_script = REPO / "update.py"
    src = Path(__file__).resolve()
    if src != dest_script.resolve():
        dest_script.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        dest_script.chmod(dest_script.stat().st_mode | stat.S_IEXEC)

    errors: dict[str, str] = {}
    constituents: list[dict] = []

    if asia_mode:
        tickers = list(ASIA_TICKERS)
        log(f"asia mode: fetching {len(tickers)} tickers; other CSVs will be left untouched")
    else:
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
            errors["_constituents"] = reason
            existing = DATA / "_constituents.csv"
            if existing.is_file():
                with existing.open(encoding="utf-8") as fh:
                    reader = csv.DictReader(fh)
                    for row in reader:
                        if row.get("symbol"):
                            constituents.append(
                                {
                                    "symbol": row["symbol"],
                                    "name": row.get("name", ""),
                                    "sector": row.get("sector", ""),
                                    "index": row.get("index", ""),
                                }
                            )
                log(f"reusing previous _constituents.csv ({len(constituents)} rows)")
            else:
                log("no previous _constituents.csv; continuing with fixed tickers only")

        tickers = []
        seen: set[str] = set()
        for s in FIXED_TICKERS + [r["symbol"] for r in constituents]:
            if s and s not in seen:
                seen.add(s)
                tickers.append(s)
        log(f"tickers to fetch: {len(tickers)}  (workers={WORKERS})")

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
                step = 1 if len(tickers) <= 25 else 25
                if done % step == 0 or done == len(tickers):
                    failed_n = len([k for k in errors if k != "_constituents"])
                    log(f"  progress {done}/{len(tickers)}  ok={len(ok)}  failed={failed_n}")

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = [pool.submit(worker, s) for s in tickers]
        for f in as_completed(futs):
            _ = f.result()

    failed_tickers = {k: v for k, v in errors.items() if k != "_constituents"}

    if asia_mode:
        # Overwrite only the Asia/BTC CSVs that succeeded. Leave every other
        # CSV (and a failed Asia ticker's existing file) untouched.
        last_dates: dict[str, str] = {}
        for sym, rows in ok.items():
            write_csv(DATA / csv_filename(sym), rows)
            last_dates[sym] = rows[-1]["date"]

        existing_headers, existing_dates = parse_updated(DATA / "_updated.txt")
        existing_dates.update(last_dates)
        finished = now_utc()
        myt = finished.astimezone(MYT)
        utc_s = finished.strftime("%Y-%m-%d %H:%M:%S") + " UTC"
        myt_s = myt.strftime("%Y-%m-%d %H:%M:%S") + " Asia/Kuala_Lumpur"
        headers = existing_headers or [
            "source: Yahoo Finance chart API",
            "history_start: 2019-01-01",
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

    # Full mode: replace the universe, rewrite metadata from this run only.
    keep_names = {csv_filename(s) for s in ok}
    keep_names.update({"_updated.txt", "_errors.txt", "_constituents.csv", "_themes.csv"})
    if DATA.is_dir():
        for p in DATA.iterdir():
            if p.is_file() and p.name.endswith(".csv") and p.name not in keep_names and not p.name.startswith("_"):
                p.unlink()

    last_dates = {}
    for sym, rows in ok.items():
        write_csv(DATA / csv_filename(sym), rows)
        last_dates[sym] = rows[-1]["date"]

    # Do not leave a stale CSV for a ticker that failed this full run.
    for sym in failed_tickers:
        stale = DATA / csv_filename(sym)
        if stale.is_file():
            stale.unlink()

    write_errors_file(DATA / "_errors.txt", errors)

    finished = now_utc()
    myt = finished.astimezone(MYT)
    # Preserve an earlier asia-mode timestamp if one exists, so the afternoon
    # marker is not erased by the next morning's full run until that day's
    # asia job rewrites it.
    prev_headers, _prev_dates = parse_updated(DATA / "_updated.txt")
    asia_utc = ""
    asia_myt = ""
    for line in prev_headers:
        if line.startswith("asia_updated_utc:"):
            asia_utc = line.split(":", 1)[1].strip()
        elif line.startswith("asia_updated_asia_kuala_lumpur:"):
            asia_myt = line.split(":", 1)[1].strip()

    upd = []
    upd.append(f"updated_utc: {finished.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    upd.append(f"updated_asia_kuala_lumpur: {myt.strftime('%Y-%m-%d %H:%M:%S')} Asia/Kuala_Lumpur")
    if asia_utc:
        upd.append(f"asia_updated_utc: {asia_utc}")
    if asia_myt:
        upd.append(f"asia_updated_asia_kuala_lumpur: {asia_myt}")
    upd.append("source: Yahoo Finance chart API")
    upd.append("history_start: 2019-01-01")
    upd.append(f"tickers_ok: {len(ok)}")
    upd.append(f"tickers_failed: {len(failed_tickers)}")
    if "_constituents" in errors:
        upd.append(f"constituents_error: {errors['_constituents']}")
    else:
        upd.append(f"constituents: {len(constituents)}")
    upd.append("")
    upd.append("# symbol last_bar_date")
    for sym in sorted(last_dates):
        upd.append(f"{sym} {last_dates[sym]}")
    write_text(DATA / "_updated.txt", "\n".join(upd) + "\n")

    elapsed = (finished - started).total_seconds()
    msg = f"data: daily update {myt.strftime('%Y-%m-%d')} ({len(ok)} ok, {len(failed_tickers)} failed)"
    log(f"writing commit: {msg}")
    sha = commit_and_push(msg)
    log(f"commit {sha}")
    log(f"summary: {len(ok)} ok, {len(failed_tickers)} failed, {elapsed:.0f}s")
    if failed_tickers:
        for k in sorted(failed_tickers):
            log(f"  FAIL {k}: {failed_tickers[k]}")
    for probe in ("SPY", "2330.TW"):
        log(f"  last {probe}: {last_dates.get(probe, '未取得')}")
    return 0 if not failed_tickers else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
