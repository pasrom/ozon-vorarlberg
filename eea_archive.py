#!/usr/bin/env python3
"""
eea_archive.py — 23 Jahre Ozon-Stundenwerte fuer die vier Vorarlberger
Stationen aus dem EEA-Archiv holen und zu Kennzahlen verdichten.

Warum es das gibt
-----------------
vorarlberg-luft.at veroeffentlicht keinen Zahlen-Verlauf (die verlinkten
"Grafischer Verlauf"-Seiten sind JPEGs), und der Umweltbundesamt-Zeitverlauf
unter luft.umweltbundesamt.at/pub/map_chart/index.pl rendert ebenfalls nur PNGs.
Die EEA dagegen legt dieselben Messwerte als Parquet in oeffentlich lesbaren
Azure-Blob-Containern ab — ohne API-Key.

    airquality-p          E2a, ungeprueft   2025-01-01 .. heute minus 1-25 h
    airquality-p-e1a      E1a, geprueft     2013-01-01 .. 2024-12-31
    airquality-p-airbase  AIRBASE           ab 1988 (Lustenau) .. 2012-12-31

Zusammen also 1988 bis heute, stuendlich — je Station unterschiedlich lang:
Lustenau ab 1988-01, Sulzberg ab 1989-05, Wald am Arlberg ab 2003-01, Bludenz
ab 2004-01. Verifiziert: die Tagesmaxima aus
diesen Dateien stimmen fuer alle vier Stationen exakt mit den Vortagswerten
ueberein, die vorarlberg-luft.at ausgibt.

Zeitzone und Beschriftung — der wichtigste Fallstrick
-----------------------------------------------------
Die Spalte "Start" ist zeitzonenlos und steht in UTC. Sie bezeichnet den
BEGINN der gemittelten Stunde. Die Landesseite dagegen beschriftet ihre Werte
nach dem ENDE der Stunde und in Lokalzeit: der auf der Seite mit "13:00"
ausgewiesene Wert steckt im EEA-Datensatz unter Start 10:00 (= 12:00 MESZ,
Fenster 12-13 Uhr lokal).

Belegt an 11 Stundenwerten aus vier Stationen, alle exakt. Wer hier eine
Stunde falsch liegt, verschiebt den ganzen Tagesgang und damit das empfohlene
Trainingsfenster.

Der Tagesgang in diesem Modul ist nach dem Fenster-START in Lokalzeit
beschriftet: "06 Uhr" heisst die Stunde 06:00-07:00. Das ist die Lesart, die
ein Trainingsfenster braucht ("um 06:00 rausgehen").

Tageskennzahlen (Tagesmaxima, Ueberschreitungstage) werden dagegen in fester
MEZ aggregiert — so macht es die Quelle. Siehe AGG_TZ.

Aufrufe
-------
    python3 eea_archive.py --build              # laden, cachen, archive.json
    python3 eea_archive.py --build --since 2013 # nur ab 2013
    python3 eea_archive.py --stats              # Kennzahlen aus dem Cache
    python3 eea_archive.py --coverage           # Abdeckung pro Station/Jahr
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

# Die EEA-Spalte "Start" ist zeitzonenlos und steht in UTC. Belegt gegen die
# offizielle Landesseite: 11 von 11 Stundenwerten treffen exakt (siehe
# test_eea_archive.TestAgainstOfficialSource). Eine frueher hier stehende
# Annahme "feste MEZ" war um eine Stunde falsch — sie stammte aus einer
# Phasenkorrelation gegen Modelldaten, die zwischen 0 und -1 h kaum trennt
# (r 0,87 gegen 0,86). Exakte Integer-Treffer schlagen Korrelation.
EEA_TZ = timezone.utc

# Tagesgrenze der Aggregation. Die oesterreichische Immissionsdatenbank
# aggregiert Tageskennzahlen in FESTER MEZ (UTC+1, ohne Sommerzeit) und zeigt
# die Einzelwerte in Lokalzeit an. Mit der Tagesgrenze in MESZ gerechnet
# stimmten die Tagesmaxima des 8h-Mittels reproduzierbar nicht (3 von 8), mit
# fester MEZ stimmen alle 8 von 8 Referenzwerten exakt.
AGG_TZ = timezone(timedelta(hours=1))

# EEA-Messpunkt je Station. Der Code ist zweiteilig: 08 ist das Netz
# (Vorarlberg im Immissionsdatenverbund), die zweite Zahl die Stations-ID.
# Dieselbe Nummer taucht im IDV als station_info('08','0503') auf.
SAMPLING_POINT = {
    "ATVA002": "SPO.08.0706.983.7.1",     # Lustenau Wiesenrain   (IDV 08/0706)
    "ATVA007": "SPO.08.2708.5527.7.1",    # Bludenz Herrengasse   (IDV 08/2708)
    "ATVA009": "SPO.08.2801.3213.7.1",    # Wald am Arlberg S16   (IDV 08/2801)
    "ATVA008": "SPO.08.0503.3670.7.1",    # Sulzberg Gmeind       (IDV 08/0503)
}

# (Container, erstes Jahr, letztes Jahr) — aufsteigend. Spaetere Container
# gewinnen bei Ueberlappung, weil sie die aktuelleren Daten tragen.
# Der airbase-Container reicht viel weiter zurueck als zunaechst angenommen:
# Lustenau ab 1988-01, Sulzberg ab 1989-05, Wald am Arlberg ab 2003, Bludenz ab
# 2004. Deshalb wird hier nicht mehr auf 2003 beschnitten — die Jahresangaben
# dienen nur noch dazu, unnoetige Downloads zu ueberspringen.
DATASETS = [
    ("airquality-p-airbase", 1988, 2012, "airbase"),
    ("airquality-p-e1a", 2013, 2024, "e1a"),
    ("airquality-p", 2025, 9999, "e2a"),
]

FIRST_YEAR = 1988      # frueheste Daten ueberhaupt (Lustenau)

CACHE = Path("cache/eea")
ARCHIVE_JSON = Path("archive.json")

# Ozonsaison. Der Tagesgang ausserhalb ist ein anderes Regime und wuerde das
# Trainingsfenster verwaessern.
SEASON_MONTHS = (4, 5, 6, 7, 8, 9)
PROFILE_YEARS = 5          # Tagesgang nur aus den letzten N Jahren
RECENT_DAYS = 21           # so viele Tage Stundenwerte wandern ins archive.json
EIGHT_H_MIN_VALID = 6      # von 8 Stunden muessen so viele gueltig sein


# ---------------------------------------------------------------------------
# Laden
# ---------------------------------------------------------------------------


def blob_url(container: str, point: str) -> str:
    return f"{BLOB}/{container}/AT/{point}.parquet"


def cache_path(container: str, point: str) -> Path:
    return CACHE / f"{container}__{point}.parquet"


def blob_last_modified(container: str, point: str,
                       timeout: int = 30) -> Optional[str]:
    """Wann hat die EEA diese Datei zuletzt geschrieben?

    Der E2a-Container wird nur etwa einmal taeglich neu geschrieben. Damit
    schwankt der Datenverzug zwischen rund 1 h direkt danach und rund 25 h
    davor. Der Wert wird mitprotokolliert, damit der Rhythmus ueber die Tage
    belegbar wird statt geschaetzt.
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
    """Eine Parquet-Datei holen und cachen. None, wenn es sie nicht gibt."""
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
    """(Zeit in Europe/Vienna, Wert) fuer alle gueltigen Stunden.

    Validity: 1..3 gueltig, -1 ungueltig, -99 nicht gemessen. Alles unter 1
    fliegt raus — ein ungueltiger Wert ist schlimmer als eine Luecke.
    """
    try:
        import pyarrow.parquet as pq
    except ImportError:
        sys.exit("Fehlende Abhaengigkeit: pip install -r requirements.txt")

    t = pq.read_table(path, columns=["Start", "Value", "Validity"]).to_pydict()
    tz = _tz()
    out: list[tuple[datetime, float]] = []
    for s, v, val in zip(t["Start"], t["Value"], t["Validity"]):
        if v is None or val is None or int(val) < 1:
            continue
        # Naiven Stempel als feste MEZ deuten, dann nach Lokalzeit drehen.
        ts = s.replace(tzinfo=EEA_TZ).astimezone(tz) if s.tzinfo is None \
            else s.astimezone(tz)
        out.append((ts, float(v)))
    return out


