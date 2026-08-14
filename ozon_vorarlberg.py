#!/usr/bin/env python3
"""
ozon_vorarlberg.py — Ozon-Scraper und Historien-Logger fuer Vorarlberg.

Datenquelle: https://www.vorarlberg-luft.at/tab1O3.htm

Die Seite ist ein serverseitig generierter, statischer HTML-Export (Generator:
InterConnect Software, Layout seit 2004 unveraendert) und wird stuendlich neu
geschrieben. Sie enthaelt pro Messstation genau fuenf Zahlen: den aktuellen
1h-Mittelwert, Tagesmaximum 1h und 8h (gleitend) sowie die beiden
Vortagsmaxima. Einen Zahlen-Verlauf gibt es NICHT — die verlinkten
"Grafischer Verlauf"-Seiten enthalten nur ein fertig gerendertes JPEG.

Deshalb baut dieses Skript die Historie selbst auf: jeder Abruf wird nach
history.jsonl angehaengt (dedupliziert ueber den Zeitstempel der Quellseite),
und daraus entsteht die Zeitreihe, die das Dashboard zeichnet.

Bewertung — zwei getrennte Achsen, absichtlich nicht vermischt:

  1. Trainings-Skala (auf dem AKTUELLEN 1h-Wert). Was du gerade atmest.
     Pragmatische Skala, kein Rechtswert: die 100/120-Marken sind von den
     8h-Richtwerten geliehen, 180 ist die echte oesterreichische
     Informationsschwelle (1h).
         < 100  good      frei      volle Einheit moeglich
         < 120  warning   ok        lange harte Einheiten kuerzen
         < 180  serious   locker    nur Grundlage, nichts Intensives
         >=180  critical  drinnen   Informationsschwelle, Outdoor absagen

  2. Tagesbewertung (auf dem Tagesmax. 8h-Mittel). Der gesundheitliche
     Kontext des Tages, mit den echten Referenzwerten:
         WHO-Kurzzeit-Leitwert 2021   100 ug/m3 (max. 8h-Tagesmittel)
         EU-Zielwert                  120 ug/m3 (8h, gleitend)
         AT-Informationsschwelle      180 ug/m3 (1h)

Das alte Dashboard hat den 1h-Wert gross angezeigt, die Ampel aber nach dem
8h-Tagesmaximum gefaerbt. Das ist irrefuehrend: um die Mittagszeit schleppt
der gleitende 8h-Wert noch die kuehlen Morgenstunden mit und liegt deutlich
unter dem, was gerade tatsaechlich in der Luft ist.

Aufrufe:
    python3 ozon_vorarlberg.py                      # JSON auf stdout
    python3 ozon_vorarlberg.py --compact            # eine Zeile pro Station
    python3 ozon_vorarlberg.py --log --out data.json    # der Cron-Aufruf
    python3 ozon_vorarlberg.py --station lustenau
    python3 ozon_vorarlberg.py --html fixtures/tab1O3_live.htm   # offline
    python3 ozon_vorarlberg.py --watch 1800 --log --out data.json
    python3 ozon_vorarlberg.py --demo --out data.json   # Demo-Historie
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
# Referenzwerte (ug/m3)
# ---------------------------------------------------------------------------

WHO_SHORT_8H = 100   # WHO global air quality guidelines 2021, max. 8h-Tagesmittel
EU_TARGET_8H = 120   # EU-Zielwert, 8h gleitend
AT_INFO_1H = 180     # Oesterreichische Informationsschwelle, 1h
WHO_PEAK_SEASON = 60  # WHO Langfrist (Mittel der 8h-Tagesmaxima, 6 Monate)

# ---------------------------------------------------------------------------
# Stationen
#
# Die Reihenfolge ist die Zuordnung der Farb-Slots im Dashboard und darf nicht
# umsortiert werden: "Farbe folgt der Entitaet, nicht ihrem Rang". Sortiert
# nach Hoehenlage aufsteigend — das ist gleichzeitig die inhaltliche Achse
# (Talsohle -> Hoehenlage), an der sich der Ozon-Tagesgang aufspannt.
#
# Hoehenangaben sind Naeherungswerte und stammen NICHT aus der Quelle.
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

# Trainings-Skala auf dem akuten 1h-Wert: (obere Grenze, Status, Kurzwort, Text)
TRAINING_SCALE = [
    (WHO_SHORT_8H, "good", "frei",
     "Volle Einheit möglich, auch intensiv."),
    (EU_TARGET_8H, "warning", "ok",
     "Kurze Einheiten unkritisch, lange harte Blocks kürzen."),
    (AT_INFO_1H, "serious", "locker",
     "Nur Grundlage und locker. Intervalle auf morgen früh verschieben."),
    (None, "critical", "drinnen",
     "Informationsschwelle erreicht. Outdoor-Sport absagen."),
]

STATUS_ORDER = {"good": 0, "warning": 1, "serious": 2, "critical": 3, "unknown": -1}

DEFAULT_HISTORY = "history.jsonl"
DEFAULT_ARCHIVE = "archive.json"
HISTORY_HOURS = 72          # wie viel Verlauf ins data.json wandert
PROFILE_MIN_DAYS = 2        # ab wann ein Tagesgang-Profil sinnvoll ist

TZ_NAME = "Europe/Vienna"


def _tz():
    """Zeitzone der Quelle. Die Seite stempelt oesterreichische Lokalzeit."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(TZ_NAME)
    except Exception:
        # Kein tzdata verfuegbar: lokale Systemzeitzone als Naeherung.
        return datetime.now().astimezone().tzinfo


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class LayoutError(RuntimeError):
    """Die Quellseite sieht nicht mehr aus wie erwartet."""


