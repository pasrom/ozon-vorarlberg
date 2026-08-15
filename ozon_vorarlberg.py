#!/usr/bin/env python3
"""
ozon_vorarlberg.py — ozone scraper and history logger for Vorarlberg.

Source: https://www.vorarlberg-luft.at/tab1O3.htm

The page is a server-side generated static HTML export (generator:
InterConnect Software, layout unchanged since 2004) and is rewritten hourly.
It carries exactly five numbers per station: the current 1h mean, the daily
maxima of the 1h and the running 8h mean, and the same two maxima for the
previous day. There is NO numeric time series — the linked "graphical course"
pages contain nothing but a pre-rendered JPEG.

So this script builds the history itself: every fetch is appended to
history.jsonl (deduplicated on the source page's own timestamp), and that is
the series the dashboard draws.

Assessment — two separate axes, deliberately not mixed:

  1. Training scale (on the CURRENT 1h value). What you are breathing now.
     A pragmatic scale, not a legal limit: the 100/120 marks are borrowed from
     the 8h guidelines, while 180 is the actual Austrian information
     threshold (1h).
         < 100  good      free      full session possible
         < 120  warning   ok        shorten long hard blocks
         < 180  serious   easy      base only, nothing intense
         >=180  critical  indoors   information threshold, cancel outdoor sport

  2. Daily assessment (on the daily maximum of the 8h mean). The health
     context of the day, measured against the real reference values:
         WHO short-term guideline 2021   100 ug/m3 (max. daily 8h mean)
         EU target value                 120 ug/m3 (8h, running)
         AT information threshold        180 ug/m3 (1h)

The first dashboard showed the 1h value in large type but coloured the traffic
light by the daily 8h maximum. That is misleading: around midday the running
8h mean still drags the cool morning hours along and sits well below what is
actually in the air.

Usage:
    python3 ozon_vorarlberg.py                      # JSON on stdout
    python3 ozon_vorarlberg.py --compact            # one line per station
    python3 ozon_vorarlberg.py --log --out data.json    # the cron invocation
    python3 ozon_vorarlberg.py --station lustenau
    python3 ozon_vorarlberg.py --html fixtures/tab1O3_live.htm   # offline
    python3 ozon_vorarlberg.py --watch 1800 --log --out data.json
    python3 ozon_vorarlberg.py --demo --out data.json   # demo history
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

URL = "https://www.vorarlberg-luft.at/tab1O3.htm"

# ---------------------------------------------------------------------------
# Reference values (ug/m3)
# ---------------------------------------------------------------------------

WHO_SHORT_8H = 100   # WHO global air quality guidelines 2021, max. daily 8h mean
EU_TARGET_8H = 120   # EU target value, running 8h
AT_INFO_1H = 180     # Austrian information threshold, 1h
WHO_PEAK_SEASON = 60  # WHO long-term (mean of daily 8h maxima, 6 months)

# ---------------------------------------------------------------------------
# Stations
#
# The order assigns the colour slots in the dashboard and must not be
# reshuffled: "colour follows the entity, not its rank". Sorted by altitude
# ascending — which is also the substantive axis (valley floor -> high
# altitude) along which the ozone daily cycle plays out.
#
# Altitudes are approximations and do NOT come from the source.
# ---------------------------------------------------------------------------

STATION_ORDER = ["ATVA002", "ATVA007", "ATVA009", "ATVA008"]

STATIONS: dict[str, dict] = {
    "ATVA002": {
        "name": "Lustenau Wiesenrain",
        "short": "Lustenau",
        "chart": "Lustenau",
        "altitude_m_approx": 404,
        "kind": "tal",
        "region": "Rheintal",
    },
    "ATVA007": {
        "name": "Bludenz Herrengasse",
        "short": "Bludenz",
        "chart": "Bludenz",
        "altitude_m_approx": 570,
        "kind": "tal",
        "region": "Walgau",
    },
    "ATVA009": {
        "name": "Wald am Arlberg",
        "short": "Wald a. Arlberg",
        "chart": "Wald a. A.",
        "altitude_m_approx": 900,
        "kind": "tal",
        "region": "Klostertal",
    },
    "ATVA008": {
        "name": "Sulzberg Gmeind",
        "short": "Sulzberg",
        "chart": "Sulzberg",
        "altitude_m_approx": 1015,
        "kind": "hoehe",
        "region": "Bregenzerwald",
    },
}

# Training scale on the acute 1h value: (upper bound, status, word, advice)
TRAINING_SCALE = [
    (WHO_SHORT_8H, "good", "free",
     "Full session possible, intervals included."),
    (EU_TARGET_8H, "warning", "ok",
     "Short sessions are fine, shorten long hard blocks."),
    (AT_INFO_1H, "serious", "easy",
     "Base pace only. Move intervals to tomorrow morning."),
    (None, "critical", "indoors",
     "Information threshold reached. Cancel outdoor sport."),
]

STATUS_ORDER = {"good": 0, "warning": 1, "serious": 2, "critical": 3, "unknown": -1}

DEFAULT_HISTORY = "history.jsonl"
DEFAULT_ARCHIVE = "archive.json"
HISTORY_HOURS = 72          # how much of the series goes into data.json
PROFILE_MIN_DAYS = 2        # from how many days a daily profile is meaningful

TZ_NAME = "Europe/Vienna"


def _tz():
    """Timezone of the source. The page stamps Austrian local time."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(TZ_NAME)
    except Exception:
        # No tzdata available: fall back to the local system timezone.
        return datetime.now().astimezone().tzinfo


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class LayoutError(RuntimeError):
    """The source page no longer looks the way we expect."""


