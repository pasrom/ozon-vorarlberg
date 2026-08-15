#!/usr/bin/env python3
"""Tests for eea_archive.py and the archive wiring in ozon_vorarlberg.py.

Runs entirely offline: the network functions (download) are never touched and
Parquet inputs are synthesised. Usage: python3 -m unittest -v
"""

import json
import math
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import eea_archive as ea
import ozon_vorarlberg as oz

TZ = oz._tz()


def hours(start: datetime, values: list[float]) -> list[tuple[datetime, float]]:
    return [(start + timedelta(hours=i), v) for i, v in enumerate(values)]


def season_series(days: int, start: datetime | None = None,
                  base: float = 90, amp: float = 45):
    """Deterministische Stundenreihe mit Peak um 16 Uhr."""
    start = start or datetime(2024, 5, 1, 0, 0, tzinfo=TZ)
    out = []
    for i in range(days * 24):
        ts = start + timedelta(hours=i)
        v = base + amp * math.cos((ts.hour - 16) / 24 * 2 * math.pi)
        out.append((ts, round(v, 1)))
    return out


class TestTimezone(unittest.TestCase):
    """The costliest mistake here: being one hour off shifts the
    recommended training window. Established against the official source in
    TestAgainstOfficialSource."""

    def test_eea_stamps_are_utc(self):
        self.assertEqual(ea.EEA_TZ.utcoffset(None), timedelta(0))

    def test_aggregation_day_boundary_is_fixed_cet(self):
        # The source aggregates daily metrics in fixed CET but shows
        # individual values in local time. Two different things.
        self.assertEqual(ea.AGG_TZ.utcoffset(None), timedelta(hours=1))

    def test_summer_stamp_shifts_two_hours(self):
        naive = datetime(2026, 8, 14, 10, 0)
        local = naive.replace(tzinfo=ea.EEA_TZ).astimezone(TZ)
        self.assertEqual(local.hour, 12, "10:00 UTC = 12:00 CEST")

    def test_winter_stamp_shifts_one_hour(self):
        naive = datetime(2026, 1, 14, 10, 0)
        local = naive.replace(tzinfo=ea.EEA_TZ).astimezone(TZ)
        self.assertEqual(local.hour, 11, "10:00 UTC = 11:00 CET")

    def test_midnight_window_belongs_to_the_previous_day(self):
        # 24:00 counts towards the old day — otherwise the evening window
        # moves into the next day and inflates its maximum.
        end = datetime(2026, 8, 15, 1, 0, tzinfo=TZ)      # 00:00 MEZ
        self.assertEqual(ea.day_of_window(end), date(2026, 8, 14))
        later = datetime(2026, 8, 15, 2, 0, tzinfo=TZ)    # 01:00 MEZ
        self.assertEqual(ea.day_of_window(later), date(2026, 8, 15))


class TestMapping(unittest.TestCase):
    def test_every_station_has_a_sampling_point(self):
        self.assertEqual(set(ea.SAMPLING_POINT), set(oz.STATION_ORDER))

    def test_sampling_points_are_vorarlberg_and_ozone(self):
        for sid, point in ea.SAMPLING_POINT.items():
            parts = point.split(".")
            self.assertEqual(parts[0], "SPO", point)
            self.assertEqual(parts[1], "08", f"{sid}: network 08 = Vorarlberg")
            self.assertEqual(parts[4], "7", f"{sid}: pollutant 7 = ozone")

    def test_sampling_points_are_unique(self):
        vals = list(ea.SAMPLING_POINT.values())
        self.assertEqual(len(vals), len(set(vals)))

    def test_datasets_cover_a_contiguous_range(self):
        spans = [(y0, y1) for _c, y0, y1, _t in ea.DATASETS]
        for (_a, end), (start, _b) in zip(spans, spans[1:]):
            self.assertEqual(start, end + 1, "no gap between containers")

    def test_blob_url_shape(self):
        u = ea.blob_url("airquality-p", "SPO.08.0503.3670.7.1")
        self.assertTrue(u.startswith("https://"))
        self.assertTrue(u.endswith("/AT/SPO.08.0503.3670.7.1.parquet"))


