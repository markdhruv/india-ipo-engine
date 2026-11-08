"""Listing-outcome capture, the calendar guards, and the regime read."""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ipo_engine.outcomes import Regime, accuracy, regime
from ipo_engine.sources.listing import ListingOutcome, _first_bar_from
from ipo_engine.sources.nse import _issue_price, _plausible_listing_date
from ipo_engine.store import Store
from ipo_engine.util import load_config

CFG = load_config()


def _epoch(day):
    return int(dt.datetime.combine(day, dt.time(), tzinfo=dt.UTC).timestamp())


def _chart(days, opens, closes, splits=()):
    """A Yahoo-shaped chart payload, optionally with split events."""
    result = {
        "timestamp": [_epoch(d) for d in days],
        "indicators": {"quote": [{"open": opens, "close": closes}]},
    }
    if splits:
        result["events"] = {"splits": {
            str(_epoch(day)): {"date": _epoch(day), "numerator": num,
                               "denominator": den, "splitRatio": f"{num}:{den}"}
            for day, num, den in splits}}
    return {"chart": {"result": [result]}}


class TestCalendarGuards(unittest.TestCase):
    """NSE's calendar carries stale rows that would poison the dataset."""

    def test_issue_price_outside_its_own_band_is_rejected(self):
        # Ola Electric: issuePrice 11 against a band of 72-76.
        row = {"issuePrice": "11", "priceRange": "Rs.72 to Rs.76"}
        self.assertEqual(_issue_price(row), 76.0)

    def test_issue_price_inside_the_band_is_kept(self):
        row = {"issuePrice": "150", "priceRange": "Rs.142 to Rs.150"}
        self.assertEqual(_issue_price(row), 150.0)

    def test_cut_off_price_at_the_band_edge_is_tolerated(self):
        row = {"issuePrice": "142", "priceRange": "Rs.142 to Rs.150"}
        self.assertEqual(_issue_price(row), 142.0)

    def test_missing_issue_price_falls_back_to_the_band_top(self):
        self.assertEqual(_issue_price({"priceRange": "Rs.90 to Rs.95"}), 95.0)

    def test_listing_years_after_close_is_rejected(self):
        # Ola Electric: closed Aug 2024, calendar claims it listed Nov 2026.
        self.assertIsNone(_plausible_listing_date(
            dt.date(2024, 8, 6), dt.date(2026, 11, 6), "Ola"))

    def test_listing_before_close_is_rejected(self):
        self.assertIsNone(_plausible_listing_date(
            dt.date(2026, 11, 6), dt.date(2026, 11, 1), "Backwards Ltd"))

    def test_normal_t_plus_three_listing_is_kept(self):
        listed = dt.date(2026, 11, 5)
        self.assertEqual(_plausible_listing_date(
            dt.date(2026, 11, 2), listed, "Normal Ltd"), listed)


