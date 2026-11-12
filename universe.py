"""Expanded scan universe for update.py.

Candidate list, market cap, and sector: Nasdaq stock screener API (all
NASDAQ, NYSE, and NYSE American listings). Dollar volume is computed in
update.py from the Yahoo daily bars, never taken from Nasdaq.
"""
from __future__ import annotations

import json
import re
import urllib.request

NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&download=true"
MIN_MARKET_CAP = 2_000_000_000
MIN_DOLLAR_VOLUME = 20_000_000
ADV_DAYS = 63
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_SYMBOL_OK = re.compile(r"[A-Z]{1,5}([./][A-Z])?")
_LP_UNITS = re.compile(r"common units|limited partner|partnership units|lp units|l\.p\.\s+units|class a units", re.I)


def fetch_screener() -> list[dict]:
    req = urllib.request.Request(
        NASDAQ_SCREENER_URL,
        headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://www.nasdaq.com",
            "Referer": "https://www.nasdaq.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    rows = ((payload.get("data") or {}).get("rows")) or []
    if len(rows) < 3000:
        raise RuntimeError(f"Nasdaq screener returned only {len(rows)} rows")
    return rows


def to_yahoo(sym: str) -> str:
    return sym.strip().upper().replace("/", "-").replace(".", "-")


def exclusion_reason(r: dict) -> str | None:
    sym = (r.get("symbol") or "").strip().upper()
    name = (r.get("name") or "").strip()
    n = name.lower()
    ind = (r.get("industry") or "").strip().lower()
    if not _SYMBOL_OK.fullmatch(sym):
        return "symbol_format"
    if "preferred" in n:
        return "preferred"
    if re.search(r"\bnotes?\b|debenture|\bbonds?\b|\bzones\b|subordinated", n):
        return "debt"
    if re.search(r"warrant", n):
        return "warrant"
    adr = "american depositary" in n or "american depository" in n
    if re.search(r"\brights?\b", n) and not adr:
        return "right"
    if re.search(r"\bunits?\b", n) and not _LP_UNITS.search(n) and not adr:
        return "unit"
    if "blank check" in ind or re.search(r"\bacquisition corp", n):
        return "spac"
    if re.search(r"\betf\b|\bfund\b|exchange traded|\betn\b", n) or "trusts except educational" in ind \
            or "mutual fund" in ind:
        return "fund"
    return None


def screen(rows: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Market-cap and security-type screen. Returns candidates and exclusion counts."""
    out: dict[str, dict] = {}
    excluded: dict[str, int] = {}
    for r in rows:
        try:
            mcap = float(str(r.get("marketCap") or "0").replace(",", "") or 0)
        except ValueError:
            mcap = 0.0
        if mcap < MIN_MARKET_CAP:
            continue
        why = exclusion_reason(r)
        if why:
            excluded[why] = excluded.get(why, 0) + 1
            continue
        y = to_yahoo(r["symbol"])
        out[y] = {
            "symbol": y,
            "name": re.sub(r"\s+", " ", (r.get("name") or "").strip()),
            "sector": (r.get("sector") or "").strip(),
            "market_cap": int(round(mcap)),
            "nasdaq_symbol": r["symbol"].strip(),
        }
    return [out[k] for k in sorted(out)], excluded


def avg_dollar_volume(rows: list[dict], days: int = ADV_DAYS) -> float | None:
    """Mean of close * volume over the last `days` bars that have both values."""
    vals = []
    for r in reversed(rows):
        c, v = r.get("close"), r.get("volume")
        if c is None or v is None:
            continue
        vals.append(float(c) * float(v))
        if len(vals) == days:
            break
    if len(vals) < days:
        return None
    return sum(vals) / len(vals)