def load_station(sid: str, since: int = FIRST_YEAR, refresh: bool = False,
                 quiet: bool = False) -> list[tuple[datetime, float]]:
    """Alle Container einer Station laden und zu einer Reihe verschmelzen."""
    point = SAMPLING_POINT[sid]
    merged: dict[datetime, float] = {}
    for container, _y0, y1, _tag in DATASETS:
        if y1 < since:
            continue
        p = download(container, point, refresh=refresh)
        if p is None:
            if not quiet:
                print(f"  {sid} {container}: nicht vorhanden", file=sys.stderr)
            continue
        rows = read_parquet(p)
        kept = 0
        for ts, v in rows:
            if ts.year < since:
                continue
            merged[ts] = v      # spaeterer Container gewinnt
            kept += 1
        if not quiet:
            print(f"  {sid} {container:22} {kept:>7} Stunden", file=sys.stderr)
    return sorted(merged.items())


# ---------------------------------------------------------------------------
# Kennzahlen
# ---------------------------------------------------------------------------


def rolling_8h(series: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    """Gleitendes 8-h-Mittel, dem ENDE des Fensters zugeordnet (EU-Konvention).

    ``series`` ist mit dem Fenster-START jeder Stunde beschriftet (so liefert
    es read_parquet). Der Stundenwert mit Start S deckt S..S+1 ab, ein
    8-h-Fenster aus den Staenden S-7..S deckt also S-7..S+1 ab und endet bei
    S+1 — nicht bei S. Genau diese eine Stunde fehlte hier zuerst, was das
    Tagesmaximum reproduzierbar um rund 6 µg/m³ zu hoch machte: der Schnitt
    durfte eine Nachmittagsstunde zu viel mitnehmen.

    Verlangt eine echte Stundenkette: Luecken brechen das Fenster, statt es
    ueber sie hinweg zu mitteln.
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
    """Tag, dem ein Stundenwert zugeordnet wird (Stempel = Fensterstart)."""
    return start_ts.astimezone(AGG_TZ).date()


def day_of_window(end_ts: datetime) -> date:
    """Tag, dem ein 8-h-Fenster zugeordnet wird (Stempel = Fensterende).

    Nach EU-Konvention gehoert ein Fenster zu dem Tag, an dem es ENDET, wobei
    24:00 noch zum alten Tag zaehlt. Das erste Fenster eines Tages ist damit
    das, welches am Vorabend beginnt und in der Nacht endet — es schleppt den
    hohen Vorabend mit und ist an ruhigen Vormittagen oft das Tagesmaximum.
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
    """WHO-Langfristkennzahl: Mittel der 8-h-Tagesmaxima ueber die sechs
    zusammenhaengenden Monate mit dem hoechsten solchen Mittel."""
    by_month: dict[int, list[float]] = defaultdict(list)
    for d, v in dmax8.items():
        if d.year == year:
            by_month[d.month].append(v)
    best = None
    for start in range(1, 8):                      # Fenster Jan..Jul beginnend
        months = range(start, start + 6)
        vals = [v for m in months for v in by_month.get(m, [])]
        if len(vals) < 120:                        # zu duenn fuer die Kennzahl
            continue
        m = sum(vals) / len(vals)
        if best is None or m > best[0]:
            best = (m, start)
    return round(best[0], 1) if best else None


def yearly_stats(series: list[tuple[datetime, float]]) -> dict:
    s8 = rolling_8h(series)
    dmax8 = daily_max(s8, day_of_window)      # Stempel = Fensterende
    dmax1 = daily_max(series, day_of_hour)    # Stempel = Fensterstart

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
            # Das laufende Jahr ist noch nicht zu Ende: Tage>120 und
            # peak_season_mean sind Zwischenstaende, nicht Jahreswerte.
            "partial": y >= this_year,
        }
    return out