class TestRolling8h(unittest.TestCase):
    def test_flat_series(self):
        s = hours(datetime(2024, 7, 1, 0, tzinfo=TZ), [100] * 24)
        r = ea.rolling_8h(s)
        self.assertTrue(all(abs(v - 100) < 1e-9 for _t, v in r))

    def test_window_is_assigned_to_its_end(self):
        # Starts 00:00..07:00 cover 00:00-08:00; the mean belongs at 08:00,
        # not at 07:00.
        s = hours(datetime(2024, 7, 1, 0, tzinfo=TZ), list(range(1, 25)))
        r = dict(ea.rolling_8h(s))
        end = datetime(2024, 7, 1, 8, tzinfo=TZ)
        self.assertAlmostEqual(r[end], sum(range(1, 9)) / 8)
        # The first FULL window ends at 08:00; before that there are only
        # partial windows from EIGHT_H_MIN_VALID values onwards.
        self.assertNotIn(datetime(2024, 7, 1, 5, tzinfo=TZ), r)

    def test_short_start_is_dropped_until_min_valid(self):
        s = hours(datetime(2024, 7, 1, 0, tzinfo=TZ), [50] * 10)
        r = ea.rolling_8h(s)
        # The first hours have fewer than EIGHT_H_MIN_VALID predecessors.
        self.assertEqual(len(r), 10 - ea.EIGHT_H_MIN_VALID + 1)

    def test_gap_breaks_the_window(self):
        a = hours(datetime(2024, 7, 1, 0, tzinfo=TZ), [10] * 8)
        b = hours(datetime(2024, 7, 2, 0, tzinfo=TZ), [200] * 8)
        r = dict(ea.rolling_8h(a + b))
        # The first hour after the gap must not average across it.
        first_after = datetime(2024, 7, 2, 6, tzinfo=TZ)
        self.assertIn(first_after, r)
        self.assertAlmostEqual(r[first_after], 200.0,
                               msg="the mean must not include the 10s")

    def test_empty(self):
        self.assertEqual(ea.rolling_8h([]), [])


class TestDailyMax(unittest.TestCase):
    def test_picks_max_per_day(self):
        # Midday values: far from the day boundary, so the test only checks
        # the maximum, not the zone logic.
        s = (hours(datetime(2024, 7, 1, 12, tzinfo=TZ), [10, 90, 40]) +
             hours(datetime(2024, 7, 2, 12, tzinfo=TZ), [70, 20]))
        d = ea.daily_max(s)
        self.assertEqual(d[date(2024, 7, 1)], 90)
        self.assertEqual(d[date(2024, 7, 2)], 70)

    def test_day_boundary_is_cet_not_local(self):
        # Summer: 00:00 CEST is 23:00 CET of the previous day. The source
        # aggregates in fixed CET, so this hour belongs to the day before.
        s = hours(datetime(2024, 7, 2, 0, tzinfo=TZ), [55])
        self.assertEqual(list(ea.daily_max(s)), [date(2024, 7, 1)])
        s2 = hours(datetime(2024, 7, 2, 1, tzinfo=TZ), [55])
        self.assertEqual(list(ea.daily_max(s2)), [date(2024, 7, 2)])

    def test_winter_has_no_offset(self):
        s = hours(datetime(2024, 1, 2, 0, tzinfo=TZ), [55])
        self.assertEqual(list(ea.daily_max(s)), [date(2024, 1, 2)])

    def test_empty(self):
        self.assertEqual(ea.daily_max([]), {})


class TestPeakSeason(unittest.TestCase):
    def test_needs_enough_days(self):
        dmax = {date(2024, 7, d): 100 for d in range(1, 11)}
        self.assertIsNone(ea.peak_season_mean(dmax, 2024))

    def test_picks_the_highest_six_month_window(self):
        dmax = {}
        for m in range(1, 13):
            for d in range(1, 29):
                # summer clearly higher than winter.
                dmax[date(2024, m, d)] = 120 if 4 <= m <= 9 else 40
        v = ea.peak_season_mean(dmax, 2024)
        self.assertIsNotNone(v)
        self.assertAlmostEqual(v, 120.0, delta=0.1)

    def test_ignores_other_years(self):
        dmax = {date(2023, m, d): 999 for m in range(1, 13) for d in range(1, 29)}
        dmax.update({date(2024, m, d): 50 for m in range(1, 13)
                     for d in range(1, 29)})
        self.assertAlmostEqual(ea.peak_season_mean(dmax, 2024), 50.0, delta=0.1)


