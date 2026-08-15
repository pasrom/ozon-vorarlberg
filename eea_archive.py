#!/usr/bin/env python3
"""
eea_archive.py — fetch up to 39 years of hourly ozone readings for the four
Vorarlberg stations from the EEA archive and condense them into metrics.

Why this exists
---------------
vorarlberg-luft.at publishes no numeric series (the linked "graphical course"
pages are JPEGs), and the Umweltbundesamt time-series tool at
luft.umweltbundesamt.at/pub/map_chart/index.pl renders nothing but PNGs
either. The EEA, by contrast, stores the very same measurements as Parquet in
publicly readable Azure blob containers — no API key.

    airquality-p          E2a, unverified   2025-01-01 .. now minus 1-25 h
    airquality-p-e1a      E1a, verified     2013-01-01 .. 2024-12-31
    airquality-p-airbase  AIRBASE           from 1988 (Lustenau) .. 2012-12-31

Together that is 1988 to today, hourly — different lengths per station:
Lustenau from 1988-01, Sulzberg from 1989-05, Wald am Arlberg from 2003-01,
Bludenz from 2004-01. Verified: the daily maxima computed from these files
match the previous-day values published by vorarlberg-luft.at exactly, for all
four stations.

Timezone and labelling — the pitfall that matters most
------------------------------------------------------
The "Start" column is timezone-naive and holds UTC. It marks the BEGINNING of
the averaged hour. The regional page, by contrast, labels its values by the
END of the hour and in local time: the value shown there as "13:00" sits in
the EEA data under Start 10:00 (= 12:00 CEST, window 12-13 local).

Established against 11 hourly values across four stations, all exact. Being
one hour off here shifts the entire daily cycle, and with it the recommended
training window.

The daily cycle in this module is labelled by the window START in local time:
"06" means the hour 06:00-07:00. That is the reading a training window needs
("head out at 06:00").

Daily metrics (daily maxima, exceedance days) are aggregated in fixed CET
instead — that is what the source does. See AGG_TZ.

Usage
-----
    python3 eea_archive.py --build              # fetch, cache, archive.json
    python3 eea_archive.py --build --since 2013 # only from 2013
    python3 eea_archive.py --stats              # metrics from the cache
    python3 eea_archive.py --coverage           # coverage per station/year
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from ozon_vorarlberg import (
    AT_INFO_1H,
    EU_TARGET_8H,
    STATION_ORDER,
    STATIONS,
    WHO_PEAK_SEASON,
    WHO_SHORT_8H,
    _median,
    _tz,
)

BLOB = "https://eeadmz1batchservice02.blob.core.windows.net"

# The EEA "Start" column is timezone-naive and holds UTC. Established against
# the official regional page: 11 of 11 hourly values match exactly (see
# test_eea_archive.TestAgainstOfficialSource). An earlier assumption of "fixed
# CET" was one hour off — it came from a phase correlation against model data
# that barely separates 0 from -1 h (r 0.87 vs 0.86). Exact integer hits beat
# correlation.
EEA_TZ = timezone.utc

# Day boundary for aggregation. The Austrian immission database aggregates
# daily metrics in FIXED CET (UTC+1, no daylight saving) while displaying the
# individual values in local time. Computed with the day boundary in CEST the
# daily maxima of the 8h mean were reproducibly wrong (3 of 8); with fixed CET
# all 8 of 8 reference values match exactly.
AGG_TZ = timezone(timedelta(hours=1))

# EEA sampling point per station. The code has two parts: 08 is the network
# (Vorarlberg in the Austrian immission data network), the second number is
# the station ID. The same number appears there as station_info('08','0503').
SAMPLING_POINT = {
    "ATVA002": "SPO.08.0706.983.7.1",     # Lustenau Wiesenrain   (IDV 08/0706)
    "ATVA007": "SPO.08.2708.5527.7.1",    # Bludenz Herrengasse   (IDV 08/2708)
    "ATVA009": "SPO.08.2801.3213.7.1",    # Wald am Arlberg S16   (IDV 08/2801)
    "ATVA008": "SPO.08.0503.3670.7.1",    # Sulzberg Gmeind       (IDV 08/0503)
}

# (container, first year, last year) — ascending. Later containers win on
# overlap because they carry the more current data.
# The airbase container reaches much further back than first assumed: Lustenau
# from 1988-01, Sulzberg from 1989-05, Wald am Arlberg from 2003, Bludenz from
# 2004. That is why nothing is clipped to 2003 any more — the year bounds now
# only serve to skip unnecessary downloads.
DATASETS = [
    ("airquality-p-airbase", 1988, 2012, "airbase"),
    ("airquality-p-e1a", 2013, 2024, "e1a"),
    ("airquality-p", 2025, 9999, "e2a"),
]

FIRST_YEAR = 1988      # earliest data of all (Lustenau)

CACHE = Path("cache/eea")
ARCHIVE_JSON = Path("archive.json")

# Ozone season. The daily cycle outside it is a different regime and would
# water down the training window.
SEASON_MONTHS = (4, 5, 6, 7, 8, 9)
PROFILE_YEARS = 5          # daily cycle from the last N years only
RECENT_DAYS = 21           # this many days of hourly values go into archive.json
EIGHT_H_MIN_VALID = 6      # this many of 8 hours must be valid


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def blob_url(container: str, point: str) -> str:
    return f"{BLOB}/{container}/AT/{point}.parquet"


def cache_path(container: str, point: str) -> Path:
    return CACHE / f"{container}__{point}.parquet"


def blob_last_modified(container: str, point: str,
                       timeout: int = 30) -> Optional[str]:
    """When did the EEA last write this file?

    The E2a container is rewritten only about once per day. That makes the
    data lag swing between roughly 1 h right afterwards and roughly 25 h just
    before. The value is recorded so the cadence becomes evidence rather than
    guesswork.
    """
    req = urllib.request.Request(blob_url(container, point), method="HEAD")
    req.add_header("User-Agent", "ozon-vorarlberg/2.0 (privat)")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.headers.get("Last-Modified")
    except Exception:
        return None


def download(container: str, point: str, refresh: bool = False,
             timeout: int = 180) -> Optional[Path]:
    """Fetch one Parquet file and cache it. None if it does not exist."""
    p = cache_path(container, point)
    if p.exists() and not refresh:
        return p
    url = blob_url(container, point)
    p.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url, headers={"User-Agent": "ozon-vorarlberg/2.0 (privat)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(p)
    return p


def read_parquet(path: Path) -> list[tuple[datetime, float]]:
    """(time in Europe/Vienna, value) for every valid hour.

    Validity: 1..3 valid, -1 invalid, -99 not measured. Anything below 1 is
    dropped — an invalid value is worse than a gap.
    """
    try:
        import pyarrow.parquet as pq
    except ImportError:
        sys.exit("Missing dependency: pip install -r requirements.txt")

    t = pq.read_table(path, columns=["Start", "Value", "Validity"]).to_pydict()
    tz = _tz()
    out: list[tuple[datetime, float]] = []
    for s, v, val in zip(t["Start"], t["Value"], t["Validity"]):
        if v is None or val is None or int(val) < 1:
            continue
        # Read the naive stamp as UTC, then turn it into local time.
        ts = s.replace(tzinfo=EEA_TZ).astimezone(tz) if s.tzinfo is None \
            else s.astimezone(tz)
        out.append((ts, float(v)))
    return out


def load_station(sid: str, since: int = FIRST_YEAR, refresh: bool = False,
                 quiet: bool = False) -> list[tuple[datetime, float]]:
    """Load every container of a station and merge them into one series."""
    point = SAMPLING_POINT[sid]
    merged: dict[datetime, float] = {}
    for container, _y0, y1, _tag in DATASETS:
        if y1 < since:
            continue
        p = download(container, point, refresh=refresh)
        if p is None:
            if not quiet:
                print(f"  {sid} {container}: not present", file=sys.stderr)
            continue
        rows = read_parquet(p)
        kept = 0
        for ts, v in rows:
            if ts.year < since:
                continue
            merged[ts] = v      # spaeterer Container gewinnt
            kept += 1
        if not quiet:
            print(f"  {sid} {container:22} {kept:>7} hours", file=sys.stderr)
    return sorted(merged.items())


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def rolling_8h(series: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    """Running 8h mean, assigned to the END of the window (EU convention).

    ``series`` is labelled by the window START of each hour (that is what
    read_parquet returns). The hourly value with start S covers S..S+1, so an
    8h window built from starts S-7..S covers S-7..S+1 and ends at S+1 — not
    at S. Exactly this one hour was missing here at first, which made the
    daily maximum reproducibly about 6 ug/m3 too high: the cut was allowed to
    take one afternoon hour too many.

    Requires a real chain of hours: gaps break the window rather than being
    averaged across.
    """
    out = []
    n = len(series)
    for i in range(n):
        last_start = series[i][0]
        vals = []
        j = i
        while j >= 0:
            gap = (last_start - series[j][0]).total_seconds() / 3600
            if gap > 7:
                break
            vals.append(series[j][1])
            j -= 1
        if len(vals) >= EIGHT_H_MIN_VALID:
            out.append((last_start + timedelta(hours=1), sum(vals) / len(vals)))
    return out


def day_of_hour(start_ts: datetime) -> date:
    """Day an hourly value is assigned to (stamp = window start)."""
    return start_ts.astimezone(AGG_TZ).date()


def day_of_window(end_ts: datetime) -> date:
    """Day an 8h window is assigned to (stamp = window end).

    By EU convention a window belongs to the day on which it ENDS, with 24:00
    still counting towards the old day. The first window of a day is therefore
    the one starting the previous evening and ending during the night — it
    drags the high evening along and is often the daily maximum on quiet
    mornings.
    """
    return (end_ts.astimezone(AGG_TZ) - timedelta(seconds=1)).date()


def daily_max(pairs: Iterable[tuple[datetime, float]],
              day_fn=day_of_hour) -> dict[date, float]:
    best: dict[date, float] = {}
    for ts, v in pairs:
        d = day_fn(ts)
        if d not in best or v > best[d]:
            best[d] = v
    return best


def peak_season_mean(dmax8: dict[date, float], year: int) -> Optional[float]:
    """WHO long-term metric: mean of the daily 8h maxima over the six
    consecutive months with the highest such mean."""
    by_month: dict[int, list[float]] = defaultdict(list)
    for d, v in dmax8.items():
        if d.year == year:
            by_month[d.month].append(v)
    best = None
    for start in range(1, 8):                      # windows starting Jan..Jul
        months = range(start, start + 6)
        vals = [v for m in months for v in by_month.get(m, [])]
        if len(vals) < 120:                        # too thin for the metric
            continue
        m = sum(vals) / len(vals)
        if best is None or m > best[0]:
            best = (m, start)
    return round(best[0], 1) if best else None


def yearly_stats(series: list[tuple[datetime, float]]) -> dict:
    s8 = rolling_8h(series)
    dmax8 = daily_max(s8, day_of_window)      # stamp = window end
    dmax1 = daily_max(series, day_of_hour)    # stamp = window start

    hours_by_year: dict[int, int] = defaultdict(int)
    max1_by_year: dict[int, float] = {}
    over180_by_year: dict[int, int] = defaultdict(int)
    for ts, v in series:
        y = ts.year
        hours_by_year[y] += 1
        if y not in max1_by_year or v > max1_by_year[y]:
            max1_by_year[y] = v
        if v >= AT_INFO_1H:
            over180_by_year[y] += 1

    this_year = datetime.now(_tz()).year
    out = {}
    for y in sorted(hours_by_year):
        days8 = {d: v for d, v in dmax8.items() if d.year == y}
        hours = hours_by_year[y]
        total = 366 * 24 if (y % 4 == 0 and (y % 100 or y % 400 == 0)) else 365 * 24
        out[str(y)] = {
            "hours": hours,
            "coverage": round(hours / total, 3),
            "max_1h": round(max1_by_year[y]),
            "max_8h": round(max(days8.values())) if days8 else None,
            "days_8h_over_120": sum(1 for v in days8.values() if v > EU_TARGET_8H),
            "days_8h_over_100": sum(1 for v in days8.values() if v > WHO_SHORT_8H),
            "hours_1h_over_180": over180_by_year[y],
            "peak_season_mean": peak_season_mean(dmax8, y),
            "days_measured": len({d for d in dmax1 if d.year == y}),
            # The running year is not over: days>120 and peak_season_mean
            # are interim figures, not annual values.
            "partial": y >= this_year,
        }
    return out


def hour_profile(series: list[tuple[datetime, float]],
                 last_years: int = PROFILE_YEARS) -> dict:
    """Daily cycle of the ozone season: median and quartiles per hour."""
    if not series:
        return {"median": [None] * 24, "p25": [None] * 24, "p75": [None] * 24,
                "n_days": 0, "years": []}
    newest = series[-1][0].year
    years = [y for y in range(newest - last_years + 1, newest + 1)]
    buckets: dict[int, list[float]] = defaultdict(list)
    days = set()
    for ts, v in series:
        if ts.year not in years or ts.month not in SEASON_MONTHS:
            continue
        buckets[ts.hour].append(v)
        days.add(ts.date())

    def q(vals: list[float], frac: float) -> Optional[float]:
        if not vals:
            return None
        vs = sorted(vals)
        i = min(len(vs) - 1, max(0, int(round(frac * (len(vs) - 1)))))
        return round(vs[i], 1)

    return {
        "median": [q(buckets.get(h, []), .5) for h in range(24)],
        "p25": [q(buckets.get(h, []), .25) for h in range(24)],
        "p75": [q(buckets.get(h, []), .75) for h in range(24)],
        "n_days": len(days),
        "years": years,
        "season_months": list(SEASON_MONTHS),
    }


def recent_series(series: list[tuple[datetime, float]],
                  days: int = RECENT_DAYS) -> dict:
    """The last few days as an hourly series, for the dashboard's curve.

    NOTE on labelling: by WINDOW END here, not by start. That makes the series
    join history.jsonl seamlessly, which adopts the regional page's timestamps
    — and those are labelled by window end. Without this conversion the
    archive and the local log would sit an hour apart.
    """
    if not series:
        return {"t": [], "v": [], "labelled": "window_end"}
    cutoff = series[-1][0] - timedelta(days=days)
    t, v = [], []
    for ts, val in series:
        if ts < cutoff:
            continue
        end = ts + timedelta(hours=1)
        t.append(end.isoformat(timespec="minutes"))
        v.append(round(val, 1))
    return {"t": t, "v": v, "labelled": "window_end"}


def best_window(profile: dict, width: int = 3) -> Optional[dict]:
    med = profile.get("median") or []
    best = None
    for start in range(24 - width + 1):
        chunk = med[start:start + width]
        if any(v is None for v in chunk):
            continue
        m = sum(chunk) / width
        if best is None or m < best["mean"]:
            best = {"from_hour": start, "to_hour": start + width, "mean": round(m)}
    return best


def day_of_year_context(series: list[tuple[datetime, float]],
                        target: date, window_days: int = 3) -> Optional[dict]:
    """Reference distribution of the 1h daily maxima in the same calendar
    window of previous years. Answers "is this a lot, or normal for mid-August".

    Deliberately WITHOUT today's value: the archive lags 1 to 25 hours behind,
    the running day is incomplete in it, and its maximum would therefore be a
    morning value compared against complete days. That produced reproducibly
    absurd percentiles (Lustenau 67 instead of 163 ug/m3). ozon_vorarlberg.py
    fills in today's value from the live source.
    """
    dmax1 = daily_max(series)
    ref = []
    for d, v in dmax1.items():
        if d.year == target.year:
            continue
        try:
            same = date(d.year, target.month, target.day)
        except ValueError:
            continue
        if abs((d - same).days) <= window_days:
            ref.append(round(v))
    if len(ref) < 10:
        return None
    ref.sort()

    def q(frac: float) -> int:
        return ref[min(len(ref) - 1, max(0, int(round(frac * (len(ref) - 1)))))]

    return {
        "month": target.month,
        "day": target.day,
        "window_days": window_days,
        "n_reference_days": len(ref),
        "years": sorted({d.year for d in dmax1 if d.year != target.year}),
        "p10": q(.10), "median": q(.50), "p90": q(.90), "max": ref[-1],
        # Sorted reference values so the percentile of the live daily value
        # stays computable without touching the archive again.
        "reference_sorted": ref,
    }


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build(since: int = FIRST_YEAR, refresh: bool = False,
          quiet: bool = False) -> dict:
    tz = _tz()
    now = datetime.now(tz)
    stations = {}
    for idx, sid in enumerate(STATION_ORDER):
        if not quiet:
            print(f"{STATIONS[sid]['short']}:", file=sys.stderr)
        series = load_station(sid, since=since, refresh=refresh, quiet=quiet)
        if not series:
            continue
        prof = hour_profile(series)
        stations[sid] = {
            "id": sid,
            "short": STATIONS[sid]["short"],
            "chart_label": STATIONS[sid].get("chart", STATIONS[sid]["short"]),
            "slot": idx + 1,
            "sampling_point": SAMPLING_POINT[sid],
            "first": series[0][0].isoformat(timespec="minutes"),
            "last": series[-1][0].isoformat(timespec="minutes"),
            "hours": len(series),
            "hour_profile": prof,
            "recent": recent_series(series),
            "best_window": best_window(prof),
            "yearly": yearly_stats(series),
            "day_context": day_of_year_context(series, now.date()),
        }

    # Best window across all stations: median of the station medians.
    combined = []
    for h in range(24):
        vals = [s["hour_profile"]["median"][h] for s in stations.values()
                if s["hour_profile"]["median"][h] is not None]
        combined.append(_median(vals) if vals else None)
    overall = best_window({"median": combined})

    live = DATASETS[-1][0]      # E2a: the container that keeps growing
    lm = blob_last_modified(live, SAMPLING_POINT[STATION_ORDER[0]])
    newest = max((s["last"] for s in stations.values()), default=None)

    return {
        "schema": 1,
        "built_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "upstream": {
            "container": live,
            "last_modified": lm,
            "newest_value": newest,
            "note": ("The EEA rewrites this container about once per day. "
                     "The data lag therefore swings between roughly 1 h and "
                     "roughly 25 h — which is why the local log carries the "
                     "72 h window, not the archive."),
        },
        "timezone": str(tz),
        "source": {
            "blob": BLOB,
            "datasets": [{"container": c, "from": y0,
                          "to": (None if y1 == 9999 else y1), "tag": tag}
                         for c, y0, y1, tag in DATASETS],
            "note": ("EEA Air Quality e-Reporting. Source timestamps in UTC, "
                     "converted to Europe/Vienna here. Daily metrics are "
                     "aggregated in fixed CET, as the source does."),
        },
        "thresholds": {
            "who_short_8h": WHO_SHORT_8H,
            "eu_target_8h": EU_TARGET_8H,
            "at_info_1h": AT_INFO_1H,
            "who_peak_season": WHO_PEAK_SEASON,
        },
        "overall_best_window": overall,
        "stations": [stations[s] for s in STATION_ORDER if s in stations],
    }


def print_coverage(rec: dict) -> None:
    years = sorted({y for s in rec["stations"] for y in s["yearly"]})
    print(f"{'Year':6}" + "".join(f"{s['short'][:9]:>11}" for s in rec["stations"]))
    for y in years:
        row = f"{y:6}"
        for s in rec["stations"]:
            d = s["yearly"].get(y)
            row += f"{(str(round(d['coverage']*100)) + '%') if d else '-':>11}"
        print(row)


def print_stats(rec: dict) -> None:
    for s in rec["stations"]:
        print(f"\n=== {s['short']} ({s['hours']} hours, "
              f"{s['first'][:10]} .. {s['last'][:10]}) ===")
        bw = s["best_window"]
        if bw:
            print(f"  Best window: {bw['from_hour']:02d}-{bw['to_hour']:02d} "
                  f"(median {bw['mean']} ug/m3, season Apr-Sep, "
                  f"{s['hour_profile']['n_days']} days)")
        dc = s.get("day_context")
        if dc and dc.get("today_max_1h") is not None:
            print(f"  Today {dc['today_max_1h']} ug/m3 = {dc.get('percentile')}th "
                  f"percentile of the calendar window "
                  f"(median {dc['median']}, max {dc['max']})")
        print(f"  {'Year':6}{'PeakSeason':>12}{'Days>120':>10}"
              f"{'Hrs>180':>9}{'Max1h':>7}{'Cov.':>7}")
        for y in sorted(s["yearly"]):
            d = s["yearly"][y]
            if d["coverage"] < .5:
                continue
            print(f"  {y:6}{str(d['peak_season_mean'] or '-'):>12}"
                  f"{d['days_8h_over_120']:>10}{d['hours_1h_over_180']:>9}"
                  f"{d['max_1h']:>7}{round(d['coverage']*100):>6}%")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Fetch and condense the EEA ozone archive for Vorarlberg")
    ap.add_argument("--build", action="store_true",
                    help="fetch, cache and write archive.json")
    ap.add_argument("--stats", action="store_true", help="print the metrics")
    ap.add_argument("--coverage", action="store_true",
                    help="data coverage per station and year")
    ap.add_argument("--since", type=int, default=FIRST_YEAR, metavar="YEAR",
                    help=f"only from this year onwards (default {FIRST_YEAR} = everything)")
    ap.add_argument("--out", default=str(ARCHIVE_JSON), metavar="FILE")
    ap.add_argument("--refresh", action="store_true",
                    help="discard the cache and refetch")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if not (args.build or args.stats or args.coverage):
        ap.error("nothing to do — pass --build, --stats or --coverage")

    rec = build(since=args.since, refresh=args.refresh, quiet=args.quiet)
    if not rec["stations"]:
        print("No station loaded.", file=sys.stderr)
        return 2

    up = rec.get("upstream") or {}
    if not args.quiet and up.get("last_modified"):
        print(f"\nEEA container last written: {up['last_modified']}",
              file=sys.stderr)
        print(f"Newest reading:            {up.get('newest_value')}",
              file=sys.stderr)

    if args.build:
        out = Path(args.out)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(out)
        if not args.quiet:
            total = sum(s["hours"] for s in rec["stations"])
            print(f"\n{out} written — {total} hourly values, "
                  f"{len(rec['stations'])} stations.")
    if args.coverage:
        print(); print_coverage(rec)
    if args.stats:
        print_stats(rec)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