def hour_profile(series: list[tuple[datetime, float]],
                 last_years: int = PROFILE_YEARS) -> dict:
    """Tagesgang der Ozonsaison: Median und Quartile je Stunde."""
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
    """Die letzten Tage als Stundenreihe, fuer die Verlaufskurve im Dashboard.

    ACHTUNG Beschriftung: hier nach FENSTERENDE, nicht nach Start. Damit passt
    die Reihe stossfrei an history.jsonl, das die Zeitstempel der Landesseite
    uebernimmt — und die beschriftet nach Fensterende. Ohne diese Umrechnung
    liegen Archiv und Eigenlog um eine Stunde versetzt aneinander.
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
    """Referenzverteilung der 1-h-Tagesmaxima im selben Kalenderfenster der
    Vorjahre. Beantwortet 'ist das viel oder normal fuer Mitte August'.

    Bewusst OHNE den heutigen Wert: das Archiv hinkt 1 bis 25 Stunden nach,
    der laufende Tag ist darin unvollstaendig, und sein Maximum waere damit
    ein Vormittagswert, der gegen vollstaendige Tage verglichen wird. Das
    ergab reproduzierbar absurde Perzentile (Lustenau 67 statt 163 µg/m³).
    Den Tageswert setzt ozon_vorarlberg.py aus der Live-Quelle ein.
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
        # Sortierte Referenzwerte, damit das Perzentil des Live-Tageswerts
        # ohne erneuten Archivzugriff berechenbar bleibt.
        "reference_sorted": ref,
    }