@dataclass
class Reading:
    id: str
    station: str
    short: str
    akt_1h: Optional[int]        # current 1h mean
    max_1h: Optional[int]        # daily maximum, 1h mean
    max_8h: Optional[int]        # daily maximum, running 8h mean
    prev_max_1h: Optional[int]   # previous day, 1h
    prev_max_8h: Optional[int]   # previous day, 8h


@dataclass
class Page:
    source_time: Optional[datetime]
    source_time_raw: Optional[str]
    readings: list[Reading] = field(default_factory=list)
    thresholds_on_page: list[Optional[int]] = field(default_factory=list)


_NUM = re.compile(r"-?\d+")
_UNIT = re.compile(r"g\s*/\s*m", re.I)
_TS = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s*,?\s*(\d{1,2}):(\d{2})")
_STAT_HREF = re.compile(r"stat(AT[A-Z0-9]+)\.htm", re.I)


def _to_int(cell: str) -> Optional[int]:
    """Table cell -> int. '-', empty, a unit or non-numeric -> None."""
    cell = cell.replace("\xa0", " ").strip()
    if not cell or cell.strip("-. ") == "":
        return None
    if _UNIT.search(cell):       # "ug/m3" contains a 3 — never read it as a value
        return None
    m = _NUM.search(cell.replace(",", "."))
    return int(m.group()) if m else None


def _cells(row) -> list[str]:
    return [c.get_text(" ", strip=True).replace("\xa0", " ").strip()
            for c in row.find_all(["td", "th"])]


def _parse_source_time(text: str) -> tuple[Optional[datetime], Optional[str]]:
    m = _TS.search(text)
    if not m:
        return None, None
    d, mo, y, h, mi = (int(g) for g in m.groups())
    raw = f"{d:02d}.{mo:02d}.{y} {h:02d}:{mi:02d}"
    try:
        return datetime(y, mo, d, h, mi, tzinfo=_tz()), raw
    except ValueError:
        return None, raw