class TestYearlyStats(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.series = season_series(120, datetime(2024, 4, 1, 0, tzinfo=TZ))
        cls.stats = ea.yearly_stats(cls.series)

    def test_year_present(self):
        self.assertIn("2024", self.stats)

    def test_counts_and_maxima(self):
        d = self.stats["2024"]
        self.assertEqual(d["hours"], 120 * 24)
        # 121, not 120: the series starts at 00:00 CEST, which in fixed CET is
        # still 31 March. Aggregation follows the source, not the calendar
        # of the input.
        self.assertEqual(d["days_measured"], 121)
        self.assertEqual(d["max_1h"], 135)          # base 90 + amp 45
        self.assertLess(d["max_8h"], d["max_1h"])

    def test_exceedance_counts_are_consistent(self):
        d = self.stats["2024"]
        self.assertLessEqual(d["days_8h_over_120"], d["days_8h_over_100"])

    def test_no_info_threshold_hours_below_180(self):
        self.assertEqual(self.stats["2024"]["hours_1h_over_180"], 0)

    def test_info_threshold_is_counted(self):
        s = season_series(30, datetime(2024, 7, 1, 0, tzinfo=TZ), base=150, amp=45)
        st = ea.yearly_stats(s)["2024"]
        self.assertGreater(st["hours_1h_over_180"], 0)

    def test_current_year_is_flagged_partial(self):
        y = datetime.now(TZ).year
        s = season_series(40, datetime(y, 4, 1, 0, tzinfo=TZ))
        st = ea.yearly_stats(s)[str(y)]
        self.assertTrue(st["partial"], "the running year is an interim figure")

    def test_past_year_is_not_partial(self):
        self.assertFalse(self.stats["2024"]["partial"])

    def test_coverage_is_a_fraction(self):
        c = self.stats["2024"]["coverage"]
        self.assertTrue(0 < c <= 1)


class TestHourProfile(unittest.TestCase):
    def test_24_slots_and_quartile_order(self):
        p = ea.hour_profile(season_series(120, datetime(2024, 4, 1, 0, tzinfo=TZ)))
        for k in ("median", "p25", "p75"):
            self.assertEqual(len(p[k]), 24)
        for h in range(24):
            if None in (p["p25"][h], p["median"][h], p["p75"][h]):
                continue
            self.assertLessEqual(p["p25"][h], p["median"][h])
            self.assertLessEqual(p["median"][h], p["p75"][h])

    def test_only_season_months_are_used(self):
        winter = season_series(60, datetime(2024, 1, 1, 0, tzinfo=TZ))
        p = ea.hour_profile(winter)
        self.assertEqual(p["n_days"], 0, "January is not ozone season")
        self.assertTrue(all(v is None for v in p["median"]))

    def test_season_months_constant(self):
        self.assertEqual(ea.SEASON_MONTHS, (4, 5, 6, 7, 8, 9))

    def test_empty_series(self):
        p = ea.hour_profile([])
        self.assertEqual(p["n_days"], 0)


class TestBestWindow(unittest.TestCase):
    def test_finds_the_night_minimum(self):
        s = season_series(120, datetime(2024, 4, 1, 0, tzinfo=TZ))
        bw = ea.best_window(ea.hour_profile(s))
        self.assertIsNotNone(bw)
        # The peak is at 16:00, so the minimum is around 04:00.
        self.assertLessEqual(bw["from_hour"], 5)
        self.assertEqual(bw["to_hour"] - bw["from_hour"], 3)

    def test_none_when_profile_is_empty(self):
        self.assertIsNone(ea.best_window({"median": [None] * 24}))

    def test_respects_width(self):
        prof = {"median": [50] * 24}
        self.assertEqual(ea.best_window(prof, width=5)["to_hour"] -
                         ea.best_window(prof, width=5)["from_hour"], 5)


class TestDayContext(unittest.TestCase):
    def _multi_year(self):
        s = []
        for y in range(2015, 2026):
            for off in range(-3, 4):
                d = date(y, 8, 14) + timedelta(days=off)
                s += hours(datetime(d.year, d.month, d.day, 12, tzinfo=TZ),
                           [80 + off + (y - 2015) * 2])
        return s

    def test_returns_reference_without_today(self):
        ctx = ea.day_of_year_context(self._multi_year(), date(2026, 8, 14))
        self.assertIsNotNone(ctx)
        self.assertNotIn("today_max_1h", ctx,
                         "the archive must not supply a daily value")
        self.assertIn("reference_sorted", ctx)
        self.assertEqual(ctx["reference_sorted"], sorted(ctx["reference_sorted"]))

    def test_excludes_the_target_year(self):
        s = self._multi_year()
        s += hours(datetime(2026, 8, 14, 12, tzinfo=TZ), [999])
        ctx = ea.day_of_year_context(s, date(2026, 8, 14))
        self.assertNotIn(999, ctx["reference_sorted"])
        self.assertNotIn(2026, ctx["years"])

    def test_quantiles_are_ordered(self):
        ctx = ea.day_of_year_context(self._multi_year(), date(2026, 8, 14))
        self.assertLessEqual(ctx["p10"], ctx["median"])
        self.assertLessEqual(ctx["median"], ctx["p90"])
        self.assertLessEqual(ctx["p90"], ctx["max"])

    def test_none_with_too_little_reference(self):
        s = hours(datetime(2020, 8, 14, 12, tzinfo=TZ), [100])
        self.assertIsNone(ea.day_of_year_context(s, date(2026, 8, 14)))

    def test_window_limits_which_days_count(self):
        s = self._multi_year()
        narrow = ea.day_of_year_context(s, date(2026, 8, 14), window_days=0)
        wide = ea.day_of_year_context(s, date(2026, 8, 14), window_days=3)
        self.assertLess(narrow["n_reference_days"], wide["n_reference_days"])


class TestReadParquet(unittest.TestCase):
    """read_parquet against a Parquet file we write ourselves — checks the
    validity filter and timezone conversion without network access."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError:
            self.skipTest("pyarrow not installed")
        self.path = Path(self.dir.name) / "t.parquet"
        starts = [datetime(2026, 8, 14, h) for h in (10, 11, 12, 13)]
        table = pa.table({
            "Samplingpoint": ["AT/SPO.08.0503.3670.7.1"] * 4,
            "Pollutant": [7] * 4,
            "Start": starts,
            "End": [s + timedelta(hours=1) for s in starts],
            "Value": [100.0, 110.0, -999.0, 130.0],
            "Validity": [1, 3, -1, 1],
        })
        pq.write_table(table, self.path)

    def tearDown(self):
        self.dir.cleanup()

    def test_invalid_rows_are_dropped(self):
        rows = ea.read_parquet(self.path)
        self.assertEqual(len(rows), 3, "validity -1 must be dropped")
        self.assertNotIn(-999.0, [v for _t, v in rows])

    def test_validity_3_counts_as_valid(self):
        self.assertIn(110.0, [v for _t, v in ea.read_parquet(self.path)])

    def test_timestamps_are_shifted_to_local_summer_time(self):
        rows = ea.read_parquet(self.path)
        self.assertEqual(rows[0][0].hour, 12, "10:00 UTC = 12:00 CEST")
        self.assertEqual(str(rows[0][0].tzinfo), oz.TZ_NAME)


class TestAgainstOfficialSource(unittest.TestCase):
    """The decisive tests: EEA values against the official regional page.

    Reference values come from two independent sources:
      * Screenshots from 2026-07-30 — three times of day by four stations,
        plus the daily 8h maxima at 13:00.
      * The previous-day column of the page from 2026-08-14, for 08-13.

    Needs the parquet cache (eea_archive.py --build); without it these are
    skipped so the suite stays offline.
    """

    # Seitenlabel (Lokalzeit, Fensterende) -> erwarteter 1h-Wert
    HOURLY = {
        7: {"ATVA007": 83, "ATVA008": 134, "ATVA009": 73},
        8: {"ATVA007": 83, "ATVA002": 71, "ATVA008": 133, "ATVA009": 58},
        13: {"ATVA007": 130, "ATVA002": 127, "ATVA008": 137, "ATVA009": 129},
    }
    MAX8_0730 = {"ATVA007": 107, "ATVA002": 120, "ATVA008": 148, "ATVA009": 91}
    MAX1_0813 = {"ATVA002": 163, "ATVA007": 153, "ATVA008": 154, "ATVA009": 139}
    MAX8_0813 = {"ATVA002": 153, "ATVA007": 143, "ATVA008": 150, "ATVA009": 133}

    @classmethod
    def setUpClass(cls):
        cls.series = {}
        for sid, point in ea.SAMPLING_POINT.items():
            p = ea.cache_path("airquality-p", point)
            if not p.exists():
                raise unittest.SkipTest(
                    "parquet cache missing — run eea_archive.py --build first")
            cls.series[sid] = ea.read_parquet(p)

    def test_hourly_values_match_exactly(self):
        """11 Stundenwerte, vier Stationen, drei Uhrzeiten - alle exakt.

        This is the evidence for EEA_TZ = UTC. With fixed CET only 1 of 11
        matched.
        """
        for hour, row in self.HOURLY.items():
            for sid, want in row.items():
                # The page labels by window end, read_parquet by start.
                target = datetime(2026, 7, 30, hour - 1, tzinfo=TZ)
                got = next((v for ts, v in self.series[sid] if ts == target), None)
                self.assertIsNotNone(got, f"{sid} {hour}:00 fehlt")
                self.assertEqual(round(got), want, f"{sid} um {hour}:00")

    def test_daily_max_1h_matches(self):
        for sid, want in self.MAX1_0813.items():
            got = ea.daily_max(self.series[sid]).get(date(2026, 8, 13))
            self.assertEqual(round(got), want, sid)

    def test_daily_max_8h_matches(self):
        for sid, want in self.MAX8_0813.items():
            d = ea.daily_max(ea.rolling_8h(self.series[sid]), ea.day_of_window)
            self.assertEqual(round(d[date(2026, 8, 13)]), want, sid)

    def test_daily_max_8h_partial_day_matches(self):
        """Daily 8h max on 2026-07-30 at 13:00 — the test that pins down the
        day boundary. With the boundary in CEST instead of CET every station
        came out 1 to 6 ug/m3 too high."""
        cut = datetime(2026, 7, 30, 13, tzinfo=TZ)
        for sid, want in self.MAX8_0730.items():
            vals = [v for ts, v in ea.rolling_8h(self.series[sid])
                    if ea.day_of_window(ts) == date(2026, 7, 30) and ts <= cut]
            self.assertEqual(round(max(vals)), want, sid)


# ---------------------------------------------------------------------------
# Anbindung in ozon_vorarlberg.py
# ---------------------------------------------------------------------------

class TestRecentSeries(unittest.TestCase):
    def test_labelled_by_window_end(self):
        s = hours(datetime(2024, 7, 1, 10, tzinfo=TZ), [50, 60])
        r = ea.recent_series(s)
        self.assertEqual(r["labelled"], "window_end")
        # Start 10:00 covers 10-11, so it is exported as 11:00.
        self.assertTrue(r["t"][0].startswith("2024-07-01T11:00"))
        self.assertEqual(r["v"], [50.0, 60.0])

    def test_window_is_limited(self):
        s = hours(datetime(2024, 6, 1, 0, tzinfo=TZ), [50] * (40 * 24))
        r = ea.recent_series(s, days=7)
        self.assertLessEqual(len(r["t"]), 7 * 24 + 1)
        self.assertGreater(len(r["t"]), 7 * 24 - 2)

    def test_empty(self):
        self.assertEqual(ea.recent_series([])["t"], [])


class TestMergedHistory(unittest.TestCase):
    """Archive plus local log into one curve. The pitfall is the labelling:
    both series must run by window end, otherwise they sit an hour apart."""

    def _log(self, *hours_):
        return [{"source_time": f"2026-08-14T{h:02d}:00+02:00",
                 "stations": {"ATVA002": {"akt_1h": v, "max_8h": 100}}}
                for h, v in hours_]

    def _archive(self, *hours_):
        return {"stations": [{
            "id": "ATVA002", "short": "Lustenau", "slot": 1,
            "recent": {
                "t": [f"2026-08-14T{h:02d}:00+02:00" for h, _v in hours_],
                "v": [v for _h, v in hours_],
                "labelled": "window_end"},
        }]}

    def test_log_only(self):
        h = oz.history_series(self._log((17, 163), (18, 150)))
        self.assertEqual(h["points"], 2)
        self.assertEqual(h["sources"]["log"], 2)
        self.assertEqual(h["sources"]["archive"], 0)

    def test_archive_fills_the_earlier_hours(self):
        h = oz.history_series(self._log((17, 163)),
                              archive=self._archive((14, 90.0), (15, 100.0)))
        self.assertEqual(h["points"], 3)
        self.assertEqual(h["akt_1h"]["ATVA002"], [90.0, 100.0, 163.0])
        self.assertEqual(h["sources"], {"archive": 2, "log": 1,
                                        "archive_until": "2026-08-14T15:00+02:00"})

    def test_archive_wins_on_overlap(self):
        # The log records the rounded display, the archive the measurement.
        h = oz.history_series(self._log((15, 101)),
                              archive=self._archive((15, 100.4)))
        self.assertEqual(h["akt_1h"]["ATVA002"], [100.4])
        self.assertEqual(h["sources"]["archive"], 1)
        self.assertEqual(h["sources"]["log"], 0, "must not be counted twice")

    def test_time_axis_is_the_union_and_sorted(self):
        h = oz.history_series(self._log((20, 1), (17, 2)),
                              archive=self._archive((10, 3.0)))
        self.assertEqual(h["t"], ["2026-08-14T10:00+02:00",
                                  "2026-08-14T17:00+02:00",
                                  "2026-08-14T20:00+02:00"])

    def test_missing_station_hours_become_none(self):
        h = oz.history_series(self._log((17, 163)), archive=self._archive((10, 50.0)))
        for sid in oz.STATION_ORDER:
            if sid == "ATVA002":
                continue
            self.assertEqual(h["akt_1h"][sid], [None, None])

    def test_window_cutoff_applies_to_the_merged_series(self):
        arc = self._archive(*[(h, float(h)) for h in range(0, 24)])
        h = oz.history_series([], hours=6, archive=arc)
        self.assertLessEqual(h["span_hours"], 6)
        self.assertLess(h["points"], 24)

    def test_sources_counted_only_inside_the_window(self):
        arc = self._archive(*[(h, float(h)) for h in range(0, 24)])
        h = oz.history_series([], hours=3, archive=arc)
        self.assertEqual(h["sources"]["archive"], h["points"])



class TestPercentile(unittest.TestCase):
    def test_edges_and_middle(self):
        ref = [10, 20, 30, 40, 50]
        self.assertEqual(oz.percentile_of(ref, 5), 0)
        self.assertEqual(oz.percentile_of(ref, 30), 40)
        self.assertEqual(oz.percentile_of(ref, 99), 100)

    def test_none_inputs(self):
        self.assertIsNone(oz.percentile_of([1, 2], None))
        self.assertIsNone(oz.percentile_of([], 5))


class TestLoadArchive(unittest.TestCase):
    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(oz.load_archive(Path(d) / "nope.json"))

    def test_broken_json_is_tolerated(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.json"
            p.write_text("{ kaputt", encoding="utf-8")
            self.assertIsNone(oz.load_archive(p))

    def test_empty_stations_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.json"
            p.write_text(json.dumps({"stations": []}), encoding="utf-8")
            self.assertIsNone(oz.load_archive(p))


class TestArchiveBlock(unittest.TestCase):
    def _fake_archive(self):
        return {
            "built_utc": "2026-08-14T15:00:00+00:00",
            "years": ["2024", "2025"],
            "overall_best_window": {"from_hour": 5, "to_hour": 8, "mean": 37},
            "source": {"blob": "x"},
            "stations": [{
                "id": "ATVA002", "short": "Lustenau", "chart_label": "Lustenau",
                "slot": 1, "hours": 100, "first": "2024-01-01T00:00",
                "last": "2026-08-14T09:00",
                "hour_profile": {"median": [1] * 24, "p25": [0] * 24,
                                 "p75": [2] * 24, "n_days": 300,
                                 "years": [2022, 2026]},
                "best_window": {"from_hour": 5, "to_hour": 8, "mean": 40},
                "yearly": {"2025": {"peak_season_mean": 94.4, "partial": False}},
                "day_context": {"median": 108, "p90": 150, "max": 237,
                                "n_reference_days": 100, "years": [2003, 2025],
                                "month": 8, "day": 14, "window_days": 3,
                                "reference_sorted": [50, 100, 150, 200]},
            }],
        }

    def _readings(self, max_1h):
        return [oz.Reading(id="ATVA002", station="Lustenau Wiesenrain",
                           short="Lustenau", akt_1h=161, max_1h=max_1h,
                           max_8h=116, prev_max_1h=163, prev_max_8h=153)]

    def test_none_archive_returns_none(self):
        self.assertIsNone(oz.archive_block(None, self._readings(163)))

    def test_percentile_uses_the_live_value(self):
        blk = oz.archive_block(self._fake_archive(), self._readings(163))
        ctx = blk["stations"][0]["context"]
        self.assertEqual(ctx["today_max_1h"], 163)
        self.assertEqual(ctx["today_from"], "live")
        self.assertEqual(ctx["percentile"], 75)     # 163 liegt hinter 3 von 4

    def test_reference_values_are_not_copied_into_data_json(self):
        blk = oz.archive_block(self._fake_archive(), self._readings(163))
        self.assertNotIn("reference_sorted", blk["stations"][0]["context"])

    def test_missing_live_value_leaves_percentile_none(self):
        blk = oz.archive_block(self._fake_archive(), self._readings(None))
        self.assertIsNone(blk["stations"][0]["context"]["percentile"])

    def test_profile_and_yearly_survive(self):
        st = oz.archive_block(self._fake_archive(), self._readings(163))["stations"][0]
        self.assertEqual(st["hour_profile"]["n_days"], 300)
        self.assertIn("2025", st["yearly"])
        self.assertEqual(st["slot"], 1)

    def test_top_level_fields(self):
        blk = oz.archive_block(self._fake_archive(), self._readings(163))
        self.assertTrue(blk["available"])
        self.assertEqual(blk["overall_best_window"]["from_hour"], 5)
        self.assertEqual(blk["total_hours"], 100)

    def test_years_are_derived_from_the_data_not_copied(self):
        # The archive file's "years" field is deliberately NOT adopted: it is
        # rebuilt from the yearly keys actually present. Here the station only
        # has 2025, even though the file above claims 2024+2025.
        arc = self._fake_archive()
        arc["years"] = ["2024", "2025"]
        blk = oz.archive_block(arc, self._readings(163))
        self.assertEqual(blk["years"], ["2025"])


class TestBuildRecordWithArchive(unittest.TestCase):
    def test_archive_is_none_without_input(self):
        page = oz.parse_html(
            Path("fixtures/tab1O3_live.htm").read_bytes()
            .decode("iso-8859-1", errors="replace"))
        rec = oz.build_record(page, [])
        self.assertIsNone(rec["archive"])

    def test_json_serialisable_with_archive(self):
        page = oz.parse_html(
            Path("fixtures/tab1O3_live.htm").read_bytes()
            .decode("iso-8859-1", errors="replace"))
        arc = TestArchiveBlock()._fake_archive()
        rec = oz.build_record(page, [], archive=arc)
        json.dumps(rec, ensure_ascii=False)
        self.assertTrue(rec["archive"]["available"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