# ---------------------------------------------------------------------------
# Aufbau
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

    # Bestes Fenster ueber alle Stationen: Median der Stationsmediane.
    combined = []
    for h in range(24):
        vals = [s["hour_profile"]["median"][h] for s in stations.values()
                if s["hour_profile"]["median"][h] is not None]
        combined.append(_median(vals) if vals else None)
    overall = best_window({"median": combined})

    live = DATASETS[-1][0]      # E2a: der Container, der nachwaechst
    lm = blob_last_modified(live, SAMPLING_POINT[STATION_ORDER[0]])
    newest = max((s["last"] for s in stations.values()), default=None)

    return {
        "schema": 1,
        "built_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "upstream": {
            "container": live,
            "last_modified": lm,
            "newest_value": newest,
            "note": ("Die EEA schreibt diesen Container etwa einmal taeglich "
                     "neu. Der Verzug der Messwerte schwankt daher zwischen "
                     "rund 1 h und rund 25 h — deshalb traegt das eigene Log "
                     "das 72-h-Fenster, nicht das Archiv."),
        },
        "timezone": str(tz),
        "source": {
            "blob": BLOB,
            "datasets": [{"container": c, "from": y0,
                          "to": (None if y1 == 9999 else y1), "tag": tag}
                         for c, y0, y1, tag in DATASETS],
            "note": ("EEA Air Quality e-Reporting. Zeitstempel der Quelle in "
                     "fester MEZ (UTC+1, keine Sommerzeit), hier nach "
                     "Europe/Vienna konvertiert."),
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
    print(f"{'Jahr':6}" + "".join(f"{s['short'][:9]:>11}" for s in rec["stations"]))
    for y in years:
        row = f"{y:6}"
        for s in rec["stations"]:
            d = s["yearly"].get(y)
            row += f"{(str(round(d['coverage']*100)) + '%') if d else '-':>11}"
        print(row)


def print_stats(rec: dict) -> None:
    for s in rec["stations"]:
        print(f"\n=== {s['short']} ({s['hours']} Stunden, "
              f"{s['first'][:10]} .. {s['last'][:10]}) ===")
        bw = s["best_window"]
        if bw:
            print(f"  Bestes Fenster: {bw['from_hour']:02d}-{bw['to_hour']:02d} Uhr "
                  f"(Median {bw['mean']} µg/m³, Saison Apr-Sep, "
                  f"{s['hour_profile']['n_days']} Tage)")
        dc = s.get("day_context")
        if dc and dc.get("today_max_1h") is not None:
            print(f"  Heute {dc['today_max_1h']} µg/m³ = {dc.get('percentile')}. "
                  f"Perzentil des Kalenderfensters "
                  f"(Median {dc['median']}, Max {dc['max']})")
        print(f"  {'Jahr':6}{'PeakSeason':>12}{'Tage>120':>10}"
              f"{'Std>180':>9}{'Max1h':>7}{'Abd.':>7}")
        for y in sorted(s["yearly"]):
            d = s["yearly"][y]
            if d["coverage"] < .5:
                continue
            print(f"  {y:6}{str(d['peak_season_mean'] or '-'):>12}"
                  f"{d['days_8h_over_120']:>10}{d['hours_1h_over_180']:>9}"
                  f"{d['max_1h']:>7}{round(d['coverage']*100):>6}%")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="EEA-Ozonarchiv fuer Vorarlberg holen und verdichten")
    ap.add_argument("--build", action="store_true",
                    help="laden, cachen und archive.json schreiben")
    ap.add_argument("--stats", action="store_true", help="Kennzahlen ausgeben")
    ap.add_argument("--coverage", action="store_true",
                    help="Datenabdeckung pro Station und Jahr")
    ap.add_argument("--since", type=int, default=FIRST_YEAR, metavar="JAHR",
                    help=f"nur ab diesem Jahr (Default {FIRST_YEAR} = alles)")
    ap.add_argument("--out", default=str(ARCHIVE_JSON), metavar="DATEI")
    ap.add_argument("--refresh", action="store_true",
                    help="Cache verwerfen und neu laden")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if not (args.build or args.stats or args.coverage):
        ap.error("nichts zu tun — --build, --stats oder --coverage angeben")

    rec = build(since=args.since, refresh=args.refresh, quiet=args.quiet)
    if not rec["stations"]:
        print("Keine Station geladen.", file=sys.stderr)
        return 2

    up = rec.get("upstream") or {}
    if not args.quiet and up.get("last_modified"):
        print(f"\nEEA-Container zuletzt geschrieben: {up['last_modified']}",
              file=sys.stderr)
        print(f"Neuester Messwert:                {up.get('newest_value')}",
              file=sys.stderr)

    if args.build:
        out = Path(args.out)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(out)
        if not args.quiet:
            total = sum(s["hours"] for s in rec["stations"])
            print(f"\n{out} geschrieben — {total} Stundenwerte, "
                  f"{len(rec['stations'])} Stationen.")
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
