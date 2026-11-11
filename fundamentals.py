"""EDGAR quarterly fundamentals helpers used by update.py.

Values come from the SEC companyfacts XBRL API. report_date is the
8-K Item 2.02 filing date (earnings press-release date) when available.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

FUNDAMENTALS_START = date(2015, 1, 1)
SEC_UA = "Jia Ren jiaren19@yahoo.com"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_SUBS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_SUBS_FILE_URL = "https://data.sec.gov/submissions/{name}"

# Theme stocks Claude asked to include in addition to S&P 500 + Nasdaq-100.
THEME_FUNDAMENTAL_TICKERS = [
    "MU", "SNDK", "WDC", "STX",
    "COHR", "LITE", "CIEN", "GLW", "AAOI", "FN",
    "BE", "VST", "CEG", "GEV", "ETN", "VRT", "PWR", "NRG",
    "AMAT", "LRCX", "KLAC", "ASML", "TER", "AEHR", "FORM", "ONTO", "ACMR", "CAMT",
    "CRWV", "NBIS", "IREN", "APLD", "CIFR", "WULF", "CORZ",
]

# Tag priority within EDGAR. One filing family (companyfacts); we do not
# mix Yahoo/Nasdaq numbers into these fields.
REVENUE_TAGS = [
    ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
    ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
    ("us-gaap", "Revenues"),
    ("us-gaap", "SalesRevenueNet"),
    ("us-gaap", "RevenuesNetOfInterestExpense"),
    ("ifrs-full", "RevenueFromContractsWithCustomers"),
    ("ifrs-full", "Revenue"),
]
GROSS_PROFIT_TAGS = [
    ("us-gaap", "GrossProfit"),
    ("us-gaap", "GrossProfitLoss"),
    ("ifrs-full", "GrossProfit"),
]
NET_INCOME_TAGS = [
    ("us-gaap", "NetIncomeLoss"),
    ("us-gaap", "ProfitLoss"),
    ("ifrs-full", "ProfitLoss"),
    ("ifrs-full", "ProfitLossAttributableToOwnersOfParent"),
]
EPS_TAGS = [
    ("us-gaap", "EarningsPerShareDiluted"),
    ("us-gaap", "EarningsPerShareBasicAndDiluted"),
    ("ifrs-full", "DilutedEarningsLossPerShare"),
    ("ifrs-full", "BasicAndDilutedEarningsLossPerShare"),
]

PERIODIC_FORMS = {
    "10-Q", "10-K", "10-Q/A", "10-K/A",
    "20-F", "20-F/A", "40-F", "40-F/A",
}
CURRENT_FORMS = {"8-K", "8-K/A", "6-K", "6-K/A"}
VALUE_FORMS = PERIODIC_FORMS | CURRENT_FORMS
QUARTER_MIN_DAYS = 70
QUARTER_MAX_DAYS = 120
ANNUAL_MIN_DAYS = 330
ANNUAL_MAX_DAYS = 400
ANNOUNCE_MAX_DAYS = 90

PREFERRED_UNITS = {
    "revenue": ["USD", "EUR", "GBP", "CNY", "JPY", "ILS", "CAD", "CHF", "TWD", "KRW"],
    "gross_profit": ["USD", "EUR", "GBP", "CNY", "JPY", "ILS", "CAD", "CHF", "TWD", "KRW"],
    "net_income": ["USD", "EUR", "GBP", "CNY", "JPY", "ILS", "CAD", "CHF", "TWD", "KRW"],
    "eps_diluted": [
        "USD/shares", "EUR/shares", "GBP/shares", "CNY/shares",
        "JPY/shares", "ILS/shares", "CAD/shares", "CHF/shares",
        "TWD/shares", "KRW/shares",
    ],
}


def _cik10(cik: int | str) -> str:
    return f"{int(cik):010d}"


def duration_days(row: dict) -> int | None:
    start, end = row.get("start"), row.get("end")
    if not start or not end:
        return None
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except ValueError:
        return None


def is_quarterly(row: dict) -> bool:
    d = duration_days(row)
    return d is not None and QUARTER_MIN_DAYS <= d <= QUARTER_MAX_DAYS


def is_annual(row: dict) -> bool:
    d = duration_days(row)
    return d is not None and ANNUAL_MIN_DAYS <= d <= ANNUAL_MAX_DAYS


def parse_iso_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


class EdgarClient:
    """Polite SEC HTTP client with on-disk JSON cache."""

    def __init__(self, cache_dir: Path, min_interval: float = 0.12, log=print):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()
        self.log = log

    def _reserve_slot(self) -> None:
        with self._lock:
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()

    def get_json(self, url: str, cache_name: str | None, max_age_s: float | None, force: bool = False):
        cache_path = self.cache_dir / cache_name if cache_name else None
        if cache_path and cache_path.is_file() and not force:
            age = time.time() - cache_path.stat().st_mtime
            if max_age_s is None or age <= max_age_s:
                try:
                    return json.loads(cache_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
        last_err = None
        for attempt in range(1, 7):
            self._reserve_slot()
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": SEC_UA,
                    "Accept": "application/json,text/plain,*/*",
                    "Accept-Encoding": "identity",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    body = resp.read()
                self._last = time.time()
                data = json.loads(body.decode("utf-8"))
                if cache_path:
                    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
                    tmp.write_text(json.dumps(data), encoding="utf-8")
                    tmp.replace(cache_path)
                return data
            except urllib.error.HTTPError as exc:
                self._last = time.time()
                last_err = exc
                snippet = ""
                try:
                    snippet = exc.read().decode("utf-8", "replace")[:200]
                except Exception:
                    pass
                if exc.code in (403, 429, 500, 502, 503, 504):
                    backoff = min(90, 4 * attempt if exc.code in (500, 502, 503, 504) else 15 * attempt)
                    self.log(f"  SEC HTTP {exc.code} attempt {attempt}/6, backoff {backoff}s")
                    time.sleep(backoff)
                    continue
                if exc.code == 404:
                    return None
                raise RuntimeError(f"SEC HTTP {exc.code} for {url}: {snippet}") from exc
            except Exception as exc:
                self._last = time.time()
                last_err = exc
                time.sleep(min(30, 2 ** attempt))
        raise RuntimeError(f"SEC fetch failed after retries: {last_err}")

    def company_tickers(self, force: bool = False) -> dict[str, int]:
        data = self.get_json(SEC_TICKERS_URL, "company_tickers.json", max_age_s=86400 * 7, force=force)
        out: dict[str, int] = {}
        if not data:
            return out
        rows = data.values() if isinstance(data, dict) else data
        for rec in rows:
            if not isinstance(rec, dict):
                continue
            ticker = str(rec.get("ticker") or "").upper().strip()
            cik = rec.get("cik_str")
            if not ticker or cik is None:
                continue
            cik_i = int(cik)
            out[ticker] = cik_i
            out[ticker.replace(".", "-")] = cik_i
            out[ticker.replace("-", ".")] = cik_i
        return out

    def facts(self, cik: int, force: bool = False, max_age_s: float | None = 86400):
        cik10 = _cik10(cik)
        return self.get_json(SEC_FACTS_URL.format(cik=cik10), f"{cik10}_facts.json", max_age_s, force=force)

    def submissions(self, cik: int, force: bool = False, max_age_s: float | None = 86400) -> dict:
        cik10 = _cik10(cik)
        main = self.get_json(SEC_SUBS_URL.format(cik=cik10), f"{cik10}_submissions.json", max_age_s, force=force)
        if not main:
            return {}
        # Merge older filing archives so 8-K history reaches back to 2015.
        files = ((main.get("filings") or {}).get("files") or [])
        all_recent = dict((main.get("filings") or {}).get("recent") or {})
        for meta in files:
            name = meta.get("name")
            filing_to = parse_iso_date(meta.get("filingTo"))
            # Skip archives that ended before our history window (minus a year of slack).
            if filing_to and filing_to < date(2014, 1, 1):
                continue
            if not name:
                continue
            extra = self.get_json(
                SEC_SUBS_FILE_URL.format(name=name),
                name,
                max_age_s=86400 * 30,
                force=force,
            )
            if not extra:
                continue
            # extra may be a filings object with the same column lists
            block = extra.get("filings", extra)
            if "recent" in block and isinstance(block["recent"], dict):
                block = block["recent"]
            if not all_recent:
                all_recent = {k: list(v) for k, v in block.items() if isinstance(v, list)}
                continue
            n_add = len(block.get("accessionNumber") or [])
            if not n_add:
                continue
            for key, vals in all_recent.items():
                add = block.get(key) or [None] * n_add
                if len(add) < n_add:
                    add = list(add) + [None] * (n_add - len(add))
                if isinstance(vals, list):
                    vals.extend(add[:n_add])
        if "filings" not in main:
            main["filings"] = {}
        main["filings"]["recent"] = all_recent
        return main


def filing_records(submissions: dict) -> list[dict]:
    recent = (submissions.get("filings") or {}).get("recent") or {}
    acc = recent.get("accessionNumber") or []
    keys = list(recent.keys())
    out = []
    for i in range(len(acc)):
        out.append({k: (recent[k][i] if i < len(recent.get(k) or []) else None) for k in keys})
    return out


def announcement_dates(filings: list[dict]) -> list[tuple[date, str]]:
    """Earnings publication dates: 8-K Item 2.02, plus 6-Ks that look like results."""
    found: list[tuple[date, str]] = []
    seen: set[tuple[date, str]] = set()
    for rec in filings:
        form = str(rec.get("form") or "")
        filed = parse_iso_date(rec.get("filingDate"))
        if not filed:
            continue
        items = str(rec.get("items") or "")
        desc = str(rec.get("primaryDocDescription") or "")
        if form.startswith("8-K") and "2.02" in items:
            key = (filed, "8-K")
            if key not in seen:
                seen.add(key)
                found.append(key)
            continue
        if form.startswith("6-K"):
            # 6-K has no Item 2.02. Keep candidates whose description mentions
            # results/earnings, or whose reportDate looks like a period end
            # (matched later against actual quarter ends).
            blob = (desc + " " + items).lower()
            if any(w in blob for w in ("earning", "result of operation", "results of operation", "interim report", "quarterly")):
                key = (filed, "6-K")
                if key not in seen:
                    seen.add(key)
                    found.append(key)
    found.sort()
    return found


def sixk_by_report_date(filings: list[dict]) -> dict[date, date]:
    """Map 6-K reportDate -> earliest filingDate (best-effort foreign earnings date)."""
    out: dict[date, date] = {}
    for rec in filings:
        form = str(rec.get("form") or "")
        if not form.startswith("6-K"):
            continue
        filed = parse_iso_date(rec.get("filingDate"))
        report = parse_iso_date(rec.get("reportDate"))
        if not filed or not report:
            continue
        # Many non-earnings 6-Ks set reportDate == filingDate; those are not
        # period ends and will simply fail to match a fiscal_quarter_end.
        if report not in out or filed < out[report]:
            out[report] = filed
    return out


def first_periodic_filed(filings: list[dict], period_end: date) -> tuple[date, str] | None:
    """Earliest 10-Q/10-K/20-F whose reportDate equals this quarter end."""
    best = None
    for rec in filings:
        form = str(rec.get("form") or "")
        base = form.replace("/A", "")
        if base not in {"10-Q", "10-K", "20-F", "40-F"}:
            continue
        report = parse_iso_date(rec.get("reportDate"))
        filed = parse_iso_date(rec.get("filingDate"))
        if report != period_end or not filed:
            continue
        if best is None or filed < best[0]:
            best = (filed, base)
    return best


def match_report_date(
    period_end: date,
    announcements: list[tuple[date, str]],
    sixk_reports: dict[date, date],
    periodic: tuple[date, str] | None,
    next_period_end: date | None,
) -> tuple[str, str]:
    """Return (report_date_iso_or_blank, date_type)."""
    window_end = period_end.toordinal() + ANNOUNCE_MAX_DAYS
    if next_period_end:
        # Do not assign the next quarter's earnings date to this quarter.
        window_end = min(window_end, next_period_end.toordinal() - 1)
    for filed, typ in announcements:
        if filed <= period_end:
            continue
        if filed.toordinal() > window_end:
            continue
        return filed.isoformat(), typ
    # Foreign 6-K whose reportDate equals (or is within 2 days of) the quarter end.
    for delta in (0, -1, 1, -2, 2):
        key = date.fromordinal(period_end.toordinal() + delta)
        filed = sixk_reports.get(key)
        if filed and filed > period_end and filed.toordinal() <= window_end:
            return filed.isoformat(), "6-K"
    if periodic:
        filed, base = periodic
        if period_end < filed and (filed - period_end).days <= ANNOUNCE_MAX_DAYS:
            return filed.isoformat(), "filing_fallback"
    return "", ""


def _iter_tag_facts(facts: dict, tax: str, tag: str) -> list[tuple[str, dict]]:
    obj = ((facts.get(tax) or {}).get(tag) or {})
    units = obj.get("units") or {}
    out = []
    for unit, recs in units.items():
        for rec in recs or []:
            out.append((unit, rec))
    return out


def first_reported_quarterly(
    facts: dict,
    tags: list[tuple[str, str]],
    preferred_units: list[str],
) -> dict[date, dict]:
    """period_end -> {val, unit, filed, form, start, accn} from the earliest official fact."""
    # Gather candidates per period end, tagged with priority and unit rank.
    cands: dict[date, list[tuple]] = defaultdict(list)
    for prio, (tax, tag) in enumerate(tags):
        for unit, rec in _iter_tag_facts(facts, tax, tag):
            if not is_quarterly(rec):
                continue
            form = str(rec.get("form") or "")
            if form not in VALUE_FORMS:
                continue
            end = parse_iso_date(rec.get("end"))
            filed = parse_iso_date(rec.get("filed"))
            if end is None or rec.get("val") is None or filed is None:
                continue
            try:
                val = float(rec["val"])
            except (TypeError, ValueError):
                continue
            if val != val:  # NaN
                continue
            unit_rank = preferred_units.index(unit) if unit in preferred_units else 50
            # Prefer periodic forms over 8-K/6-K for the official number.
            form_rank = 0 if form in PERIODIC_FORMS else 1
            cands[end].append((form_rank, prio, unit_rank, filed, form, unit, val, rec.get("start"), rec.get("accn")))
    out: dict[date, dict] = {}
    for end, rows in cands.items():
        # First pick the best tag/unit among periodic forms if any, then the
        # earliest filing of that tag/unit (as-first-reported, not a later restatement).
        periodic = [r for r in rows if r[0] == 0]
        pool = periodic or rows
        # Choose tag/unit by (prio, unit_rank) among the pool, then earliest filed.
        best_key = min((r[1], r[2]) for r in pool)
        pool2 = [r for r in pool if (r[1], r[2]) == best_key]
        chosen = min(pool2, key=lambda r: r[3])
        form_rank, prio, unit_rank, filed, form, unit, val, start, accn = chosen
        out[end] = {
            "val": val,
            "unit": unit,
            "filed": filed,
            "form": form,
            "start": start,
            "tag_prio": prio,
        }
    return out


def first_reported_annual(
    facts: dict,
    tags: list[tuple[str, str]],
    preferred_units: list[str],
) -> dict[date, dict]:
    cands: dict[date, list[tuple]] = defaultdict(list)
    for prio, (tax, tag) in enumerate(tags):
        for unit, rec in _iter_tag_facts(facts, tax, tag):
            if not is_annual(rec):
                continue
            form = str(rec.get("form") or "")
            if form not in PERIODIC_FORMS:
                continue
            end = parse_iso_date(rec.get("end"))
            filed = parse_iso_date(rec.get("filed"))
            if end is None or rec.get("val") is None or filed is None:
                continue
            try:
                val = float(rec["val"])
            except (TypeError, ValueError):
                continue
            if val != val:
                continue
            unit_rank = preferred_units.index(unit) if unit in preferred_units else 50
            cands[end].append((prio, unit_rank, filed, form, unit, val))
    out: dict[date, dict] = {}
    for end, rows in cands.items():
        best_key = min((r[0], r[1]) for r in rows)
        pool = [r for r in rows if (r[0], r[1]) == best_key]
        chosen = min(pool, key=lambda r: r[2])
        prio, unit_rank, filed, form, unit, val = chosen
        out[end] = {"val": val, "unit": unit, "filed": filed, "form": form}
    return out


def fiscal_year_ends(filings: list[dict], annual_map: dict[date, dict]) -> list[date]:
    """Fiscal year-end dates from 10-K/20-F reportDate, else annual fact ends."""
    ends = []
    seen = set()
    for rec in filings:
        form = str(rec.get("form") or "").replace("/A", "")
        if form not in {"10-K", "20-F", "40-F"}:
            continue
        report = parse_iso_date(rec.get("reportDate"))
        if report and report not in seen:
            seen.add(report)
            ends.append(report)
    for end in annual_map:
        if end not in seen:
            seen.add(end)
            ends.append(end)
    ends.sort()
    return ends


def derive_q4(quarterlies: dict[date, dict], annuals: dict[date, dict], fy_ends: list[date]) -> dict[date, dict]:
    """For each FY end with no quarterly fact, Q4 = annual - Q1 - Q2 - Q3 (additive fields only)."""
    derived: dict[date, dict] = {}
    if not fy_ends:
        return derived
    for i, fy_end in enumerate(fy_ends):
        if fy_end in quarterlies:
            continue
        if fy_end not in annuals:
            continue
        prev = fy_ends[i - 1] if i else None
        # Quarters belonging to this FY: end date after previous FY end, up to this FY end.
        members = [
            d for d in quarterlies
            if d <= fy_end and (prev is None or d > prev)
        ]
        members.sort()
        if len(members) != 3:
            continue
        # Require the same currency unit as the annual figure.
        ann = annuals[fy_end]
        vals = []
        ok = True
        for d in members:
            q = quarterlies[d]
            if q.get("unit") != ann.get("unit"):
                ok = False
                break
            vals.append(q["val"])
        if not ok:
            continue
        q4 = ann["val"] - sum(vals)
        derived[fy_end] = {
            "val": q4,
            "unit": ann["unit"],
            "filed": ann["filed"],
            "form": ann["form"],
            "derived": True,
        }
    return derived


def build_symbol_rows(
    symbol: str,
    facts_payload: dict | None,
    submissions: dict | None,
) -> tuple[list[dict], str | None]:
    """Return (rows, error_or_none). Rows may be empty with an error explaining why."""
    if not facts_payload or not (facts_payload.get("facts")):
        return [], "no companyfacts XBRL in EDGAR"
    facts = facts_payload["facts"]
    filings = filing_records(submissions or {})
    announces = announcement_dates(filings)
    sixk_reports = sixk_by_report_date(filings)

    revenue_q = first_reported_quarterly(facts, REVENUE_TAGS, PREFERRED_UNITS["revenue"])
    gp_q = first_reported_quarterly(facts, GROSS_PROFIT_TAGS, PREFERRED_UNITS["gross_profit"])
    ni_q = first_reported_quarterly(facts, NET_INCOME_TAGS, PREFERRED_UNITS["net_income"])
    eps_q = first_reported_quarterly(facts, EPS_TAGS, PREFERRED_UNITS["eps_diluted"])

    revenue_a = first_reported_annual(facts, REVENUE_TAGS, PREFERRED_UNITS["revenue"])
    gp_a = first_reported_annual(facts, GROSS_PROFIT_TAGS, PREFERRED_UNITS["gross_profit"])
    ni_a = first_reported_annual(facts, NET_INCOME_TAGS, PREFERRED_UNITS["net_income"])

    fy_ends = fiscal_year_ends(filings, revenue_a or ni_a or gp_a)
    revenue_q.update(derive_q4(revenue_q, revenue_a, fy_ends))
    gp_q.update(derive_q4(gp_q, gp_a, fy_ends))
    ni_q.update(derive_q4(ni_q, ni_a, fy_ends))
    # EPS is not additive (share count changes); never derive Q4 EPS.

    period_ends = sorted(
        set(revenue_q) | set(gp_q) | set(ni_q) | set(eps_q)
    )
    period_ends = [d for d in period_ends if d >= FUNDAMENTALS_START]
    if not period_ends:
        # Distinguish annual-only foreign filers from a complete miss.
        if revenue_a or ni_a or gp_a:
            return [], "no quarterly XBRL financials in EDGAR companyfacts (annual 20-F/10-K only)"
        return [], "no quarterly revenue/gross_profit/eps/net_income in EDGAR companyfacts"

    rows = []
    for i, end in enumerate(period_ends):
        nxt = period_ends[i + 1] if i + 1 < len(period_ends) else None
        periodic = first_periodic_filed(filings, end)
        report_date, date_type = match_report_date(end, announces, sixk_reports, periodic, nxt)
        notes = []
        def field(src: dict[date, dict], name: str):
            rec = src.get(end)
            if not rec:
                return None, None
            if rec.get("derived"):
                notes.append(f"derived_q4_{name}")
            unit = rec.get("unit") or ""
            if unit and not unit.startswith("USD"):
                notes.append(f"{name}_unit:{unit}")
            return rec["val"], rec
        revenue, _ = field(revenue_q, "revenue")
        gp, _ = field(gp_q, "gross_profit")
        ni, _ = field(ni_q, "net_income")
        eps, _ = field(eps_q, "eps_diluted")
        # Dedup note unit if all USD - already skipped. Collapse derived flags.
        note = ";".join(dict.fromkeys(notes))
        rows.append({
            "symbol": symbol,
            "fiscal_quarter_end": end.isoformat(),
            "report_date": report_date,
            "revenue": revenue,
            "gross_profit": gp,
            "eps_diluted": eps,
            "net_income": ni,
            "date_type": date_type,
            "note": note,
        })
    return rows, None


def fmt_fund_num(val, kind: str) -> str:
    if val is None:
        return ""
    try:
        f = float(val)
    except (TypeError, ValueError):
        return ""
    if f != f:
        return ""
    if kind == "eps":
        s = f"{f:.6f}".rstrip("0").rstrip(".")
        return s if s else "0"
    # money: keep integers as integers (SEC stores whole dollars)
    if abs(f - round(f)) < 1e-6:
        return str(int(round(f)))
    s = f"{f:.2f}".rstrip("0").rstrip(".")
    return s if s else "0"


def write_fundamentals_csv(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fields = [
        "symbol", "fiscal_quarter_end", "report_date",
        "revenue", "gross_profit", "eps_diluted", "net_income",
        "date_type", "note",
    ]
    import csv
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(fields)
        for r in rows:
            w.writerow([
                r["symbol"],
                r["fiscal_quarter_end"],
                r.get("report_date") or "",
                fmt_fund_num(r.get("revenue"), "money"),
                fmt_fund_num(r.get("gross_profit"), "money"),
                fmt_fund_num(r.get("eps_diluted"), "eps"),
                fmt_fund_num(r.get("net_income"), "money"),
                r.get("date_type") or "",
                r.get("note") or "",
            ])
    tmp.replace(path)


def fundamentals_universe(constituent_symbols: list[str]) -> list[str]:
    seen = set()
    out = []
    for s in list(constituent_symbols) + THEME_FUNDAMENTAL_TICKERS:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def fetch_symbol(
    client: EdgarClient,
    symbol: str,
    cik: int | None,
    force: bool = False,
) -> tuple[str, list[dict], str | None]:
    """Return (symbol, rows, error_or_warning). rows may be present with a warning."""
    if not cik:
        return symbol, [], "no CIK in SEC company_tickers"
    try:
        facts = client.facts(cik, force=force)
        subs = client.submissions(cik, force=force)
    except Exception as exc:
        return symbol, [], str(exc).replace("\n", " ")[:500]
    rows, err = build_symbol_rows(symbol, facts, subs)
    if rows:
        last = parse_iso_date(rows[-1]["fiscal_quarter_end"])
        if last and (date.today() - last).days > 200:
            warn = (
                f"partial history only; last fiscal_quarter_end {last.isoformat()} "
                "(recent quarters not in EDGAR XBRL)"
            )
            err = f"{err}; {warn}" if err else warn
    return symbol, rows, err