class TestListingParse(unittest.TestCase):
    def test_takes_the_first_bar_on_or_after_listing(self):
        days = [dt.date(2026, 11, 3), dt.date(2026, 11, 5), dt.date(2026, 11, 6)]
        bar = _first_bar_from(_chart(days, [10.0, 91.0, 95.0],
                                     [11.0, 94.8, 96.0]), dt.date(2026, 11, 5))
        self.assertEqual(bar, (dt.date(2026, 11, 5), 91.0, 94.8))

    def test_skips_empty_leading_bars(self):
        days = [dt.date(2026, 11, 5), dt.date(2026, 11, 6)]
        bar = _first_bar_from(_chart(days, [None, 95.0], [None, 96.0]),
                              dt.date(2026, 11, 5))
        self.assertEqual(bar[0], dt.date(2026, 11, 6))

    def test_missing_close_is_tolerated(self):
        days = [dt.date(2026, 11, 5)]
        bar = _first_bar_from(_chart(days, [91.0], [None]), dt.date(2026, 11, 5))
        self.assertEqual(bar, (dt.date(2026, 11, 5), 91.0, None))

    def test_no_bars_after_listing_returns_nothing(self):
        days = [dt.date(2026, 11, 1)]
        self.assertIsNone(_first_bar_from(_chart(days, [10.0], [11.0]),
                                          dt.date(2026, 11, 5)))

    def test_empty_payload_returns_nothing(self):
        self.assertIsNone(_first_bar_from({"chart": {"result": None}},
                                          dt.date(2026, 11, 5)))

    def test_gain_is_measured_against_the_open(self):
        """GMP is a quote on the open, so that is what gain means here."""
        out = ListingOutcome("BCSSL.NS", dt.date(2026, 11, 5), 91.0, 94.81, "yahoo.NS")
        self.assertAlmostEqual(out.gain_pct(82.0), 10.9756, places=3)
        self.assertAlmostEqual(out.close_gain_pct(82.0), 15.6220, places=3)

    def test_gain_without_an_issue_price_is_unknown(self):
        out = ListingOutcome("X.NS", dt.date(2026, 11, 5), 91.0, 94.0, "yahoo.NS")
        self.assertIsNone(out.gain_pct(None))
        self.assertIsNone(out.gain_pct(0))


