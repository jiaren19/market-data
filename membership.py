"""Index membership history (S&P 500, Nasdaq-100) for update.py.

Built from Wikipedia change tables, walked backwards from the current
constituent lists. No membership dates are invented: a date that the
sources do not give is left blank and explained in `note`.
"""
from __future__ import annotations

import io
import re
from datetime import date

import pandas as pd

WINDOW_START = date(2015, 1, 1)

_STOP = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "companies", "ltd", "limited",
    "plc", "holdings", "holding", "group", "the", "class", "a", "b", "c", "common", "stock",
    "shares", "share", "sa", "nv", "se", "ag", "lp", "llc", "de", "international", "intl",
    "and", "of", "&", "ordinary", "new", "adr", "ads", "sponsored", "trust", "reit", "n", "v",
}


def name_tokens(name: str) -> set[str]:
    s = str(name or "").lower()
    s = s.replace("&", " ").replace("'", "").replace("\u2019", "").replace(".", "")
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return {t for t in s.split() if t and t not in _STOP}


def names_match(a: str, b: str) -> bool:
    """Conservative company-name comparison. True only on a clear match."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return False
    if ta <= tb or tb <= ta:
        return True
    inter = len(ta & tb)
    return inter / min(len(ta), len(tb)) >= 0.6 or inter / len(ta | tb) >= 0.5


def clean_ticker(raw) -> str:
    if raw is None or (isinstance(raw, float) and raw != raw):
        return ""
    s = str(raw).split("[")[0].replace("|", " ").replace("\xa0", " ").strip()
    if not s or s.lower() == "nan":
        return ""
    s = s.split()[0]
    return s.replace(".", "-").upper()


def _clean_name(raw) -> str:
    if raw is None or (isinstance(raw, float) and raw != raw):
        return ""
    s = re.sub(r"\[\d+\]", "", str(raw)).replace("|", " ").strip()
    return "" if s.lower() == "nan" else s


def parse_change_table(html: bytes) -> list[dict]:
    """Return events {date, add, add_name, rem, rem_name, reason} from the first wide table."""
    tables = pd.read_html(io.BytesIO(html))
    t = None
    for cand in tables:
        if cand.shape[1] >= 6 and cand.shape[0] > 50:
            t = cand
            break
    if t is None:
        raise RuntimeError("change table not found")
    t = t.iloc[:, :6].copy()
    t.columns = ["date", "add_t", "add_n", "rem_t", "rem_n", "reason"]
    events = []
    for _, r in t.iterrows():
        d = pd.to_datetime(str(r["date"]), errors="coerce")
        if pd.isna(d):
            continue
        add, rem = clean_ticker(r["add_t"]), clean_ticker(r["rem_t"])
        if not add and not rem:
            continue
        events.append({
            "date": d.date(),
            "add": add, "add_name": _clean_name(r["add_n"]),
            "rem": rem, "rem_name": _clean_name(r["rem_n"]),
            "reason": _clean_name(r["reason"]),
        })
    if len(events) < 100:
        raise RuntimeError(f"change table too short ({len(events)} events)")
    return events


def fja_first_seen(fja_csv: bytes | None) -> dict[str, date]:
    """Earliest fja05680 snapshot date (>= 2015) on which each ticker is listed."""
    out: dict[str, date] = {}
    if not fja_csv:
        return out
    df = pd.read_csv(io.BytesIO(fja_csv))
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"] >= pd.Timestamp(WINDOW_START)].dropna(subset=["date"]).sort_values("date")
    for _, r in df.iterrows():
        d = r["date"].date()
        for x in str(r["tickers"]).split(","):
            t = clean_ticker(x)
            if t and t not in out:
                out[t] = d
    return out


def reconstruct(index: str, current: dict[str, dict], events: list[dict],
                source_start: date, fja_seen: dict[str, date] | None = None) -> tuple[list[dict], list[str]]:
    """Walk events backwards from today's members.

    current: symbol -> {"name", "date_added" (date|None from the current table)}
    Returns (spells overlapping 2015-01-01.., warnings).
    """
    warnings: list[str] = []
    open_: dict[str, dict] = {}
    done: list[dict] = []
    for sym, info in current.items():
        open_[sym] = {"symbol": sym, "index": index, "date_added": None, "date_removed": None,
                      "name": info.get("name", ""), "note": "", "_cur_added": info.get("date_added")}
    ev = sorted(events, key=lambda e: e["date"], reverse=True)
    i = 0
    while i < len(ev):
        d = ev[i]["date"]
        same = []
        while i < len(ev) and ev[i]["date"] == d:
            same.append(ev[i])
            i += 1
        # Going backwards: additions on d close spells, then removals on d open spells.
        for e in same:
            a = e["add"]
            if not a:
                continue
            sp = open_.pop(a, None)
            if sp is None:
                # Possible ticker change during membership: match by company name.
                cands = [s for s in open_.values() if names_match(s["name"], e["add_name"])]
                if len(cands) == 1:
                    sp = open_.pop(cands[0]["symbol"])
                    sp["note"] = (sp["note"] + "; " if sp["note"] else "") + f"added_as:{a}"
                else:
                    warnings.append(f"{index} {d} add {a} ({e['add_name']}): no later removal or current "
                                    "membership found under this ticker/name; spell omitted")
                    continue
            sp["date_added"] = d
            done.append(sp)
        for e in same:
            r = e["rem"]
            if not r:
                continue
            if r in open_:
                warnings.append(f"{index} {d} remove {r}: already open from a later event; kept later spell")
                continue
            open_[r] = {"symbol": r, "index": index, "date_added": None, "date_removed": d,
                        "name": e["rem_name"], "note": "", "_cur_added": None}
    for sp in open_.values():
        cur = sp.get("_cur_added")
        seen = (fja_seen or {}).get(sp["symbol"])
        if sp["date_removed"] is None and cur and cur >= WINDOW_START and seen and (cur - seen).days > 7:
            # The current table's "Date added" is contradicted: fja05680 already lists
            # this ticker as a member earlier, and no change-table event adds it.
            sp["note"] = (sp["note"] + "; " if sp["note"] else "") + \
                f"current_table_date_added_{cur.isoformat()}_contradicted_fja05680_member_since_{seen.isoformat()}_or_earlier"
        elif sp["date_removed"] is None and cur:
            sp["date_added"] = cur
            sp["note"] = (sp["note"] + "; " if sp["note"] else "") + "date_added_from_current_table"
        else:
            sp["note"] = (sp["note"] + "; " if sp["note"] else "") + \
                "date_added_not_in_source_table"
        done.append(sp)
    out = []
    for sp in done:
        cur = sp.pop("_cur_added", None)
        if sp["date_removed"] is None and cur and sp["date_added"] and abs((cur - sp["date_added"]).days) > 3 \
                and "date_added_from_current_table" not in sp["note"]:
            sp["note"] = (sp["note"] + "; " if sp["note"] else "") + f"current_table_date_added:{cur.isoformat()}"
        if sp["date_removed"] is None or sp["date_removed"] >= WINDOW_START:
            out.append(sp)
    out.sort(key=lambda s: (s["symbol"], s["index"], s["date_added"] or date.min))
    return out, warnings


def members_on(spells: list[dict], index: str, d: date) -> set[str]:
    return {s["symbol"] for s in spells if s["index"] == index
            and (s["date_added"] is None or s["date_added"] <= d)
            and (s["date_removed"] is None or s["date_removed"] > d)}


def crosscheck_fja(spells: list[dict], fja_csv: bytes) -> dict:
    """Compare reconstructed S&P 500 members with fja05680/sp500 snapshots since 2015.

    fja05680 labels each date with the ticker in force then; this file uses one
    ticker per spell. Between consecutive fja snapshots, if fja drops exactly one
    ticker and adds exactly one while this reconstruction has no change in that
    interval, it is treated as a ticker change and fja's older label is mapped
    forward before comparing.
    """
    df = pd.read_csv(io.BytesIO(fja_csv))
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"] >= pd.Timestamp(WINDOW_START)].dropna(subset=["date"]).sort_values("date")
    if df.empty:
        return {"dates": 0}
    snaps = []
    for _, r in df.iterrows():
        d = r["date"].date()
        theirs = {clean_ticker(x) for x in str(r["tickers"]).split(",") if x.strip()}
        snaps.append((d, members_on(spells, "SP500", d), theirs))
    renames: list[tuple[str, str, str]] = []
    for (d0, o0, t0), (d1, o1, t1) in zip(snaps, snaps[1:]):
        rem, add = t0 - t1, t1 - t0
        if len(rem) == 1 and len(add) == 1 and o0 == o1:
            renames.append((d1.isoformat(), next(iter(rem)), next(iter(add))))
    # Map each fja label forward through later ticker changes.
    fwd = {old: new for _, old, new in renames}
    def final(t: str) -> str:
        hops = 0
        while t in fwd and hops < 10:
            t, hops = fwd[t], hops + 1
        return t
    raw, resid, worst, cdiff = [], [], None, []
    for d, o, t in snaps:
        raw.append(len(o ^ t))
        cdiff.append(abs(len(o) - len(t)))
        tm = {final(x) for x in t}
        a, b = sorted(o - tm), sorted(tm - o)
        n = len(a) + len(b)
        resid.append(n)
        if worst is None or n > worst[0]:
            worst = (n, d, a, b)
    raw.sort()
    rs = sorted(resid)
    return {"dates": len(snaps), "first_date": snaps[0][0].isoformat(), "last_date": snaps[-1][0].isoformat(),
            "raw_median_symdiff": raw[len(raw) // 2], "ticker_changes_detected": len(renames),
            "median_symdiff": rs[len(rs) // 2], "p90_symdiff": rs[int(len(rs) * 0.9)], "max_symdiff": rs[-1],
            "share_dates_exact": round(sum(1 for x in rs if x == 0) / len(rs), 3),
            "median_abs_count_diff": sorted(cdiff)[len(cdiff) // 2], "max_abs_count_diff": max(cdiff),
            "worst": {"date": worst[1].isoformat(), "only_here": worst[2][:25], "only_fja": worst[3][:25]},
            "renames": renames}
