"""Correctness tests for the scoring and parsing layers.

Several of these pin down bugs that were found against live data and would be
invisible in a smoke test - notably the HTML-entity trap, where an unescaped
``&#8377;22.98 Cr`` parses as the number 8377, and NSE's habit of reporting a
subscription multiple of literal "0.00" for fields it simply does not carry.

Run with:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ipo_engine.merge import _coherent, _reconcile_subscription
from ipo_engine.models import IPO, CategoryBid
from ipo_engine.scoring import (allotment_probability, expected_gain,
                                min_application_shares, score)
from ipo_engine.util import clean_text, load_config, norm_name, to_float

CFG = load_config()


def make_ipo(**kw) -> IPO:
    defaults = dict(name="Test Industries Limited", segment="MAINBOARD",
                    price=100.0, lot_size=100)
    defaults.update(kw)
    return IPO(**defaults)


class TestParsing(unittest.TestCase):
    def test_entities_are_decoded_before_numbers_are_read(self):
        # The live bug: '&#8377;' is the rupee sign; read raw it becomes 8377.
        self.assertEqual(clean_text("&#8377;22.98 Cr"), "₹22.98 Cr")
        self.assertEqual(to_float(clean_text("&#8377;22.98 Cr")), 22.98)

    def test_emoji_and_arrows_are_stripped(self):
        self.assertEqual(clean_text("&#128293;Rating ▲▼"), "Rating")

    def test_tags_removed_before_entities_decoded(self):
        self.assertEqual(clean_text("<b>&#8377;1</b> (1.06%)"), "₹1 (1.06%)")

    def test_to_float_handles_messy_inputs(self):
        self.assertEqual(to_float("12.79x"), 12.79)
        self.assertEqual(to_float("1,234.5"), 1234.5)
        self.assertEqual(to_float("2,000"), 2000.0)
        self.assertIsNone(to_float("-"))
        self.assertIsNone(to_float(""))

    def test_norm_name_matches_across_sources(self):
        self.assertEqual(norm_name("M. K. Sons Fine Jewels Limited"),
                         norm_name("M.K.Sons Fine Jewels"))
        self.assertEqual(norm_name("HGS (India) Limited"), norm_name("HGS (India)"))


class TestCategoryBid(unittest.TestCase):
    def test_zero_times_means_absent_not_zero_demand(self):
        """NSE sends times="0.00" in projections that do not carry the field."""
        bid = CategoryBid("QIB", shares_offered=1000, shares_bid=5000, times=0.0)
        self.assertEqual(bid.subscription(), 5.0)

    def test_real_multiple_wins_over_ratio(self):
        bid = CategoryBid("QIB", shares_offered=1000, shares_bid=5000, times=4.0)
        self.assertEqual(bid.subscription(), 4.0)

    def test_absent_everything_is_none(self):
        self.assertIsNone(CategoryBid("QIB").subscription())


class TestAllotmentMath(unittest.TestCase):
    def test_minimum_application_respects_rupee_thresholds(self):
        ipo = make_ipo(price=100.0, lot_size=100)      # one lot = Rs 10,000
        self.assertEqual(min_application_shares(ipo, "RETAIL"), 100)
        # sNII needs Rs 2 lakh -> 20 lots
        self.assertEqual(min_application_shares(ipo, "SNII"), 2000)
        # bNII needs Rs 10 lakh -> 100 lots
        self.assertEqual(min_application_shares(ipo, "BNII"), 10000)

    def test_undersubscribed_category_is_certain_allotment(self):
        ipo = make_ipo()
        ipo.bids["RETAIL"] = CategoryBid("RETAIL", shares_offered=1000,
                                         shares_bid=500)
        self.assertEqual(allotment_probability(ipo, "RETAIL"), 1.0)

    def test_odds_use_applications_not_one_over_subscription(self):
        """The HGS case: 312x subscribed, but odds come from lots vs applicants."""
        ipo = make_ipo(price=120.0, lot_size=1200)
        ipo.bids["RETAIL"] = CategoryBid(
            "RETAIL", shares_offered=959_990, shares_bid=226_288_800,
            applications=94_287)
        odds = allotment_probability(ipo, "RETAIL")
        expected = (959_990 / 1200) / 94_287          # ~0.85%
        self.assertAlmostEqual(odds, expected, places=6)
        self.assertLess(odds, 0.01)

    def test_falls_back_to_inverse_subscription_without_applications(self):
        ipo = make_ipo()
        ipo.bids["RETAIL"] = CategoryBid("RETAIL", times=20.0)
        self.assertAlmostEqual(allotment_probability(ipo, "RETAIL"), 0.05)

    def test_odds_never_exceed_one(self):
        ipo = make_ipo()
        ipo.bids["RETAIL"] = CategoryBid("RETAIL", shares_offered=1_000_000,
                                         shares_bid=2_000_000, applications=5)
        self.assertEqual(allotment_probability(ipo, "RETAIL"), 1.0)


class TestExpectedGain(unittest.TestCase):
    def test_gmp_is_shrunk_not_taken_at_face_value(self):
        ipo = make_ipo(gmp_pct=20.0)
        gain, _ = expected_gain(ipo, CFG)
        self.assertLess(gain, 20.0)

    def test_shrink_is_concave_so_high_gmp_is_discounted_harder(self):
        low = make_ipo(gmp_pct=10.0)
        high = make_ipo(gmp_pct=100.0)
        low_gain, _ = expected_gain(low, CFG)
        high_gain, _ = expected_gain(high, CFG)
        self.assertGreater(high_gain / 100.0, low_gain / 10.0 - 1)
        # retained fraction must fall as GMP rises
        self.assertLess(high_gain / 100.0, low_gain / 10.0)

    def test_sme_is_discounted_against_an_identical_mainboard_issue(self):
        main = make_ipo(gmp_pct=30.0, segment="MAINBOARD")
        sme = make_ipo(gmp_pct=30.0, segment="SME")
        self.assertGreater(expected_gain(main, CFG)[0], expected_gain(sme, CFG)[0])

    def test_undersubscribed_issue_is_dragged_down_and_avoided(self):
        """At under 1x the measured median outcome is zero, not negative.

        So the curve pulls the estimate toward zero rather than inventing a
        loss, and the verdict carries the warning.
        """
        weak = make_ipo(gmp_pct=5.0)
        weak.bids["TOTAL"] = CategoryBid("TOTAL", times=0.4)
        gmp_only = make_ipo(gmp_pct=5.0)

        self.assertLess(expected_gain(weak, CFG)[0], expected_gain(gmp_only, CFG)[0])
        self.assertEqual(score(weak, CFG).verdict, "AVOID")

    def test_quota_derived_qib_does_not_trigger_the_penalty(self):
        """Many SME issues reserve no QIB portion; a derived 0.02x is not a signal."""
        derived = make_ipo(gmp_pct=5.0, segment="SME")
        derived.bids["QIB"] = CategoryBid("QIB", shares_offered=3_015_000,
                                          shares_bid=68_400, offer_derived=True)
        reported = make_ipo(gmp_pct=5.0, segment="SME")
        reported.bids["QIB"] = CategoryBid("QIB", shares_offered=3_015_000,
                                           shares_bid=68_400, offer_derived=False)
        self.assertGreater(expected_gain(derived, CFG)[0],
                           expected_gain(reported, CFG)[0])

    def test_no_data_yields_no_estimate(self):
        gain, _ = expected_gain(make_ipo(), CFG)
        self.assertIsNone(gain)


class TestComposition(unittest.TestCase):
    """Who subscribed, not just how much.

    A headline total is the quota-weighted mean of the categories, so a large
    multiple can be carried entirely by retail and HNI money while
    institutions decline.
    """

    @staticmethod
    def _issue(total, qib, nii=None, retail=None, qib_derived=False, **kw):
        ipo = make_ipo(gmp_pct=12.0, **kw)
        ipo.bids["TOTAL"] = CategoryBid("TOTAL", times=total)
        ipo.bids["QIB"] = CategoryBid("QIB", times=qib, offer_derived=qib_derived)
        if nii is not None:
            ipo.bids["NII"] = CategoryBid("NII", times=nii)
        if retail is not None:
            ipo.bids["RETAIL"] = CategoryBid("RETAIL", times=retail)
        return ipo

    def test_qib_lagging_a_big_book_is_penalised(self):
        """The trap: 60x headline, but institutions barely showed up."""
        healthy = self._issue(total=60.0, qib=40.0, nii=90.0, retail=55.0)
        trap = self._issue(total=60.0, qib=2.0, nii=90.0, retail=120.0)
        self.assertGreater(expected_gain(healthy, CFG)[0],
                           expected_gain(trap, CFG)[0])

    def test_qib_lag_only_judged_on_a_well_subscribed_book(self):
        """At 3x total there is no 'headline' for QIB to lag behind."""
        small = self._issue(total=3.0, qib=0.5, nii=4.0, retail=3.0)
        reasons = expected_gain(small, CFG)[1]
        self.assertFalse(any("lags" in r for r in reasons))

    def test_quota_derived_qib_never_triggers_the_lag_penalty(self):
        derived = self._issue(total=60.0, qib=0.1, nii=90.0, retail=55.0,
                              qib_derived=True)
        reasons = expected_gain(derived, CFG)[1]
        self.assertFalse(any("lags" in r for r in reasons))

    def test_extreme_nii_leverage_is_penalised(self):
        balanced = self._issue(total=100.0, qib=80.0, nii=160.0, retail=90.0)
        leveraged = self._issue(total=100.0, qib=15.0, nii=400.0, retail=90.0)
        self.assertGreater(expected_gain(balanced, CFG)[0],
                           expected_gain(leveraged, CFG)[0])

    def test_ordinary_nii_skew_is_not_penalised(self):
        """Live SME books routinely run NII at 2-4x QIB; that is not a flag."""
        typical = self._issue(total=491.0, qib=259.0, nii=858.0, retail=556.0)
        reasons = expected_gain(typical, CFG)[1]
        self.assertFalse(any("funded applications" in r for r in reasons))

    def test_deep_qib_suppresses_the_leverage_flag(self):
        """Heavy NII beside a genuinely deep institutional book is enthusiasm."""
        ipo = self._issue(total=200.0, qib=150.0, nii=600.0, retail=180.0,
                          segment="MAINBOARD")
        reasons = expected_gain(ipo, CFG)[1]
        self.assertFalse(any("funded applications" in r for r in reasons))

    def test_a_thin_major_leg_is_penalised_despite_a_strong_headline(self):
        broad = self._issue(total=30.0, qib=25.0, nii=40.0, retail=28.0)
        narrow = self._issue(total=30.0, qib=25.0, nii=40.0, retail=0.6)
        self.assertGreater(expected_gain(broad, CFG)[0],
                           expected_gain(narrow, CFG)[0])

    def test_weakest_leg_is_reported(self):
        ipo = self._issue(total=30.0, qib=25.0, nii=40.0, retail=0.6)
        score(ipo, CFG)
        self.assertEqual(ipo.weakest_category, "RETAIL")
        self.assertAlmostEqual(ipo.weakest_category_sub, 0.6)

    def test_minor_reserved_portions_are_ignored(self):
        """An unfilled employee quota says nothing about the issue."""
        ipo = self._issue(total=30.0, qib=25.0, nii=40.0, retail=28.0)
        ipo.bids["EMPLOYEE"] = CategoryBid("EMPLOYEE", times=0.2)
        score(ipo, CFG)
        self.assertEqual(ipo.weakest_category, "QIB")

    def test_uniform_mild_coverage_does_not_look_like_a_winner(self):
        """Everything just-covered is not the same as everything strong."""
        mild = self._issue(total=1.2, qib=1.1, nii=1.3, retail=1.2)
        strong = self._issue(total=80.0, qib=70.0, nii=140.0, retail=75.0)
        self.assertLess(expected_gain(mild, CFG)[0], expected_gain(strong, CFG)[0])


class TestPerSegmentComposition(unittest.TestCase):
    """Thresholds now come from outcomes, not from what looks common.

    The first version was tuned to the normal range of live books - mainboard
    NII/QIB running 4.5-7.7 - and so was set at 10x, where it caught 2 issues
    in 156. Measured against realised gains, NII at 3x QIB already separates
    +9.1% from +27.3%, so the cut-off moved to where the signal is.
    """

    @staticmethod
    def _issue(segment, total, qib, nii, retail):
        ipo = make_ipo(gmp_pct=12.0, segment=segment)
        ipo.bids["TOTAL"] = CategoryBid("TOTAL", times=total)
        ipo.bids["QIB"] = CategoryBid("QIB", times=qib)
        ipo.bids["NII"] = CategoryBid("NII", times=nii)
        ipo.bids["RETAIL"] = CategoryBid("RETAIL", times=retail)
        return ipo

    def _reasons(self, *args):
        return expected_gain(self._issue(*args), CFG)[1]

    def test_mainboard_is_the_stricter_segment_on_nii_skew(self):
        """Measurement reversed my earlier assumption.

        I had mainboard tolerating more skew because its live books run
        hotter. Against outcomes the opposite holds, so mainboard flags at
        3x QIB and SME at 4x.
        """
        shape = (100.0, 25.0, 85.0, 110.0)      # NII/QIB = 3.4
        self.assertTrue(any("funded applications" in r
                            for r in self._reasons("MAINBOARD", *shape)))
        self.assertFalse(any("funded applications" in r
                             for r in self._reasons("SME", *shape)))

    def test_a_book_led_by_institutions_trips_nothing(self):
        """QIB at or above the headline is the healthy shape."""
        for segment in ("MAINBOARD", "SME"):
            reasons = self._reasons(segment, 60.0, 70.0, 90.0, 55.0)
            self.assertFalse(any("funded applications" in r or "lags" in r
                                 for r in reasons), f"{segment}: {reasons}")

    def test_qib_lag_fires_where_the_data_shows_a_gap(self):
        """QIB below ~0.75 of the book separated +9.1% from +18.7%."""
        lagging = self._reasons("MAINBOARD", 60.0, 40.0, 90.0, 70.0)   # 0.67
        self.assertTrue(any("lags" in r for r in lagging))

    def test_qib_lag_is_not_judged_on_a_small_book(self):
        self.assertFalse(any("lags" in r
                             for r in self._reasons("MAINBOARD", 4.0, 1.0, 6.0, 5.0)))

    def test_the_trap_is_caught_in_both_segments(self):
        for segment in ("MAINBOARD", "SME"):
            reasons = self._reasons(segment, 60.0, 2.0, 90.0, 120.0)
            self.assertTrue(any("lags" in r for r in reasons), segment)
            self.assertTrue(any("funded applications" in r for r in reasons), segment)

    def test_mainboard_penalises_the_trap_harder(self):
        """Institutions declining a mainboard issue is the louder failure."""
        def penalty(segment):
            trap = expected_gain(self._issue(segment, 60.0, 2.0, 90.0, 120.0), CFG)[0]
            # Control must be healthy under the measured rules: QIB leads.
            clean = expected_gain(self._issue(segment, 60.0, 70.0, 90.0, 55.0), CFG)[0]
            return clean - trap

        self.assertGreater(penalty("MAINBOARD"), penalty("SME"))


class TestCoherenceGuard(unittest.TestCase):
    def test_rejects_category_that_cannot_coexist_with_total(self):
        # Observed live: 'QIB 327x' on a 1.85x-subscribed issue.
        self.assertFalse(_coherent("QIB", 327.27, 1.85))

    def test_accepts_legitimate_skew(self):
        self.assertTrue(_coherent("QIB", 137.92, 231.18))
        self.assertTrue(_coherent("QIB", 66.72, 12.79))

    def test_total_is_never_rejected(self):
        self.assertTrue(_coherent("TOTAL", 500.0, 1.0))

    def test_published_ratio_corrects_a_derived_offer_size(self):
        target = make_ipo(price=120.0, lot_size=1200)
        target.bids["RETAIL"] = CategoryBid(
            "RETAIL", shares_offered=723_240, shares_bid=226_288_800,
            applications=94_287, offer_derived=True)
        target.bids["TOTAL"] = CategoryBid("TOTAL", times=231.18)

        source = IPO(name="x")
        source.bids["TOTAL"] = CategoryBid("TOTAL", times=231.18)
        source.bids["RETAIL"] = CategoryBid("RETAIL", times=235.72)

        _reconcile_subscription(target, source)
        retail = target.bids["RETAIL"]
        self.assertAlmostEqual(retail.shares_offered, 226_288_800 / 235.72, places=2)
        self.assertFalse(retail.offer_derived)
        self.assertEqual(retail.applications, 94_287)   # never discarded

    def test_exchange_reported_ratio_is_not_overwritten(self):
        target = make_ipo()
        target.bids["QIB"] = CategoryBid("QIB", shares_offered=1000,
                                         shares_bid=5000, offer_derived=False)
        source = IPO(name="x")
        source.bids["QIB"] = CategoryBid("QIB", times=99.0)
        _reconcile_subscription(target, source)
        self.assertEqual(target.bids["QIB"].subscription(), 5.0)


class TestEndToEndScoring(unittest.TestCase):
    def test_expected_value_is_odds_times_gain_on_capital(self):
        ipo = make_ipo(price=120.0, lot_size=1200, gmp_pct=22.5, segment="SME")
        ipo.bids["TOTAL"] = CategoryBid("TOTAL", times=231.18)
        ipo.bids["RETAIL"] = CategoryBid("RETAIL", shares_offered=959_990,
                                         shares_bid=226_288_800, applications=94_287)
        score(ipo, CFG)

        capital = 1200 * 120.0
        self.assertEqual(ipo.capital_per_application, capital)
        odds = ipo.allotment_prob["RETAIL"]
        self.assertAlmostEqual(
            ipo.expected_value_inr,
            odds * capital * ipo.expected_gain_pct / 100.0, places=4)

    def test_huge_subscription_still_yields_tiny_expected_value(self):
        """The headline insight: 231x and +22% is worth a few hundred rupees."""
        ipo = make_ipo(price=120.0, lot_size=1200, gmp_pct=22.5, segment="SME")
        ipo.bids["TOTAL"] = CategoryBid("TOTAL", times=231.18)
        ipo.bids["RETAIL"] = CategoryBid("RETAIL", shares_offered=959_990,
                                         shares_bid=226_288_800, applications=94_287)
        score(ipo, CFG)
        self.assertGreater(ipo.expected_gain_pct, 15)
        self.assertLess(ipo.expected_value_inr, 500)

    def test_verdicts_ladder_with_expected_gain(self):
        def verdict_for(gmp_pct, total):
            ipo = make_ipo(gmp_pct=gmp_pct)
            ipo.bids["TOTAL"] = CategoryBid("TOTAL", times=total)
            return score(ipo, CFG).verdict

        self.assertEqual(verdict_for(0.5, 2.0), "AVOID")
        self.assertEqual(verdict_for(8.0, 15.0), "WATCH")
        self.assertIn(verdict_for(60.0, 60.0), ("STRONG", "ACTIONABLE"))

    def test_a_strong_book_alone_can_clear_the_bar(self):
        """The point of the rebalance: subscription is now a real predictor.

        Under the old tier scheme a 60x book with no GMP quote contributed
        about +2pp and could never reach the bar on its own.
        """
        ipo = make_ipo()
        ipo.bids["TOTAL"] = CategoryBid("TOTAL", times=60.0)
        scored = score(ipo, CFG)
        self.assertGreater(scored.expected_gain_pct,
                           CFG["triggers"]["min_expected_listing_gain_pct"])

    def test_undersubscribed_is_always_avoid(self):
        ipo = make_ipo(gmp_pct=50.0)
        ipo.bids["TOTAL"] = CategoryBid("TOTAL", times=0.8)
        self.assertEqual(score(ipo, CFG).verdict, "AVOID")

    def test_confidence_drops_without_gmp(self):
        with_gmp = score(make_ipo(gmp_pct=10.0), CFG)
        without = make_ipo()
        without.bids["TOTAL"] = CategoryBid("TOTAL", times=10.0)
        self.assertGreater(with_gmp.confidence, score(without, CFG).confidence)


class TestAlertTriggers(unittest.TestCase):
    """Alert selection and de-duplication, against a throwaway database."""

    def setUp(self):
        import tempfile
        from datetime import timedelta

        from ipo_engine.store import Store
        from ipo_engine.util import today_ist

        self._dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._dir.name) / "t.db")

        self.ipo = make_ipo(name="Hot Issue Limited", symbol="HOT",
                            price=100.0, lot_size=150, gmp_pct=28.0,
                            open_date=today_ist() - timedelta(days=2),
                            close_date=today_ist() + timedelta(days=1))
        self.ipo.bids["TOTAL"] = CategoryBid("TOTAL", times=62.0)
        self.ipo.bids["QIB"] = CategoryBid("QIB", times=110.0)
        score(self.ipo, CFG)

    def tearDown(self):
        self.store.close()
        self._dir.cleanup()

    def _kinds(self):
        from ipo_engine import alerts as alerts_mod
        return alerts_mod.build([self.ipo], CFG, self.store)

    def test_only_the_highest_crossed_level_fires(self):
        """First sighting at 62x must not announce 1x, 5x, 20x and 50x too."""
        over = [a for a in self._kinds() if a.kind == "oversubscribed"]
        self.assertEqual(len(over), 1)
        self.assertEqual(over[0].level, 50.0)

    def test_superseded_levels_stay_quiet_afterwards(self):
        from ipo_engine import alerts as alerts_mod
        from ipo_engine.notify import Notifier

        quiet = dict(CFG)
        quiet["notify"] = {"windows_toast": False, "console": False}
        alerts_mod.dispatch(self._kinds(), Notifier(quiet), self.store)
        self.assertEqual(self._kinds(), [])

    def test_next_milestone_still_fires_later(self):
        from ipo_engine import alerts as alerts_mod
        from ipo_engine.notify import Notifier

        quiet = dict(CFG)
        quiet["notify"] = {"windows_toast": False, "console": False}
        alerts_mod.dispatch(self._kinds(), Notifier(quiet), self.store)

        self.ipo.bids["TOTAL"] = CategoryBid("TOTAL", times=140.0)
        score(self.ipo, CFG)
        over = [a for a in self._kinds() if a.kind == "oversubscribed"]
        self.assertEqual([a.level for a in over], [100.0])

    def test_closed_issues_raise_nothing(self):
        from datetime import timedelta

        from ipo_engine.util import today_ist
        self.ipo.close_date = today_ist() - timedelta(days=3)
        self.assertEqual(self._kinds(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