@dataclass
class Reading:
    id: str
    station: str
    short: str
    akt_1h: Optional[int]
    max_1h: Optional[int]
    max_8h: Optional[int]
    prev_max_1h: Optional[int]
    prev_max_8h: Optional[int]


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
    """Tabellenzelle -> int. '-', leer, Einheit oder nicht-numerisch -> None."""
    cell = cell.replace("\xa0", " ").strip()
    if not cell or cell.strip("-. ") == "":
        return None
    if _UNIT.search(cell):       # "ug/m3" enthaelt eine 3 — nicht als Wert lesen
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
    """Ozon-Tabelle aus dem Seiten-HTML lesen.

    Stationszeilen werden ueber den Link auf ihre Detailseite erkannt
    (``statATVA007.htm`` -> ``ATVA007``). Die Stations-ID ist stabiler als der
    Anzeigename, der Tippfehler und Umbenennungen ueberleben muss.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        sys.exit("Fehlende Abhaengigkeit: pip install -r requirements.txt")

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
        # Die Quelle verschachtelt Layout-Tabellen: der aeussere Wrapper-<tr>
        # umschliesst die komplette Werte-Tabelle und enthaelt damit auch deren
        # ersten Stations-Link. Ohne diesen Filter wird er als Datenzeile
        # gelesen, belegt die Stations-ID mit Muell und die echte Zeile fliegt
        # danach als Duplikat raus.
        if row.find("table") is not None:
            continue

        cells = _cells(row)
        if not cells:
            continue

        # Schwellwert-Zeile mitnehmen: die Seite dokumentiert ihre eigenen
        # Grenzwerte, das ist ein guter Plausibilitaetscheck.
        if cells[0].lower().startswith("schwellwert"):
            page.thresholds_on_page = [_to_int(c) for c in cells[1:6]]
            continue

        sid = None
        link = row.find("a", href=_STAT_HREF)
        if link:
            m = _STAT_HREF.search(link.get("href", ""))
            sid = m.group(1).upper() if m else None
        if sid is None:
            # Fallback: Name-Matching, falls die Detaillinks je verschwinden.
            label = cells[0].lower()
            sid = next(
                (k for k, v in STATIONS.items() if v["name"].lower() in label
                 or v["short"].lower() in label),
                None,
            )
        if sid is None or sid in seen:
            continue
        if len(cells) < 6:          # Name + 5 Werte; alles darunter ist Deko
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
    """Laut scheitern, wenn sich die Quelle strukturell veraendert hat.

    Lieber ein harter Fehler als stillschweigend falsch zugeordnete Spalten.
    """
    problems = []
    if not page.readings:
        problems.append("keine Stationszeile erkannt")
    missing = set(STATION_ORDER) - {r.id for r in page.readings}
    if missing:
        problems.append(f"Stationen fehlen: {sorted(missing)}")
    if page.source_time is None:
        problems.append("kein Zeitstempel in der Ueberschrift gefunden")
    got = page.thresholds_on_page
    want = [AT_INFO_1H, AT_INFO_1H, EU_TARGET_8H, AT_INFO_1H, EU_TARGET_8H]
    if got and got != want:
        problems.append(f"Schwellwert-Zeile {got}, erwartet {want} "
                        "(Spaltenreihenfolge kann sich geaendert haben)")
    if problems:
        raise LayoutError("Quellseiten-Layout unerwartet: " + "; ".join(problems))


def fetch(url: str = URL, timeout: int = 20) -> str:
    """Seite holen und korrekt dekodieren.

    Die Seite deklariert iso-8859-1 und haelt sich daran. Auf
    ``apparent_encoding`` zu raten ist unnoetig und ging bei kurzen Seiten
    schon schief — Umlaute in "Luftqualitaet" landeten als Mojibake im JSON.
    """
    try:
        import requests
    except ImportError:
        sys.exit("Fehlende Abhaengigkeit: pip install -r requirements.txt")

    headers = {
        "User-Agent": "ozon-vorarlberg/2.0 (privat, stuendlich)",
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
# Bewertung
# ---------------------------------------------------------------------------


def training_level(akt_1h: Optional[int]) -> dict:
    """Trainings-Ampel auf dem akuten 1h-Wert."""
    if akt_1h is None:
        return {"status": "unknown", "word": "keine Daten",
                "advice": "Station liefert gerade keinen Wert."}
    for limit, status, word, advice in TRAINING_SCALE:
        if limit is None or akt_1h < limit:
            return {"status": status, "word": word, "advice": advice}
    return {"status": "critical", "word": "drinnen", "advice": ""}


def day_assessment(max_8h: Optional[int]) -> dict:
    """Gesundheitliche Tagesbewertung auf dem 8h-Tagesmaximum."""
    if max_8h is None:
        return {"status": "unknown", "label": "keine Daten",
                "vs_who": None, "vs_eu": None}
    if max_8h >= EU_TARGET_8H:
        label, status = "über EU-Zielwert", "serious"
    elif max_8h >= WHO_SHORT_8H:
        label, status = "über WHO-Leitwert", "warning"
    else:
        label, status = "unter WHO-Leitwert", "good"
    return {
        "status": status,
        "label": label,
        "vs_who": max_8h - WHO_SHORT_8H,
        "vs_eu": max_8h - EU_TARGET_8H,
    }


def trend_for(series: list[Optional[int]], flat_band: int = 5) -> dict:
    """Trend aus den letzten geloggten Werten. Braucht mindestens zwei."""
    vals = [v for v in series if v is not None]
    if len(vals) < 2:
        return {"dir": "unknown", "delta": None, "arrow": "–"}
    delta = vals[-1] - vals[-2]
    if abs(delta) < flat_band:
        return {"dir": "flat", "delta": delta, "arrow": "▬"}
    if delta > 0:
        return {"dir": "up", "delta": delta, "arrow": "▲"}
    return {"dir": "down", "delta": delta, "arrow": "▼"}


# ---------------------------------------------------------------------------
# Historie
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
            continue          # eine kaputte Zeile darf den Rest nicht killen
    out.sort(key=lambda e: e.get("source_time") or "")
    return out


def append_history(path: str | os.PathLike, page: Page) -> bool:
    """Snapshot anhaengen. False, wenn dieser Quell-Zeitstempel schon drin ist.

    Dedupliziert wird ueber den Zeitstempel der QUELLE, nicht ueber die
    Abrufzeit: die Seite wird stuendlich neu geschrieben, ein Cron alle 15
    Minuten wuerde sonst denselben Messwert viermal in die Reihe legen.
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
    """Alte Zeilen aus history.jsonl entfernen. Gibt die Zahl der Entfernten.

    Das Log ist nur eine Bruecke bis das EEA-Archiv denselben Zeitraum
    nachliefert. Weil die EEA ihren Container nur etwa einmal taeglich neu
    schreibt, schwankt dieser Verzug zwischen rund 1 und rund 25 Stunden — das
    72-h-Fenster des Dashboards traegt deshalb das Log allein. Alles was aelter
    als vier Tage ist, wird nie wieder gebraucht.
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
    """Verlaufsreihe fuer das Dashboard, aus zwei Quellen zusammengefuegt.

    Das EEA-Archiv liefert geprueft gemessene Stundenwerte, wird aber nur etwa
    einmal taeglich neu geschrieben — sein Verzug schwankt zwischen rund 1 und
    rund 25 Stunden. Es taugt daher zur Erstbefuellung, nicht als Fueller fuer
    die letzten Stunden. Dauerhaft traegt das eigene Log das 72-h-Fenster.
    Solange es noch nicht so weit zurueckreicht, springt das Archiv ein.

    Beide Reihen sind nach FENSTERENDE beschriftet: history.jsonl uebernimmt
    die Zeitstempel der Landesseite, und eea_archive.recent_series rechnet
    dafuer eigens von Start auf Ende um. Ohne das lagen sie um eine Stunde
    versetzt aneinander.

    Bei gleichem Zeitstempel gewinnt das Archiv — das ist der eigentliche
    Messwert, waehrend das Log nur die gerundete Anzeige der Seite mitschreibt.
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
                per_station[sid][ts] = float(v)      # Archiv gewinnt
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

    # max_8h bleibt aus dem Eigenlog: es ist ein Tagesmaximum und im Verlauf
    # ohnehin nicht gezeichnet.
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
    """Tagesgang pro Station: Median des 1h-Werts je Stunde des Tages.

    Erst ab ``PROFILE_MIN_DAYS`` verschiedenen Tagen sinnvoll — vorher ist das
    kein Profil, sondern nur der eine geloggte Tag mit Extra-Schritten.
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
    """Sauberstes zusammenhaengendes Zeitfenster ueber alle Stationen."""
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
# EEA-Archiv (optional)
# ---------------------------------------------------------------------------


def load_archive(path: str | os.PathLike) -> Optional[dict]:
    """archive.json einlesen, falls vorhanden. Fehlt es, laeuft alles weiter —
    dann eben nur mit der selbst geloggten Historie."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return rec if rec.get("stations") else None