def parse_html(html: str, strict: bool = False) -> Page:
    """Read the ozone table out of the page HTML.

    Station rows are recognised by the link to their detail page
    (``statATVA007.htm`` -> ``ATVA007``). The station ID is more stable than
    the display name, which has to survive typos and renamings.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        sys.exit("Missing dependency: pip install -r requirements.txt")

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    heading = " ".join(
        el.get_text(" ", strip=True) for el in soup.find_all(["h1", "h2", "p"])
    )
    src_dt, src_raw = _parse_source_time(heading or soup.get_text(" ", strip=True))

    page = Page(source_time=src_dt, source_time_raw=src_raw)
    seen: set[str] = set()

    for row in soup.find_all("tr"):
        # The source nests layout tables: the outer wrapper <tr> encloses the
        # whole values table and therefore also carries its first station
        # link. Without this filter it is read as a data row, claims the
        # station ID with garbage, and the real row is then dropped as a
        # duplicate.
        if row.find("table") is not None:
            continue

        cells = _cells(row)
        if not cells:
            continue

        # Pick up the threshold row: the page documents its own limits, which
        # makes a good sanity check.
        if cells[0].lower().startswith("schwellwert"):
            page.thresholds_on_page = [_to_int(c) for c in cells[1:6]]
            continue

        sid = None
        link = row.find("a", href=_STAT_HREF)
        if link:
            m = _STAT_HREF.search(link.get("href", ""))
            sid = m.group(1).upper() if m else None
        if sid is None:
            # Fallback: name matching, in case the detail links ever vanish.
            label = cells[0].lower()
            sid = next(
                (k for k, v in STATIONS.items() if v["name"].lower() in label
                 or v["short"].lower() in label),
                None,
            )
        if sid is None or sid in seen:
            continue
        if len(cells) < 6:          # name + 5 values; anything less is chrome
            continue
        seen.add(sid)

        nums = [_to_int(c) for c in cells[1:]]
        nums = (nums + [None] * 5)[:5]
        akt_1h, max_1h, max_8h, prev_1h, prev_8h = nums

        meta = STATIONS.get(sid, {})
        page.readings.append(
            Reading(
                id=sid,
                station=meta.get("name", cells[0]),
                short=meta.get("short", cells[0]),
                akt_1h=akt_1h,
                max_1h=max_1h,
                max_8h=max_8h,
                prev_max_1h=prev_1h,
                prev_max_8h=prev_8h,
            )
        )

    page.readings.sort(
        key=lambda r: STATION_ORDER.index(r.id) if r.id in STATION_ORDER else 99
    )

    if strict:
        _check_layout(page)
    return page


def _check_layout(page: Page) -> None:
    """Fail loudly when the source has changed structurally.

    A hard error beats silently misassigned columns.
    """
    problems = []
    if not page.readings:
        problems.append("no station row recognised")
    missing = set(STATION_ORDER) - {r.id for r in page.readings}
    if missing:
        problems.append(f"stations missing: {sorted(missing)}")
    if page.source_time is None:
        problems.append("no timestamp found in the heading")
    got = page.thresholds_on_page
    want = [AT_INFO_1H, AT_INFO_1H, EU_TARGET_8H, AT_INFO_1H, EU_TARGET_8H]
    if got and got != want:
        problems.append(f"threshold row {got}, expected {want} "
                        "(column order may have changed)")
    if problems:
        raise LayoutError("unexpected source page layout: " + "; ".join(problems))


def fetch(url: str = URL, timeout: int = 20) -> str:
    """Fetch the page and decode it correctly.

    The page declares iso-8859-1 and sticks to it. Guessing via
    ``apparent_encoding`` is unnecessary and has gone wrong on short pages
    before — umlauts in "Luftqualitaet" ended up as mojibake in the JSON.
    """
    # The filter has to wrap the import, not follow it: urllib3 emits its
    # LibreSSL notice at import time, triggered by requests. macOS system
    # Python is built against LibreSSL, so it shows up on every run there and
    # would clutter the cron log. Scoped tightly to this one statement; a
    # Homebrew Python does not need it at all.
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            import requests
        except ImportError:
            sys.exit("Missing dependency: pip install -r requirements.txt")

    headers = {
        "User-Agent": "ozon-vorarlberg/2.0 (personal, hourly)",
        "Accept": "text/html,application/xhtml+xml",
    }
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    raw = resp.content
    m = re.search(br'charset=["\']?([\w-]+)', raw[:2048], re.I)
    enc = m.group(1).decode("ascii", "ignore") if m else "iso-8859-1"
    try:
        return raw.decode(enc, errors="replace")
    except LookupError:
        return raw.decode("iso-8859-1", errors="replace")


# ---------------------------------------------------------------------------
# Assessment
# ---------------------------------------------------------------------------


def training_level(akt_1h: Optional[int]) -> dict:
    """Training traffic light on the acute 1h value."""
    if akt_1h is None:
        return {"status": "unknown", "word": "no data",
                "advice": "Station is not reporting a value right now."}
    for limit, status, word, advice in TRAINING_SCALE:
        if limit is None or akt_1h < limit:
            return {"status": status, "word": word, "advice": advice}
    return {"status": "critical", "word": "indoors", "advice": ""}


def day_assessment(max_8h: Optional[int]) -> dict:
    """Health assessment of the day, on the daily 8h maximum."""
    if max_8h is None:
        return {"status": "unknown", "label": "no data",
                "vs_who": None, "vs_eu": None}
    if max_8h >= EU_TARGET_8H:
        label, status = "above EU target", "serious"
    elif max_8h >= WHO_SHORT_8H:
        label, status = "above WHO guideline", "warning"
    else:
        label, status = "below WHO guideline", "good"
    return {
        "status": status,
        "label": label,
        "vs_who": max_8h - WHO_SHORT_8H,
        "vs_eu": max_8h - EU_TARGET_8H,
    }


def trend_for(series: list[Optional[int]], flat_band: int = 5) -> dict:
    """Trend from the last logged values. Needs at least two."""
    vals = [v for v in series if v is not None]
    if len(vals) < 2:
        return {"dir": "unknown", "delta": None, "arrow": "–"}
    # Round: since the series is merged with the archive it carries unrounded
    # floats, and the raw difference produced display noise like
    # "+18.700000000000003".
    delta = round(vals[-1] - vals[-2], 1)
    if abs(delta) < flat_band:
        return {"dir": "flat", "delta": delta, "arrow": "▬"}
    if delta > 0:
        return {"dir": "up", "delta": delta, "arrow": "▲"}
    return {"dir": "down", "delta": delta, "arrow": "▼"}


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def load_history(path: str | os.PathLike) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue          # one broken line must not kill the rest
    out.sort(key=lambda e: e.get("source_time") or "")
    return out


def append_history(path: str | os.PathLike, page: Page) -> bool:
    """Append a snapshot. False if this source timestamp is already present.

    Deduplication runs on the SOURCE timestamp, not on the fetch time: the
    page is rewritten hourly, so a cron every 15 minutes would otherwise put
    the same reading into the series four times.
    """
    if page.source_time is None:
        return False
    key = page.source_time.isoformat(timespec="minutes")
    p = Path(path)

    if p.exists():
        for line in reversed(p.read_text(encoding="utf-8").splitlines()[-200:]):
            if not line.strip():
                continue
            try:
                if json.loads(line).get("source_time") == key:
                    return False
            except json.JSONDecodeError:
                continue

    entry = {
        "source_time": key,
        "fetched_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stations": {
            r.id: {"akt_1h": r.akt_1h, "max_1h": r.max_1h, "max_8h": r.max_8h}
            for r in page.readings
        },
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return True


def prune_history(path: str | os.PathLike, keep_days: int = 3) -> int:
    """Drop old lines from history.jsonl. Returns how many were removed.

    The log is only a bridge until the EEA archive covers the same period.
    Because the EEA rewrites its container only about once per day, that lag
    swings between roughly 1 and roughly 25 hours — which is why the log alone
    carries the dashboard's 72 h window. Anything older than four days is
    never needed again.
    """
    p = Path(path)
    if not p.exists():
        return 0
    entries = load_history(p)
    if not entries:
        return 0
    try:
        newest = datetime.fromisoformat(entries[-1]["source_time"])
    except (KeyError, ValueError):
        return 0
    cutoff = newest - timedelta(days=keep_days)
    kept = []
    for e in entries:
        try:
            if datetime.fromisoformat(e["source_time"]) >= cutoff:
                kept.append(e)
        except (KeyError, ValueError):
            continue
    removed = len(entries) - len(kept)
    if removed <= 0:
        return 0
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in kept),
                   encoding="utf-8")
    tmp.replace(p)
    return removed


def history_series(entries: list[dict], hours: int = HISTORY_HOURS,
                   archive: Optional[dict] = None) -> dict:
    """Series for the dashboard, assembled from two sources.

    The EEA archive supplies validated hourly measurements but is rewritten
    only about once per day — its lag swings between roughly 1 and roughly 25
    hours. That makes it good for the initial fill, not for covering the last
    few hours. In steady state the local log carries the 72 h window; while it
    does not yet reach back that far, the archive stands in.

    Both series are labelled by WINDOW END: history.jsonl adopts the source
    page's timestamps, and eea_archive.recent_series converts from start to
    end specifically for this. Without that they would sit an hour apart.

    On identical timestamps the archive wins — that is the actual measurement,
    whereas the log only records the page's rounded display value.
    """
    per_station: dict[str, dict[str, float]] = {sid: {} for sid in STATION_ORDER}
    from_log: set[tuple[str, str]] = set()
    from_archive: set[tuple[str, str]] = set()

    for e in entries:
        st = e.get("source_time")
        if not st:
            continue
        for sid in STATION_ORDER:
            v = (e.get("stations", {}).get(sid) or {}).get("akt_1h")
            if v is not None:
                per_station[sid][st] = float(v)
                from_log.add((sid, st))

    if archive:
        for st_rec in archive.get("stations", []):
            sid = st_rec.get("id")
            rec = st_rec.get("recent") or {}
            if sid not in per_station:
                continue
            for ts, v in zip(rec.get("t", []), rec.get("v", [])):
                if v is None:
                    continue
                per_station[sid][ts] = float(v)      # archive wins
                from_archive.add((sid, ts))
                from_log.discard((sid, ts))

    all_ts = sorted({ts for d in per_station.values() for ts in d})
    if not all_ts:
        return {"t": [], "akt_1h": {}, "max_8h": {}, "points": 0,
                "span_hours": 0,
                "sources": {"archive": 0, "log": 0, "archive_until": None}}

    try:
        cutoff = datetime.fromisoformat(all_ts[-1]) - timedelta(hours=hours)
        all_ts = [t for t in all_ts if datetime.fromisoformat(t) >= cutoff]
    except ValueError:
        pass

    akt = {sid: [per_station[sid].get(t) for t in all_ts] for sid in STATION_ORDER}

    # max_8h stays from the local log: it is a daily maximum and is not drawn
    # in the series anyway.
    m8_by_ts = {sid: {} for sid in STATION_ORDER}
    for e in entries:
        st = e.get("source_time")
        for sid in STATION_ORDER:
            v = (e.get("stations", {}).get(sid) or {}).get("max_8h")
            if st and v is not None:
                m8_by_ts[sid][st] = v
    m8 = {sid: [m8_by_ts[sid].get(t) for t in all_ts] for sid in STATION_ORDER}

    span = 0
    if len(all_ts) >= 2:
        try:
            span = round((datetime.fromisoformat(all_ts[-1])
                          - datetime.fromisoformat(all_ts[0])).total_seconds() / 3600)
        except ValueError:
            span = 0

    win = set(all_ts)
    arc_ts = sorted({ts for _s, ts in from_archive})
    sources = {
        "archive": sum(1 for _s, ts in from_archive if ts in win),
        "log": sum(1 for _s, ts in from_log if ts in win),
        "archive_until": arc_ts[-1] if arc_ts else None,
    }
    return {"t": all_ts, "akt_1h": akt, "max_8h": m8, "points": len(all_ts),
            "span_hours": span, "sources": sources}


def _median(vals: Sequence[Optional[float]]) -> Optional[float]:
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return float(vals[mid]) if n % 2 else (vals[mid - 1] + vals[mid]) / 2


def hour_profile(entries: list[dict]) -> dict:
    """Daily cycle per station: median 1h value per hour of the day.

    Only meaningful from ``PROFILE_MIN_DAYS`` distinct days onwards — before
    that it is not a profile, just the one logged day with extra steps.
    """
    buckets: dict[str, dict[int, list[int]]] = {sid: {} for sid in STATION_ORDER}
    days: set[str] = set()

    for e in entries:
        st = e.get("source_time")
        if not st:
            continue
        try:
            dt = datetime.fromisoformat(st)
        except ValueError:
            continue
        days.add(dt.date().isoformat())
        for sid in STATION_ORDER:
            v = (e.get("stations", {}).get(sid) or {}).get("akt_1h")
            if v is not None:
                buckets[sid].setdefault(dt.hour, []).append(v)

    n_days = len(days)
    profile = {
        sid: [
            None if h not in buckets[sid] else _median(buckets[sid][h])
            for h in range(24)
        ]
        for sid in STATION_ORDER
    }
    best = best_window(profile) if n_days >= PROFILE_MIN_DAYS else None
    return {
        "n_days": n_days,
        "sufficient": n_days >= PROFILE_MIN_DAYS,
        "median_akt_1h": profile,
        "best_window": best,
    }


def best_window(profile: dict, width: int = 3) -> Optional[dict]:
    """Cleanest contiguous time window across all stations."""
    hourly = []
    for h in range(24):
        vals = [profile[sid][h] for sid in profile if profile[sid][h] is not None]
        hourly.append(_median(vals) if vals else None)

    best = None
    for start in range(24 - width + 1):
        chunk = hourly[start:start + width]
        if any(v is None for v in chunk):
            continue
        mean = sum(chunk) / width
        if best is None or mean < best["mean"]:
            best = {"from_hour": start, "to_hour": start + width,
                    "mean": round(mean)}
    return best


# ---------------------------------------------------------------------------
# EEA archive (optional)
# ---------------------------------------------------------------------------


def load_archive(path: str | os.PathLike) -> Optional[dict]:
    """Read archive.json if present. If it is missing everything still runs —
    just without the long-term metrics."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return rec if rec.get("stations") else None


