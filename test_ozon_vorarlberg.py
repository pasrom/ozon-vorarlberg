#!/usr/bin/env python3
"""Tests fuer ozon_vorarlberg.py — laufen ohne pytest: python3 -m unittest -v

Die wichtigste Fixture ist fixtures/tab1O3_live.htm: ein echter, unveraenderter
Abzug der Quellseite inklusive ihrer verschachtelten Layout-Tabellen. Genau
diese Verschachtelung hat den urspruenglichen Parser gebrochen, und die alte
handgeschriebene Minimal-Fixture konnte das nicht zeigen.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import ozon_vorarlberg as oz

HERE = Path(__file__).parent
LIVE = HERE / "fixtures" / "tab1O3_live.htm"
MINIMAL = HERE / "fixtures" / "tab1O3_minimal.htm"


def read(p: Path) -> str:
    raw = p.read_bytes()
    try:
        t = raw.decode("utf-8")
        if "�" not in t:
            return t
    except UnicodeDecodeError:
        pass
    return raw.decode("iso-8859-1", errors="replace")


class TestToInt(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(oz._to_int("146"), 146)
        self.assertEqual(oz._to_int("  111  "), 111)
        self.assertEqual(oz._to_int("\xa0 98 \xa0"), 98)

    def test_blank_and_dash(self):
        for cell in ("", " ", "-", "--", "\xa0", "  -  ", "."):
            self.assertIsNone(oz._to_int(cell), cell)

    def test_unit_cell_is_not_a_value(self):
        # "ug/m3" und "µg/m 3" enthalten eine 3 - die darf nie als Messwert
        # durchgehen.
        for cell in ("µg/m3", "µg/m 3", "ug/m3", "µg / m 3"):
            self.assertIsNone(oz._to_int(cell), cell)


class TestParseLive(unittest.TestCase):
    """Regression fuer die verschachtelten Layout-Tabellen der echten Seite."""

    @classmethod
    def setUpClass(cls):
        cls.page = oz.parse_html(read(LIVE), strict=True)

    def test_all_four_stations(self):
        self.assertEqual([r.id for r in self.page.readings],
                         ["ATVA002", "ATVA007", "ATVA009", "ATVA008"])

    def test_values_exact(self):
        got = {r.id: (r.akt_1h, r.max_1h, r.max_8h, r.prev_max_1h, r.prev_max_8h)
               for r in self.page.readings}
        self.assertEqual(got, {
            "ATVA007": (146, 151, 111, 153, 143),   # Bludenz Herrengasse
            "ATVA002": (161, 163, 116, 163, 153),   # Lustenau Wiesenrain
            "ATVA008": (151, 152, 144, 154, 150),   # Sulzberg Gmeind
            "ATVA009": (130, 135, 104, 139, 133),   # Wald am Arlberg
        })

    def test_bludenz_is_not_eaten_by_the_wrapper_row(self):
        # Der aeussere <tr> umschliesst die ganze Werte-Tabelle und enthaelt
        # deren ersten Stations-Link (statATVA007). Ohne den Nested-Table-Filter
        # belegte er ATVA007 mit Muell und die echte Zeile fiel als Duplikat weg.
        bludenz = next(r for r in self.page.readings if r.id == "ATVA007")
        self.assertEqual(bludenz.akt_1h, 146)
        self.assertEqual(bludenz.station, "Bludenz Herrengasse")

    def test_source_time(self):
        self.assertEqual(self.page.source_time_raw, "14.08.2026 16:00")
        self.assertIsNotNone(self.page.source_time)
        self.assertEqual((self.page.source_time.year, self.page.source_time.month,
                          self.page.source_time.day, self.page.source_time.hour),
                         (2026, 8, 14, 16))

    def test_thresholds_row_matches_our_constants(self):
        self.assertEqual(self.page.thresholds_on_page, [180, 180, 120, 180, 120])

    def test_order_is_by_altitude(self):
        alts = [oz.STATIONS[r.id]["altitude_m_approx"] for r in self.page.readings]
        self.assertEqual(alts, sorted(alts))


class TestParseMinimal(unittest.TestCase):
    """Alte Fixture ohne Detail-Links: der Name-Fallback muss greifen."""

    @classmethod
    def setUpClass(cls):
        cls.page = oz.parse_html(read(MINIMAL))

    def test_four_stations_via_name_fallback(self):
        self.assertEqual(len(self.page.readings), 4)

    def test_missing_value_stays_none(self):
        lustenau = next(r for r in self.page.readings if r.id == "ATVA002")
        self.assertIsNone(lustenau.akt_1h)     # Zelle ist "-"
        self.assertEqual(lustenau.max_1h, 135)
        self.assertEqual(lustenau.max_8h, 120)

    def test_schwellwerte_row_is_not_a_station(self):
        self.assertNotIn("Schwellwerte", [r.station for r in self.page.readings])


class TestLayoutGuard(unittest.TestCase):
    def test_strict_raises_when_stations_vanish(self):
        with self.assertRaises(oz.LayoutError):
            oz.parse_html("<html><body><table><tr><td>nix</td></tr></table></body>"
                          "</html>", strict=True)

    def test_strict_raises_on_reordered_threshold_columns(self):
        html = read(LIVE).replace(
            "<td class=\"info\" align=\"center\">\n\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t180",
            "<td class=\"info\" align=\"center\">\n\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t120", 1)
        if html == read(LIVE):
            self.skipTest("Whitespace der Fixture passt nicht auf das Muster")
        with self.assertRaises(oz.LayoutError):
            oz.parse_html(html, strict=True)

    def test_non_strict_is_tolerant(self):
        page = oz.parse_html("<html><body><table><tr><td>nix</td></tr></table>"
                             "</body></html>")
        self.assertEqual(page.readings, [])


class TestTrainingScale(unittest.TestCase):
    def test_boundaries(self):
        cases = [(0, "good"), (99, "good"), (100, "warning"), (119, "warning"),
                 (120, "serious"), (179, "serious"), (180, "critical"),
                 (300, "critical")]
        for v, want in cases:
            self.assertEqual(oz.training_level(v)["status"], want, f"{v} µg/m³")

    def test_none_is_unknown(self):
        self.assertEqual(oz.training_level(None)["status"], "unknown")

    def test_every_band_has_advice(self):
        for v in (50, 110, 150, 200):
            self.assertTrue(oz.training_level(v)["advice"].strip())


class TestDayAssessment(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(oz.day_assessment(80)["status"], "good")
        self.assertEqual(oz.day_assessment(100)["status"], "warning")
        self.assertEqual(oz.day_assessment(119)["status"], "warning")
        self.assertEqual(oz.day_assessment(120)["status"], "serious")

    def test_deltas(self):
        d = oz.day_assessment(148)
        self.assertEqual(d["vs_who"], 48)
        self.assertEqual(d["vs_eu"], 28)

    def test_none(self):
        self.assertEqual(oz.day_assessment(None)["status"], "unknown")

    def test_is_independent_of_training_scale(self):
        # Der ganze Sinn der Trennung: akut hoch, Tagesmittel noch niedrig.
        self.assertEqual(oz.training_level(146)["status"], "serious")
        self.assertEqual(oz.day_assessment(111)["status"], "warning")


class TestTrend(unittest.TestCase):
    def test_needs_two_points(self):
        self.assertEqual(oz.trend_for([])["dir"], "unknown")
        self.assertEqual(oz.trend_for([120])["dir"], "unknown")
        self.assertEqual(oz.trend_for([None, 120])["dir"], "unknown")

    def test_directions(self):
        self.assertEqual(oz.trend_for([100, 130])["dir"], "up")
        self.assertEqual(oz.trend_for([130, 100])["dir"], "down")
        self.assertEqual(oz.trend_for([120, 122])["dir"], "flat")

    def test_delta_and_gaps(self):
        t = oz.trend_for([100, None, 140])
        self.assertEqual(t["delta"], 40)       # None-Luecken werden uebersprungen
        self.assertEqual(t["dir"], "up")


class TestHistory(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "history.jsonl"
        self.page = oz.parse_html(read(LIVE))

    def tearDown(self):
        self.dir.cleanup()

    def test_append_then_dedupe(self):
        self.assertTrue(oz.append_history(self.path, self.page))
        self.assertFalse(oz.append_history(self.path, self.page),
                         "gleicher Quell-Zeitstempel darf nicht doppelt landen")
        self.assertEqual(len(self.path.read_text().strip().splitlines()), 1)

    def test_dedupe_is_on_source_time_not_fetch_time(self):
        # Ein Cron alle 15 Minuten ruft dieselbe stuendliche Seite viermal ab.
        for _ in range(4):
            oz.append_history(self.path, self.page)
        self.assertEqual(len(oz.load_history(self.path)), 1)

    def test_new_source_time_appends(self):
        oz.append_history(self.path, self.page)
        later = oz.Page(
            source_time=self.page.source_time + timedelta(hours=1),
            source_time_raw="14.08.2026 17:00",
            readings=self.page.readings,
        )
        self.assertTrue(oz.append_history(self.path, later))
        self.assertEqual(len(oz.load_history(self.path)), 2)

    def test_page_without_timestamp_is_not_logged(self):
        p = oz.Page(source_time=None, source_time_raw=None,
                    readings=self.page.readings)
        self.assertFalse(oz.append_history(self.path, p))
        self.assertFalse(self.path.exists())

    def test_broken_line_does_not_kill_the_rest(self):
        oz.append_history(self.path, self.page)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write("{ das ist kein json\n")
        self.assertEqual(len(oz.load_history(self.path)), 1)

    def test_missing_file_is_empty(self):
        self.assertEqual(oz.load_history(Path(self.dir.name) / "nope.jsonl"), [])


def synth(hours, start_hour=0, base=100, amp=50):
    """Deterministische Historie: base +/- amp mit Peak um 16 Uhr."""
    import math
    tz = oz._tz()
    t0 = datetime(2026, 8, 10, start_hour, 0, tzinfo=tz)
    out = []
    for i in range(hours):
        ts = t0 + timedelta(hours=i)
        v = base + amp * math.cos((ts.hour - 16) / 24 * 2 * math.pi)
        out.append({
            "source_time": ts.isoformat(timespec="minutes"),
            "fetched_utc": ts.isoformat(timespec="seconds"),
            "stations": {sid: {"akt_1h": round(v), "max_1h": round(v + 5),
                               "max_8h": round(v - 10)}
                         for sid in oz.STATION_ORDER},
        })
    return out


class TestHistorySeries(unittest.TestCase):
    def test_empty(self):
        s = oz.history_series([])
        self.assertEqual(s["points"], 0)
        self.assertEqual(s["t"], [])

    def test_shape_is_parallel_arrays(self):
        s = oz.history_series(synth(10))
        self.assertEqual(s["points"], 10)
        self.assertEqual(set(s["akt_1h"]), set(oz.STATION_ORDER))
        for sid in oz.STATION_ORDER:
            self.assertEqual(len(s["akt_1h"][sid]), 10,
                             "jede Serie muss so lang sein wie die Zeitachse")

    def test_window_is_clipped(self):
        s = oz.history_series(synth(200), hours=24)
        self.assertLessEqual(s["span_hours"], 24)
        self.assertLess(s["points"], 200)

    def test_missing_station_becomes_none_not_a_hole(self):
        entries = synth(3)
        del entries[1]["stations"]["ATVA008"]
        s = oz.history_series(entries)
        self.assertEqual(len(s["akt_1h"]["ATVA008"]), 3)
        self.assertIsNone(s["akt_1h"]["ATVA008"][1])


class TestHourProfile(unittest.TestCase):
    def test_one_day_is_not_sufficient(self):
        p = oz.hour_profile(synth(20))
        self.assertEqual(p["n_days"], 1)
        self.assertFalse(p["sufficient"])
        self.assertIsNone(p["best_window"])

    def test_three_days_gives_a_window(self):
        p = oz.hour_profile(synth(72))
        self.assertEqual(p["n_days"], 3)
        self.assertTrue(p["sufficient"])
        self.assertIsNotNone(p["best_window"])

    def test_profile_has_24_slots(self):
        p = oz.hour_profile(synth(72))
        for sid in oz.STATION_ORDER:
            self.assertEqual(len(p["median_akt_1h"][sid]), 24)

    def test_best_window_lands_in_the_night_minimum(self):
        # Peak liegt bei 16 Uhr, das Minimum also gegen 04 Uhr.
        p = oz.hour_profile(synth(72))
        bw = p["best_window"]
        self.assertEqual(bw["to_hour"] - bw["from_hour"], 3)
        self.assertLessEqual(bw["from_hour"], 5)

    def test_best_window_skips_incomplete_stretches(self):
        prof: dict[str, list] = {sid: [None] * 24 for sid in oz.STATION_ORDER}
        for h in range(6, 12):
            for sid in oz.STATION_ORDER:
                prof[sid][h] = 100 - h
        bw = oz.best_window(prof)
        self.assertIsNotNone(bw)
        self.assertGreaterEqual(bw["from_hour"], 6)
        self.assertLessEqual(bw["to_hour"], 12)

    def test_best_window_none_when_nothing_complete(self):
        prof: dict[str, list] = {sid: [None] * 24 for sid in oz.STATION_ORDER}
        self.assertIsNone(oz.best_window(prof))


class TestMedian(unittest.TestCase):
    def test_odd_even_and_none(self):
        self.assertEqual(oz._median([3, 1, 2]), 2.0)
        self.assertEqual(oz._median([1, 2, 3, 4]), 2.5)
        self.assertEqual(oz._median([None, 5, None]), 5.0)
        self.assertIsNone(oz._median([None, None]))
        self.assertIsNone(oz._median([]))


class TestBuildRecord(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        page = oz.parse_html(read(LIVE))
        cls.rec = oz.build_record(page, synth(72))

    def test_is_json_serialisable(self):
        json.dumps(self.rec, ensure_ascii=False)

    def test_slots_are_stable_and_unique(self):
        slots = [s["slot"] for s in self.rec["stations"]]
        self.assertEqual(slots, [1, 2, 3, 4],
                         "Farb-Slots muessen der Stationsreihenfolge folgen")

    def test_every_station_has_both_assessments(self):
        for s in self.rec["stations"]:
            self.assertIn("status", s["training"])
            self.assertIn("status", s["day"])
            self.assertIn("chart_label", s)

    def test_summary(self):
        su = self.rec["summary"]
        self.assertEqual(su["cleanest"], "Wald a. Arlberg")   # 130
        self.assertEqual(su["worst"], "Lustenau")             # 161
        self.assertFalse(su["any_info_threshold"])
        self.assertEqual(su["eu_target_exceeded_at"], ["Sulzberg"])  # 8h 144

    def test_overall_is_the_worst_status(self):
        self.assertEqual(self.rec["summary"]["overall_training_status"], "serious")

    def test_thresholds_are_exported(self):
        self.assertEqual(self.rec["thresholds"], {
            "who_short_8h": 100, "eu_target_8h": 120,
            "at_info_1h": 180, "who_peak_season": 60})

    def test_training_scale_is_exported_for_the_legend(self):
        self.assertEqual(len(self.rec["training_scale"]), 4)
        self.assertIsNone(self.rec["training_scale"][-1]["below"])

    def test_not_demo(self):
        self.assertFalse(self.rec["demo"])

    def test_info_threshold_flag(self):
        page = oz.parse_html(read(LIVE))
        page.readings[0].akt_1h = 185
        rec = oz.build_record(page, [])
        self.assertTrue(rec["summary"]["any_info_threshold"])
        self.assertEqual(rec["stations"][0]["training"]["status"], "critical")


class TestDemo(unittest.TestCase):
    def test_demo_is_flagged_and_has_history(self):
        page, entries = oz.demo_history(48)
        rec = oz.build_record(page, entries, demo=True)
        self.assertTrue(rec["demo"])
        self.assertGreaterEqual(rec["history"]["points"], 40)
        self.assertTrue(rec["hour_profile"]["sufficient"])

    def test_demo_is_deterministic(self):
        a, _ = oz.demo_history(24)
        b, _ = oz.demo_history(24)
        self.assertEqual([r.akt_1h for r in a.readings],
                         [r.akt_1h for r in b.readings])

    def test_sulzberg_stays_higher_at_night(self):
        # Physik-Plausibilitaet der Demo: Hoehenstation flacher und im Mittel
        # hoeher als die Talstationen.
        _, entries = oz.demo_history(24)
        night = [e for e in entries if datetime.fromisoformat(
            e["source_time"]).hour == 4]
        self.assertTrue(night)
        e = night[0]["stations"]
        self.assertGreater(e["ATVA008"]["akt_1h"], e["ATVA002"]["akt_1h"])


class TestCli(unittest.TestCase):
    def test_html_and_out(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "data.json"
            rc = oz.main(["--html", str(LIVE), "--out", str(out), "--quiet"])
            self.assertEqual(rc, 0)
            rec = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(len(rec["stations"]), 4)

    def test_log_writes_history(self):
        with tempfile.TemporaryDirectory() as d:
            hist = Path(d) / "h.jsonl"
            oz.main(["--html", str(LIVE), "--log", "--history", str(hist),
                     "--quiet"])
            self.assertEqual(len(oz.load_history(hist)), 1)

    def test_station_filter(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "d.json"
            oz.main(["--html", str(LIVE), "--station", "sulzberg",
                     "--out", str(out), "--quiet"])
            rec = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual([s["id"] for s in rec["stations"]], ["ATVA008"])

    def test_station_filter_no_match_returns_1(self):
        self.assertEqual(
            oz.main(["--html", str(LIVE), "--station", "innsbruck", "--quiet"]), 1)

    def test_strict_failure_returns_2(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "bad.htm"
            bad.write_text("<html><body>nix</body></html>", encoding="utf-8")
            self.assertEqual(oz.main(["--html", str(bad), "--strict",
                                      "--quiet"]), 2)

    def test_out_is_written_atomically(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "data.json"
            oz.main(["--html", str(LIVE), "--out", str(out), "--quiet"])
            self.assertFalse((Path(d) / "data.json.tmp").exists(),
                             "Temp-Datei muss weggeraeumt sein")


if __name__ == "__main__":
    unittest.main(verbosity=2)