def percentile_of(reference_sorted: list[int], value: Optional[int]) -> Optional[int]:
    """Wo liegt der heutige Tageswert in der Referenzverteilung der Vorjahre."""
    if value is None or not reference_sorted:
        return None
    return round(100 * bisect_left(reference_sorted, value) / len(reference_sorted))


def archive_block(archive: Optional[dict], readings: list[Reading]) -> Optional[dict]:
    """Archivkennzahlen fuer data.json aufbereiten.

    Der Tageswert kommt bewusst aus der LIVE-Quelle, nicht aus dem Archiv: das
    Archiv hinkt 1 bis 25 Stunden nach, sein Maximum fuer den laufenden Tag
    ist also unvollstaendig.
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
# Ausgabe
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
            "slot": idx + 1,                    # fixer Farb-Slot im Dashboard
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
          f"(Quelle: vorarlberg-luft.at)")
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
        print(f"\n-> Sauberste Station: {su['cleanest']} "
              f"({su['cleanest_value']} ug/m3)")
    arc = rec.get("archive")
    if arc:
        bw = arc.get("overall_best_window") or {}
        print(f"-> Archiv: {arc['total_hours']} Stundenwerte, "
              f"{arc['years'][0]}-{arc['years'][-1]}")
        if bw:
            span = f"{arc['years'][0]}-{arc['years'][-1]}" if arc.get("years") else "?"
            print(f"-> Bestes Trainingsfenster ({span}, Saisondaten): "
                  f"{bw['from_hour']:02d}-{bw['to_hour']:02d} Uhr, "
                  f"Median {bw['mean']} ug/m3")
        for st in arc["stations"]:
            c = st.get("context") or {}
            if c.get("percentile") is not None:
                print(f"   {st['short']:16} heute {c['today_max_1h']:>3} "
                      f"= {c['percentile']:>3}. Perzentil "
                      f"(Median {c['median']}, Max {c['max']} seit "
                      f"{c['years'][0]})")
    h = rec["history"]
    print(f"-> Historie: {h['points']} "
          f"Punkt{'' if h['points'] == 1 else 'e'} / {h['span_hours']} h")


# ---------------------------------------------------------------------------
# Demo-Daten
# ---------------------------------------------------------------------------


def demo_history(hours: int = 48) -> tuple[Page, list[dict]]:
    """Plausible Historie synthetisieren, damit das Dashboard sofort was zeigt.

    Deterministisch (kein Zufall), damit die Demo reproduzierbar bleibt.
    Physik grob nachgebaut: Talstationen mit tiefem Nachtminimum und
    Nachmittagspeak, Sulzberg als Hoehenstation flach und durchgehend hoch.
    """
    tz = _tz()
    now = datetime.now(tz).replace(minute=0, second=0, microsecond=0)
    shape = {                     # (Mittelwert, Amplitude, Peak-Stunde)
        "ATVA002": (105, 60, 16),
        "ATVA007": (100, 55, 16),
        "ATVA009": (95, 45, 15),
        "ATVA008": (140, 18, 15),
    }

    entries = []
    for back in range(hours, -1, -1):
        ts = now - timedelta(hours=back)
        day_damp = 1.0 - 0.12 * (back // 24)      # Vortage leicht schwaecher
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
        description="Ozon Vorarlberg — Scraper, Historien-Logger, Bewertung",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Cron-Zeile:  ozon_vorarlberg.py --log --out data.json --quiet",
    )
    ap.add_argument("--station", help="nur Stationen, die auf diesen Text passen")
    ap.add_argument("--html", help="lokale HTML-Datei parsen statt abrufen")
    ap.add_argument("--url", default=URL, help="abweichende Quell-URL")
    ap.add_argument("--out", metavar="DATEI",
                    help="JSON in diese Datei schreiben (fuer das Dashboard)")
    ap.add_argument("--log", action="store_true",
                    help="Snapshot an die Historie anhaengen")
    ap.add_argument("--history", default=DEFAULT_HISTORY, metavar="DATEI",
                    help=f"Pfad der Historien-Datei (Default: {DEFAULT_HISTORY})")
    ap.add_argument("--prune-history", type=int, default=None, metavar="TAGE",
                    help="Log-Zeilen aelter als N Tage entfernen (empfohlen: 3)")
    ap.add_argument("--archive", default=DEFAULT_ARCHIVE, metavar="DATEI",
                    help="EEA-Archiv aus eea_archive.py --build. Fehlt es, "
                         "laeuft alles ohne Langzeitkennzahlen weiter.")
    ap.add_argument("--no-archive", action="store_true",
                    help="Archiv ignorieren, auch wenn es da ist")
    ap.add_argument("--watch", type=int, metavar="SEKUNDEN",
                    help="Endlosschleife, alle N Sekunden neu abrufen")
    ap.add_argument("--compact", action="store_true",
                    help="Klartext statt JSON, eine Zeile pro Station")
    ap.add_argument("--strict", action="store_true",
                    help="mit Fehler abbrechen, wenn das Layout unerwartet ist")
    ap.add_argument("--demo", action="store_true",
                    help="synthetische Demo-Daten statt Abruf")
    ap.add_argument("--quiet", action="store_true",
                    help="keine Ausgabe auf stdout (nur --out schreiben)")
    args = ap.parse_args(argv)

    if args.demo and (args.html or args.log):
        ap.error("--demo laesst sich nicht mit --html oder --log kombinieren")

    def run_once() -> int:
        if args.demo:
            page, entries = demo_history()
            demo = True
        else:
            demo = False
            if args.html:
                html = Path(args.html).read_bytes().decode(
                    "iso-8859-1", errors="replace")
                # Fixtures duerfen UTF-8 sein; nur umschalten, wenn es passt.
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
                print(f"FEHLER: {exc}", file=sys.stderr)
                return 2

            if not page.readings:
                print("Keine Station erkannt — Layout der Quelle geaendert?",
                      file=sys.stderr)
                return 2

            if args.log:
                added = append_history(args.history, page)
                if not args.quiet and not args.compact:
                    print(
                        f"# Historie: {'neuer Eintrag' if added else 'unveraendert'}"
                        f" ({args.history})", file=sys.stderr)

            if args.prune_history:
                gone = prune_history(args.history, args.prune_history)
                if gone and not args.quiet:
                    print(f"# Historie: {gone} alte Zeilen entfernt",
                          file=sys.stderr)
            entries = load_history(args.history)

        if args.station:
            needle = args.station.lower()
            page.readings = [r for r in page.readings
                             if needle in r.station.lower()
                             or needle in r.short.lower()
                             or needle == r.id.lower()]
            if not page.readings:
                print(f"Keine Station passt auf {args.station!r}.",
                      file=sys.stderr)
                return 1

        archive = None if args.no_archive else load_archive(args.archive)
        rec = build_record(page, entries, demo=demo, archive=archive)

        if args.out:
            out = Path(args.out)
            tmp = out.with_suffix(out.suffix + ".tmp")
            tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(out)      # atomar: das Dashboard liest nie halbe Datei

        if not args.quiet:
            if args.compact:
                print_compact(rec)
            elif not args.out:
                print(json.dumps(rec, ensure_ascii=False, indent=2))
            else:
                n = rec["history"]["points"]
                print(f"{args.out} geschrieben "
                      f"({n} Historienpunkt{'' if n == 1 else 'e'}).")
        return 0

    if args.watch:
        while True:
            try:
                run_once()
            except KeyboardInterrupt:
                return 130
            except Exception as exc:                  # Schleife muss weiterlaufen
                print(f"Abruf fehlgeschlagen: {exc}", file=sys.stderr)
            time.sleep(args.watch)
    return run_once()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