def percentile_of(reference_sorted: list[int], value: Optional[int]) -> Optional[int]:
    """Where today's daily value sits in the reference distribution."""
    if value is None or not reference_sorted:
        return None
    return round(100 * bisect_left(reference_sorted, value) / len(reference_sorted))


def archive_block(archive: Optional[dict], readings: list[Reading]) -> Optional[dict]:
    """Prepare the archive metrics for data.json.

    Today's value deliberately comes from the LIVE source, not the archive:
    the archive lags 1 to 25 hours behind, so its maximum for the running day
    is incomplete.
    """
    if not archive:
        return None
    by_id = {s["id"]: s for s in archive.get("stations", [])}
    live_max = {r.id: r.max_1h for r in readings}

    stations = []
    for sid, st in by_id.items():
        ctx = dict(st.get("day_context") or {})
        ref = ctx.pop("reference_sorted", None)
        if ref is not None:
            ctx["today_max_1h"] = live_max.get(sid)
            ctx["percentile"] = percentile_of(ref, live_max.get(sid))
            ctx["today_from"] = "live"
        stations.append({
            "id": sid,
            "short": st.get("short"),
            "chart_label": st.get("chart_label", st.get("short")),
            "slot": st.get("slot"),
            "first": st.get("first"),
            "last": st.get("last"),
            "hours": st.get("hours"),
            "hour_profile": st.get("hour_profile"),
            "best_window": st.get("best_window"),
            "yearly": st.get("yearly"),
            "context": ctx or None,
        })

    years = sorted({y for s in by_id.values() for y in (s.get("yearly") or {})})
    return {
        "available": True,
        "built_utc": archive.get("built_utc"),
        "source": archive.get("source"),
        "overall_best_window": archive.get("overall_best_window"),
        "years": years,
        "total_hours": sum(s.get("hours") or 0 for s in by_id.values()),
        "stations": stations,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def build_record(page: Page, entries: list[dict], demo: bool = False,
                 archive: Optional[dict] = None) -> dict:
    hist = history_series(entries, archive=archive)
    prof = hour_profile(entries)

    stations = []
    for idx, r in enumerate(page.readings):
        meta = STATIONS.get(r.id, {})
        past = hist["akt_1h"].get(r.id, [])
        any_1h = max((v for v in (r.akt_1h, r.max_1h) if v is not None),
                     default=None)
        tl = training_level(r.akt_1h)
        stations.append({
            "id": r.id,
            "station": r.station,
            "short": r.short,
            "chart_label": meta.get("chart", r.short),
            "slot": idx + 1,                    # fixed colour slot in the dashboard
            "altitude_m_approx": meta.get("altitude_m_approx"),
            "kind": meta.get("kind"),
            "region": meta.get("region"),
            "akt_1h": r.akt_1h,
            "max_1h": r.max_1h,
            "max_8h": r.max_8h,
            "prev_max_1h": r.prev_max_1h,
            "prev_max_8h": r.prev_max_8h,
            "training": tl,
            "day": day_assessment(r.max_8h),
            "trend": trend_for(past),
            "exceeds_eu_target": (r.max_8h is not None
                                  and r.max_8h >= EU_TARGET_8H),
            "exceeds_at_info": (any_1h is not None and any_1h >= AT_INFO_1H),
        })

    ranked = [s for s in stations if s["akt_1h"] is not None]
    ranked.sort(key=lambda s: s["akt_1h"])
    worst = max(
        (s for s in stations if s["akt_1h"] is not None),
        key=lambda s: s["akt_1h"], default=None,
    )
    overall = "unknown"
    if stations:
        overall = max(
            (s["training"]["status"] for s in stations),
            key=lambda st: STATUS_ORDER.get(st, -1),
        )

    return {
        "schema": 2,
        "demo": demo,
        "source": URL,
        "source_time": (page.source_time.isoformat(timespec="minutes")
                        if page.source_time else None),
        "source_time_raw": page.source_time_raw,
        "fetched_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "timezone": TZ_NAME,
        "thresholds": {
            "who_short_8h": WHO_SHORT_8H,
            "eu_target_8h": EU_TARGET_8H,
            "at_info_1h": AT_INFO_1H,
            "who_peak_season": WHO_PEAK_SEASON,
        },
        "training_scale": [
            {"below": limit, "status": status, "word": word, "advice": advice}
            for limit, status, word, advice in TRAINING_SCALE
        ],
        "summary": {
            "overall_training_status": overall,
            "cleanest": ranked[0]["short"] if ranked else None,
            "cleanest_value": ranked[0]["akt_1h"] if ranked else None,
            "worst": worst["short"] if worst else None,
            "worst_value": worst["akt_1h"] if worst else None,
            "any_info_threshold": any(s["exceeds_at_info"] for s in stations),
            "eu_target_exceeded_at": [s["short"] for s in stations
                                      if s["exceeds_eu_target"]],
        },
        "stations": stations,
        "history": hist,
        "hour_profile": prof,
        "archive": archive_block(archive, page.readings),
    }


def print_compact(rec: dict) -> None:
    print(f"# {rec.get('source_time_raw') or '?'}  "
          f"(source: vorarlberg-luft.at)")
    for s in rec["stations"]:
        t = s["training"]
        print(f"{t['status']:9} {t['word']:8} {s['short']:16} "
              f"akt={s['akt_1h'] if s['akt_1h'] is not None else '-':>4} "
              f"{s['trend']['arrow']}  "
              f"1h-Max={s['max_1h'] if s['max_1h'] is not None else '-':>4} "
              f"8h-Max={s['max_8h'] if s['max_8h'] is not None else '-':>4} "
              f"[{s['day']['label']}]")
    su = rec["summary"]
    if su["cleanest"]:
        print(f"\n-> Cleanest station: {su['cleanest']} "
              f"({su['cleanest_value']} ug/m3)")
    arc = rec.get("archive")
    if arc:
        bw = arc.get("overall_best_window") or {}
        print(f"-> Archive: {arc['total_hours']} hourly values, "
              f"{arc['years'][0]}-{arc['years'][-1]}")
        if bw:
            span = f"{arc['years'][0]}-{arc['years'][-1]}" if arc.get("years") else "?"
            print(f"-> Best training window ({span}, season data): "
                  f"{bw['from_hour']:02d}-{bw['to_hour']:02d}, "
                  f"median {bw['mean']} ug/m3")
        for st in arc["stations"]:
            c = st.get("context") or {}
            if c.get("percentile") is not None:
                print(f"   {st['short']:16} today {c['today_max_1h']:>3} "
                      f"= {c['percentile']:>3}th percentile "
                      f"(median {c['median']}, max {c['max']} since "
                      f"{c['years'][0]})")
    h = rec["history"]
    print(f"-> History: {h['points']} "
          f"point{'' if h['points'] == 1 else 's'} / {h['span_hours']} h")


# ---------------------------------------------------------------------------
# Demo data
# ---------------------------------------------------------------------------


def demo_history(hours: int = 48) -> tuple[Page, list[dict]]:
    """Synthesise a plausible history so the dashboard shows something at once.

    Deterministic (no randomness) so the demo stays reproducible. The physics
    is roughly reproduced: valley stations with a deep night minimum and an
    afternoon peak, Sulzberg as the high-altitude station flat and constantly
    elevated.
    """
    tz = _tz()
    now = datetime.now(tz).replace(minute=0, second=0, microsecond=0)
    shape = {                     # (mean, amplitude, peak hour)
        "ATVA002": (105, 60, 16),
        "ATVA007": (100, 55, 16),
        "ATVA009": (95, 45, 15),
        "ATVA008": (140, 18, 15),
    }

    entries = []
    for back in range(hours, -1, -1):
        ts = now - timedelta(hours=back)
        day_damp = 1.0 - 0.12 * (back // 24)      # earlier days slightly weaker
        stations = {}
        for sid, (base, amp, peak) in shape.items():
            phase = (ts.hour - peak) / 24 * 2 * math.pi
            v = base + amp * math.cos(phase) * day_damp
            stations[sid] = {
                "akt_1h": max(4, round(v)),
                "max_1h": max(4, round(base + amp * day_damp)),
                "max_8h": max(4, round(base + amp * 0.45 * day_damp)),
            }
        entries.append({
            "source_time": ts.isoformat(timespec="minutes"),
            "fetched_utc": ts.astimezone(timezone.utc)
                             .isoformat(timespec="seconds"),
            "stations": stations,
        })

    last = entries[-1]["stations"]
    page = Page(
        source_time=now,
        source_time_raw=now.strftime("%d.%m.%Y %H:%M"),
        thresholds_on_page=[AT_INFO_1H, AT_INFO_1H, EU_TARGET_8H,
                            AT_INFO_1H, EU_TARGET_8H],
        readings=[
            Reading(
                id=sid,
                station=STATIONS[sid]["name"],
                short=STATIONS[sid]["short"],
                akt_1h=last[sid]["akt_1h"],
                max_1h=last[sid]["max_1h"],
                max_8h=last[sid]["max_8h"],
                prev_max_1h=last[sid]["max_1h"] - 6,
                prev_max_8h=last[sid]["max_8h"] - 4,
            )
            for sid in STATION_ORDER
        ],
    )
    return page, entries


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Ozone Vorarlberg — scraper, history logger, assessment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="cron line:  ozon_vorarlberg.py --log --out data.json --quiet",
    )
    ap.add_argument("--station", help="only stations matching this text")
    ap.add_argument("--html", help="parse a local HTML file instead of fetching")
    ap.add_argument("--url", default=URL, help="alternative source URL")
    ap.add_argument("--out", metavar="FILE",
                    help="write JSON to this file (for the dashboard)")
    ap.add_argument("--log", action="store_true",
                    help="append a snapshot to the history")
    ap.add_argument("--history", default=DEFAULT_HISTORY, metavar="FILE",
                    help=f"path of the history file (default: {DEFAULT_HISTORY})")
    ap.add_argument("--prune-history", type=int, default=None, metavar="DAYS",
                    help="drop log lines older than N days (recommended: 4)")
    ap.add_argument("--archive", default=DEFAULT_ARCHIVE, metavar="FILE",
                    help="EEA archive from eea_archive.py --build. If absent, "
                         "everything runs on without long-term metrics.")
    ap.add_argument("--no-archive", action="store_true",
                    help="ignore the archive even when present")
    ap.add_argument("--watch", type=int, metavar="SECONDS",
                    help="loop forever, refetch every N seconds")
    ap.add_argument("--compact", action="store_true",
                    help="plain text instead of JSON, one line per station")
    ap.add_argument("--strict", action="store_true",
                    help="abort with an error when the layout is unexpected")
    ap.add_argument("--demo", action="store_true",
                    help="synthetic demo data instead of fetching")
    ap.add_argument("--quiet", action="store_true",
                    help="no output on stdout (only write --out)")
    args = ap.parse_args(argv)

    if args.demo and (args.html or args.log):
        ap.error("--demo cannot be combined with --html or --log")

    def run_once() -> int:
        if args.demo:
            page, entries = demo_history()
            demo = True
        else:
            demo = False
            if args.html:
                html = Path(args.html).read_bytes().decode(
                    "iso-8859-1", errors="replace")
                # Fixtures may be UTF-8; only switch when it actually decodes.
                try:
                    text = Path(args.html).read_text(encoding="utf-8")
                    if "�" not in text:
                        html = text
                except (UnicodeDecodeError, ValueError):
                    pass
            else:
                html = fetch(args.url)

            try:
                page = parse_html(html, strict=args.strict)
            except LayoutError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 2

            if not page.readings:
                print("No station recognised — source layout changed?",
                      file=sys.stderr)
                return 2

            if args.log:
                added = append_history(args.history, page)
                if not args.quiet and not args.compact:
                    print(
                        f"# history: {'new entry' if added else 'unchanged'}"
                        f" ({args.history})", file=sys.stderr)

            if args.prune_history:
                gone = prune_history(args.history, args.prune_history)
                if gone and not args.quiet:
                    print(f"# history: {gone} old lines removed",
                          file=sys.stderr)
            entries = load_history(args.history)

        if args.station:
            needle = args.station.lower()
            page.readings = [r for r in page.readings
                             if needle in r.station.lower()
                             or needle in r.short.lower()
                             or needle == r.id.lower()]
            if not page.readings:
                print(f"No station matches {args.station!r}.",
                      file=sys.stderr)
                return 1

        archive = None if args.no_archive else load_archive(args.archive)
        rec = build_record(page, entries, demo=demo, archive=archive)

        if args.out:
            out = Path(args.out)
            tmp = out.with_suffix(out.suffix + ".tmp")
            tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(out)      # atomic: the dashboard never reads a half file

        if not args.quiet:
            if args.compact:
                print_compact(rec)
            elif not args.out:
                print(json.dumps(rec, ensure_ascii=False, indent=2))
            else:
                n = rec["history"]["points"]
                print(f"{args.out} written "
                      f"({n} history point{'' if n == 1 else 's'}).")
        return 0

    if args.watch:
        while True:
            try:
                run_once()
            except KeyboardInterrupt:
                return 130
            except Exception as exc:                  # the loop must keep going
                print(f"fetch failed: {exc}", file=sys.stderr)
            time.sleep(args.watch)
    return run_once()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