class TestRegime(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._dir.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self._dir.cleanup()

    def _record(self, gains, segment="MAINBOARD"):
        for i, gain in enumerate(gains):
            issue = 100.0
            self.store.record_outcome(
                f"KEY{i}", f"Co {i}", issue, issue * (1 + gain / 100.0),
                segment=segment, listing_date=dt.date(2026, 10, 1) + dt.timedelta(days=i),
                source="test")

    def test_no_history_is_reported_as_unknown(self):
        self.assertEqual(regime(self.store, CFG).label, "UNKNOWN")
        self.assertIn("no listing history", regime(self.store, CFG).describe())

    def test_strong_tape_reads_hot(self):
        self._record([25.0, 30.0, 15.0, 40.0, 12.0])
        reading = regime(self.store, CFG)
        self.assertEqual(reading.label, "HOT")
        self.assertEqual(reading.hit_rate, 1.0)

    def test_weak_tape_reads_cold(self):
        self._record([-5.0, -12.0, 2.0, -8.0, -1.0])
        self.assertEqual(regime(self.store, CFG).label, "COLD")

    def test_mixed_tape_reads_normal(self):
        self._record([11.0, 2.0, 6.0, -1.0, 8.0, 4.0])
        self.assertEqual(regime(self.store, CFG).label, "NORMAL")

    def test_hit_rate_counts_against_the_configured_bar(self):
        self._record([5.0, 15.0, 9.9, 10.1])
        self.assertAlmostEqual(regime(self.store, CFG).hit_rate, 0.5)

    def test_lookback_limits_the_sample(self):
        self._record([1.0] * 30)
        self.assertEqual(regime(self.store, CFG, lookback=10).sample, 10)

    def test_median_resists_a_single_outlier(self):
        """One +300% listing must not make a flat market look hot."""
        self._record([1.0, 2.0, 3.0, 4.0, 300.0])
        reading = regime(self.store, CFG)
        self.assertEqual(reading.label, "NORMAL")
        self.assertLess(reading.median_gain, 10.0)
        self.assertGreater(reading.mean_gain, 10.0)      # the mean is fooled


class TestAccuracy(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._dir.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self._dir.cleanup()

    def test_no_pairs_reports_an_empty_sample(self):
        self.assertEqual(accuracy(self.store, CFG)["sample"], 0)

    def test_error_and_bias_are_measured_against_the_open(self):
        from ipo_engine.models import IPO
        for i, (predicted, actual) in enumerate(((12.0, 10.0), (20.0, 25.0))):
            ipo = IPO(name=f"Co {i}", symbol=f"SYM{i}", price=100.0)
            ipo.expected_gain_pct = predicted
            key = self.store.assign_identity(ipo)
            self.store.save_snapshot(ipo)
            self.store.record_outcome(key, ipo.name, 100.0, 100.0 + actual,
                                      segment="MAINBOARD",
                                      listing_date=dt.date(2026, 10, 1), source="test")
        result = accuracy(self.store, CFG)
        self.assertEqual(result["sample"], 2)
        self.assertAlmostEqual(result["mean_abs_error"], 3.5)
        self.assertAlmostEqual(result["bias"], -1.5)      # net under-estimate


class TestFinalBook(unittest.TestCase):
    """The post-listing book, which NSE does retain (it is not erased)."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._dir.name) / "t.db")
        self.store.record_outcome("JPL", "Jio Platforms Limited", 1119.0, 1358.0,
                                  segment="MAINBOARD",
                                  listing_date=dt.date(2026, 10, 28), source="test")

    def tearDown(self):
        self.store.close()
        self._dir.cleanup()

    def test_book_attaches_to_an_existing_outcome(self):
        self.assertFalse(self.store.has_final_book("JPL"))
        self.store.record_final_book("JPL", {"TOTAL": 12.45, "QIB": 37.22,
                                             "NII": 8.1, "RETAIL": 3.4})
        self.assertTrue(self.store.has_final_book("JPL"))

    def test_pairs_need_both_a_book_and_a_gain(self):
        self.assertEqual(self.store.historical_pairs(), [])
        self.store.record_final_book("JPL", {"TOTAL": 12.45, "QIB": 37.22})
        rows = self.store.historical_pairs()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["final_qib_sub"], 37.22)
        self.assertAlmostEqual(rows[0]["gain_open"], (1358.0 - 1119.0) / 1119.0 * 100)

    def test_an_outcome_without_a_book_is_excluded(self):
        self.store.record_outcome("OTHER", "Other Ltd", 100.0, 110.0,
                                  segment="MAINBOARD",
                                  listing_date=dt.date(2026, 10, 1), source="test")
        self.store.record_final_book("JPL", {"TOTAL": 12.45})
        self.assertEqual([r["ipo_key"] for r in self.store.historical_pairs()], ["JPL"])

    def test_missing_categories_are_stored_as_null(self):
        self.store.record_final_book("JPL", {"TOTAL": 12.45})
        row = self.store.historical_pairs()[0]
        self.assertIsNone(row["final_qib_sub"])


class TestSplitAdjustment(unittest.TestCase):
    """Yahoo back-adjusts OHLC, so later splits must be undone.

    Waterways Leisure split 10:1 after listing and therefore read as -91.6%
    when it actually listed at -15.7%.
    """

    def test_split_after_listing_is_reversed(self):
        from ipo_engine.sources.listing import _first_bar_from
        listed = dt.date(2026, 7, 1)
        payload = _chart([listed], [68.1], [70.0],
                         splits=[(dt.date(2026, 8, 25), 10.0, 1.0)])
        day, open_price, close_price = _first_bar_from(payload, listed)
        self.assertAlmostEqual(open_price, 681.0, places=3)
        self.assertAlmostEqual(close_price, 700.0, places=3)

    def test_split_before_listing_is_left_alone(self):
        """Already reflected in the traded price; undoing it would be wrong."""
        from ipo_engine.sources.listing import _first_bar_from
        listed = dt.date(2026, 7, 1)
        payload = _chart([listed], [68.1], [70.0],
                         splits=[(dt.date(2026, 1, 5), 10.0, 1.0)])
        self.assertAlmostEqual(_first_bar_from(payload, listed)[1], 68.1)

    def test_several_splits_compound(self):
        from ipo_engine.sources.listing import _first_bar_from
        listed = dt.date(2026, 1, 1)
        payload = _chart([listed], [10.0], [11.0],
                         splits=[(dt.date(2026, 3, 1), 2.0, 1.0),
                                 (dt.date(2026, 6, 1), 5.0, 1.0)])
        self.assertAlmostEqual(_first_bar_from(payload, listed)[1], 100.0)

    def test_no_events_means_no_adjustment(self):
        from ipo_engine.sources.listing import _first_bar_from
        listed = dt.date(2026, 10, 7)
        payload = _chart([listed], [84.0], [85.0])
        self.assertAlmostEqual(_first_bar_from(payload, listed)[1], 84.0)

    def test_corrected_gain_matches_reality(self):
        from ipo_engine.sources.listing import ListingOutcome, _first_bar_from
        listed = dt.date(2026, 7, 1)
        payload = _chart([listed], [68.1], [70.0],
                         splits=[(dt.date(2026, 8, 25), 10.0, 1.0)])
        day, open_price, close_price = _first_bar_from(payload, listed)
        out = ListingOutcome("CORDELIA.NS", day, open_price, close_price, "yahoo.NS")
        self.assertAlmostEqual(out.gain_pct(808.0), -15.7, places=1)


class TestFetchWindow(unittest.TestCase):
    """The request has to be wide enough for the split to be in it.

    The first attempt at split correction added events=split but kept a
    window ending ten days after listing. Splits typically happen months
    later, so nothing was returned and the correction silently did nothing -
    while the unit tests, which fed it a payload that already contained the
    split, stayed green. This pins the window itself.
    """

    def _captured_url(self, listing_date):
        from ipo_engine.sources import listing as mod
        seen = {}

        class FakeFetcher:
            def get(self, url, **kw):
                seen["url"] = url
                return None

        mod.fetch_listing(FakeFetcher(), "TEST", listing_date)
        return seen["url"]

    @staticmethod
    def _param(url, name):
        from urllib.parse import parse_qs, urlparse
        return int(parse_qs(urlparse(url).query)[name][0])

    def test_window_reaches_the_present_for_an_old_listing(self):
        listed = dt.date(2025, 9, 3)
        url = self._captured_url(listed)
        end = dt.datetime.fromtimestamp(self._param(url, "period2"), dt.UTC).date()
        self.assertGreaterEqual(end, dt.datetime.now(dt.UTC).date())

    def test_split_events_are_requested(self):
        self.assertIn("events=split", self._captured_url(dt.date(2026, 1, 1)))

    def test_window_starts_before_the_listing(self):
        listed = dt.date(2026, 6, 1)
        url = self._captured_url(listed)
        start = dt.datetime.fromtimestamp(self._param(url, "period1"), dt.UTC).date()
        self.assertLess(start, listed)

    def test_window_is_valid_for_a_listing_today(self):
        listed = dt.datetime.now(dt.UTC).date()
        url = self._captured_url(listed)
        self.assertGreater(self._param(url, "period2"), self._param(url, "period1"))


class TestRankCorrelation(unittest.TestCase):
    def test_perfect_monotonic_relationship(self):
        from backtest import _spearman
        xs = [1, 2, 3, 4, 5, 6]
        self.assertAlmostEqual(_spearman(xs, [10, 20, 30, 40, 50, 60]), 1.0)
        self.assertAlmostEqual(_spearman(xs, [60, 50, 40, 30, 20, 10]), -1.0)

    def test_survives_an_extreme_outlier(self):
        """A +300% listing must not dominate the way it would under Pearson."""
        from backtest import _spearman
        xs = [1, 2, 3, 4, 5, 6]
        self.assertAlmostEqual(_spearman(xs, [10, 20, 30, 40, 50, 9000]), 1.0)

    def test_ties_are_averaged(self):
        from backtest import _spearman
        self.assertIsNotNone(_spearman([1, 1, 1, 2, 3], [5, 5, 6, 7, 8]))

    def test_too_few_points_is_undefined(self):
        from backtest import _spearman
        self.assertIsNone(_spearman([1, 2], [3, 4]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
