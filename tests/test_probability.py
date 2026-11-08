"""Nearest-neighbour odds: any goal, honest spread, no refitting."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ipo_engine.models import IPO, CategoryBid
from ipo_engine.probability import (Predictor, attach, band_for, calibration)
from ipo_engine.scoring import issue_score
from ipo_engine.util import load_config

CFG = load_config()


def book(total, qib=None, nii=None, segment="MAINBOARD"):
    ipo = IPO(name="Test Ltd", segment=segment, price=100.0, lot_size=100)
    ipo.bids["TOTAL"] = CategoryBid("TOTAL", times=total)
    if qib is not None:
        ipo.bids["QIB"] = CategoryBid("QIB", times=qib)
    if nii is not None:
        ipo.bids["NII"] = CategoryBid("NII", times=nii)
    return ipo


def history(pairs):
    """(score, realised gain) rows."""
    return Predictor(CFG, list(pairs))


class TestBands(unittest.TestCase):
    def test_bands_describe_the_odds(self):
        self.assertEqual(band_for(0.90), "HIGH")
        self.assertEqual(band_for(0.70), "HIGH")
        self.assertEqual(band_for(0.55), "MODERATE")
        self.assertEqual(band_for(0.20), "LOW")
        self.assertEqual(band_for(0.05), "VERY LOW")

    def test_absent_odds_are_unknown_not_zero(self):
        self.assertEqual(band_for(None), "UNKNOWN")


class TestScore(unittest.TestCase):
    def test_score_excludes_gmp_so_history_is_matchable(self):
        """No historical GMP exists, so it cannot be part of the score."""
        quiet = book(50.0)
        hyped = book(50.0)
        hyped.gmp_pct = 80.0
        self.assertEqual(issue_score(quiet, CFG), issue_score(hyped, CFG))

    def test_score_rises_with_the_book(self):
        self.assertGreater(issue_score(book(100.0), CFG),
                           issue_score(book(10.0), CFG))

    def test_score_falls_when_qib_lags(self):
        self.assertGreater(issue_score(book(60.0, qib=70.0), CFG),
                           issue_score(book(60.0, qib=5.0), CFG))

    def test_no_book_means_no_score(self):
        self.assertIsNone(issue_score(IPO(name="X"), CFG))


class TestPredictor(unittest.TestCase):
    def test_too_little_history_quotes_nothing(self):
        predictor = history([(float(i), 5.0) for i in range(10)])
        self.assertFalse(predictor.ready)
        self.assertIsNone(predictor.predict(book(50.0)))

    def test_probability_is_the_share_of_neighbours_that_cleared(self):
        # 30 issues near score 20, of which 22 cleared; plus distant filler
        # so the predictor has enough history to be ready.
        near = [(20.0, 30.0)] * 22 + [(20.0, 1.0)] * 8
        far = [(500.0, -5.0)] * 50
        prediction = history(near + far).predict_score(20.0, goal_pct=10.0)
        self.assertEqual(prediction.sample, 30)
        self.assertAlmostEqual(prediction.probability, 22 / 30, places=4)

    def test_tied_scores_do_not_resolve_toward_bad_outcomes(self):
        """Ties are real - the curve caps, so every very large book ties.

        Ordering history by (score, gain) made the tie-break the outcome
        itself, so every tie selected the worst neighbours.
        """
        rows = [(20.0, 40.0)] * 30 + [(20.0, -5.0)] * 30
        probability = history(rows).predict_score(20.0, goal_pct=10.0).probability
        self.assertGreater(probability, 0.0,
                           "tie-break is still selecting the worst outcomes")

    def test_any_goal_is_answerable_without_refitting(self):
        rows = [(10.0, float(g)) for g in range(0, 60)]
        predictor = history(rows)
        low = predictor.predict_score(10.0, goal_pct=5.0)
        high = predictor.predict_score(10.0, goal_pct=40.0)
        self.assertGreater(low.probability, high.probability)
        self.assertEqual(low.sample, high.sample)      # same neighbours

    def test_neighbours_are_the_nearest_by_score(self):
        rows = ([(0.0, -5.0)] * 40) + ([(100.0, 50.0)] * 40)
        predictor = history(rows)
        self.assertAlmostEqual(
            predictor.predict_score(99.0, goal_pct=10.0).probability, 1.0)
        self.assertAlmostEqual(
            predictor.predict_score(1.0, goal_pct=10.0).probability, 0.0)

    def test_percentiles_describe_the_spread(self):
        rows = [(10.0, float(g)) for g in range(0, 60)]
        prediction = history(rows).predict_score(10.0, goal_pct=10.0)
        self.assertLess(prediction.p25, prediction.median)
        self.assertLess(prediction.median, prediction.p75)

    def test_sample_never_exceeds_the_neighbour_count(self):
        rows = [(10.0, 20.0)] * 200
        prediction = history(rows).predict_score(10.0)
        self.assertEqual(prediction.sample, CFG["probability"]["neighbours"])

    def test_description_is_readable_without_coefficients(self):
        near = [(20.0, 30.0)] * 24 + [(20.0, 1.0)] * 6
        text = history(near + [(500.0, -5.0)] * 50).predict_score(
            20.0, goal_pct=10.0).describe()
        self.assertIn("80%", text)
        self.assertIn("most similar past issues", text)


class TestAttach(unittest.TestCase):
    def test_attaches_odds_and_replaces_the_verdict(self):
        rows = [(20.0, 30.0)] * 50 + [(20.0, 1.0)] * 10
        predictor = history(rows)
        ipo = book(32.0)
        ipo.verdict = "ACTIONABLE"
        attached = attach([ipo], predictor, CFG)
        self.assertEqual(attached, 1)
        self.assertIsNotNone(ipo.clear_probability)
        self.assertIn(ipo.verdict, ("HIGH", "MODERATE", "LOW", "VERY LOW"))

    def test_issues_without_a_book_keep_their_legacy_verdict(self):
        rows = [(20.0, 30.0)] * 60
        ipo = IPO(name="No book")
        ipo.verdict = "NO DATA"
        self.assertEqual(attach([ipo], history(rows), CFG), 0)
        self.assertEqual(ipo.verdict, "NO DATA")


class TestCalibration(unittest.TestCase):
    def test_a_perfectly_separable_history_is_well_calibrated(self):
        train = ([(0.0, -5.0)] * 40) + ([(100.0, 50.0)] * 40)
        holdout = ([(0.0, -5.0)] * 20) + ([(100.0, 50.0)] * 20)
        for bucket in calibration(history(train), holdout, 10.0):
            self.assertAlmostEqual(bucket["predicted"], bucket["actual"],
                                   delta=0.05)

    def test_empty_holdout_reports_nothing(self):
        self.assertEqual(calibration(history([(1.0, 1.0)] * 60), [], 10.0), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
