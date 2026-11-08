"""Allocation of a budget across PANs, and the resulting outcome spread."""
from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ipo_engine.models import IPO, CategoryBid
from ipo_engine.portfolio import (BELOW_BAR, NOT_OPEN, NO_CAPITAL_LEFT,
                                  OVER_BUDGET, allotment_distribution,
                                  build_plan)
from ipo_engine.util import load_config, today_ist

CFG = load_config()


def open_ipo(name, *, lot_value, odds, gain, segment="MAINBOARD", lot=100):
    """An issue open today, with the retail odds and gain already scored."""
    ipo = IPO(name=name, segment=segment, lot_size=lot,
              price=lot_value / lot,
              open_date=today_ist() - timedelta(days=1),
              close_date=today_ist() + timedelta(days=1))
    ipo.bids["TOTAL"] = CategoryBid("TOTAL", times=40.0)
    ipo.expected_gain_pct = gain
    ipo.allotment_prob = {"RETAIL": odds}
    ipo.capital_per_application = lot_value
    return ipo


class TestDistribution(unittest.TestCase):
    def test_probabilities_sum_to_one(self):
        dist = allotment_distribution([0.05, 0.2, 0.33, 0.9])
        self.assertAlmostEqual(sum(dist), 1.0, places=12)

    def test_matches_binomial_when_odds_are_equal(self):
        from math import comb
        n, q = 6, 0.05
        dist = allotment_distribution([q] * n)
        for k in range(n + 1):
            self.assertAlmostEqual(dist[k], comb(n, k) * q**k * (1 - q)**(n - k),
                                   places=12)

    def test_handles_mixed_odds_exactly(self):
        dist = allotment_distribution([0.5, 0.25])
        self.assertAlmostEqual(dist[0], 0.5 * 0.75)
        self.assertAlmostEqual(dist[1], 0.5 * 0.75 + 0.5 * 0.25)
        self.assertAlmostEqual(dist[2], 0.5 * 0.25)

    def test_no_applications_means_certain_nothing(self):
        self.assertEqual(allotment_distribution([]), [1.0])


class TestAllocation(unittest.TestCase):
    def test_one_lot_per_pan_never_more(self):
        """Extra lots do not improve odds, so the planner must never stack them."""
        ipo = open_ipo("Cheap Ltd", lot_value=15_000, odds=0.05, gain=20.0)
        plan = build_plan([ipo], CFG, budget=1_000_000, pans=3)
        self.assertEqual(len(plan.applications), 1)
        self.assertEqual(plan.applications[0].pans, 3)        # not 66 lots
        self.assertEqual(plan.deployed, 45_000)

    def test_pans_cap_applications_per_issue(self):
        ipo = open_ipo("Cheap Ltd", lot_value=15_000, odds=0.05, gain=20.0)
        plan = build_plan([ipo], CFG, budget=1_000_000, pans=1)
        self.assertEqual(plan.application_count, 1)

    def test_reports_capital_as_the_binding_limit(self):
        """4 PANs next to 2 applications reads as a bug without this.

        An SME lot at Rs 1.44 L against a Rs 3 L budget buys two, however
        many PANs are available.
        """
        sme = open_ipo("Big SME", lot_value=144_000, odds=0.0085, gain=21.9,
                       segment="SME")
        plan = build_plan([sme], CFG, budget=300_000, pans=4)
        application = plan.applications[0]
        self.assertEqual(application.pans, 2)
        self.assertEqual(application.limited_by, "capital")
        self.assertEqual(application.pans_available, 4)
        self.assertIn("2 of 4 PANs", application.limit_note)

    def test_reports_pans_as_the_binding_limit(self):
        cheap = open_ipo("Cheap Ltd", lot_value=15_000, odds=0.05, gain=20.0)
        plan = build_plan([cheap], CFG, budget=1_000_000, pans=3)
        application = plan.applications[0]
        self.assertEqual(application.limited_by, "pans")
        self.assertIn("all 3 PANs", application.limit_note)

    def test_budget_caps_applications(self):
        ipo = open_ipo("Cheap Ltd", lot_value=15_000, odds=0.05, gain=20.0)
        plan = build_plan([ipo], CFG, budget=40_000, pans=6)
        self.assertEqual(plan.application_count, 2)           # floor(40000/15000)
        self.assertEqual(plan.deployed, 30_000)
        self.assertEqual(plan.idle, 10_000)

    def test_a_pan_may_apply_to_several_different_issues(self):
        issues = [open_ipo(f"Co {i}", lot_value=15_000, odds=0.05, gain=20.0)
                  for i in range(3)]
        plan = build_plan(issues, CFG, budget=1_000_000, pans=2)
        self.assertEqual(len(plan.applications), 3)
        self.assertEqual(plan.application_count, 6)           # 2 PANs x 3 issues

    def test_unaffordable_issue_is_surfaced_not_hidden(self):
        sme = open_ipo("Big SME", lot_value=126_000, odds=0.0036, gain=45.8,
                       segment="SME")
        plan = build_plan([sme], CFG, budget=100_000, pans=6)
        self.assertEqual(plan.applications, [])
        self.assertEqual([s.reason for s in plan.skipped], [OVER_BUDGET])
        self.assertEqual(len(plan.skipped_tail()), 1)

    def test_issue_below_the_bar_is_excluded(self):
        weak = open_ipo("Weak Ltd", lot_value=15_000, odds=0.5, gain=4.0)
        plan = build_plan([weak], CFG, budget=100_000, pans=2)
        self.assertEqual([s.reason for s in plan.skipped], [BELOW_BAR])

    def test_closed_issue_is_excluded(self):
        ipo = open_ipo("Closed Ltd", lot_value=15_000, odds=0.05, gain=20.0)
        ipo.close_date = today_ist() - timedelta(days=2)
        plan = build_plan([ipo], CFG, budget=100_000, pans=2)
        self.assertEqual([s.reason for s in plan.skipped], [NOT_OPEN])

    def test_ranked_by_return_per_rupee_not_headline_gain(self):
        """A huge SME gain loses to a cheap mainboard lot with real odds."""
        flashy = open_ipo("Flashy SME", lot_value=90_000, odds=0.004, gain=45.0,
                          segment="SME")
        boring = open_ipo("Boring Main", lot_value=15_000, odds=0.056, gain=11.0)
        plan = build_plan([flashy, boring], CFG, budget=100_000, pans=1)
        self.assertEqual(plan.applications[0].ipo.name, "Boring Main")

    def test_exhausted_budget_is_reported_as_such(self):
        rich = open_ipo("First", lot_value=60_000, odds=0.20, gain=30.0)
        poor = open_ipo("Second", lot_value=60_000, odds=0.10, gain=12.0)
        plan = build_plan([rich, poor], CFG, budget=100_000, pans=1)
        self.assertEqual(len(plan.applications), 1)
        self.assertEqual([s.reason for s in plan.skipped], [NO_CAPITAL_LEFT])


class TestOutcomeMaths(unittest.TestCase):
    def test_expected_value_matches_hand_calculation(self):
        ipo = open_ipo("Co", lot_value=15_000, odds=0.05, gain=20.0)
        plan = build_plan([ipo], CFG, budget=100_000, pans=4)
        self.assertAlmostEqual(plan.expected_allotments, 4 * 0.05)
        self.assertAlmostEqual(plan.expected_inr, 4 * 0.05 * 15_000 * 0.20)

    def test_probability_of_nothing_is_the_honest_headline(self):
        ipo = open_ipo("Co", lot_value=15_000, odds=0.05, gain=20.0)
        plan = build_plan([ipo], CFG, budget=100_000, pans=6)
        self.assertAlmostEqual(plan.p_none, 0.95 ** 6, places=10)
        self.assertGreater(plan.p_none, 0.5)      # still most likely to get nothing

    @staticmethod
    def _spread(budget, pans, issue_count):
        issues = [open_ipo(f"Co {i}", lot_value=15_000, odds=0.05, gain=20.0)
                  for i in range(issue_count)]
        return build_plan(issues, CFG, budget=budget, pans=pans)

    def test_pans_beat_capital_when_deal_flow_is_scarce(self):
        """The normal case: only two or three issues are open at once.

        A PAN can apply once per issue, so with few issues on offer extra
        money buys nothing while extra PANs keep multiplying applications.
        Rs 1 L over 6 PANs gets 12 tickets; Rs 5 L on one PAN gets 2.
        """
        many_pans = self._spread(100_000, 6, issue_count=2)
        much_cash = self._spread(500_000, 1, issue_count=2)
        self.assertEqual(much_cash.application_count, 2)
        self.assertGreater(many_pans.application_count, much_cash.application_count)
        self.assertGreater(many_pans.p_any, much_cash.p_any)

    def test_capital_binds_instead_when_deal_flow_is_abundant(self):
        """The asymmetry is not universal - with enough issues, money binds.

        Worth pinning so the 'PANs always win' claim is not over-generalised.
        """
        many_pans = self._spread(100_000, 6, issue_count=20)
        much_cash = self._spread(500_000, 1, issue_count=20)
        self.assertEqual(many_pans.application_count, 6)      # 100k / 15k
        self.assertEqual(much_cash.application_count, 20)     # one per issue
        self.assertGreater(much_cash.p_any, many_pans.p_any)

    def test_return_on_budget_accounts_for_idle_cash(self):
        ipo = open_ipo("Co", lot_value=15_000, odds=0.05, gain=20.0)
        plan = build_plan([ipo], CFG, budget=100_000, pans=1)
        self.assertLess(plan.return_on_budget_pct, plan.return_on_deployed_pct)

    def test_empty_plan_is_safe(self):
        plan = build_plan([], CFG, budget=100_000, pans=4)
        self.assertEqual(plan.p_none, 1.0)
        self.assertEqual(plan.expected_inr, 0.0)
        self.assertEqual(plan.idle, 100_000)


class TestIdentity(unittest.TestCase):
    """An issue must keep one identity across sources (the BCSSL split)."""

    def setUp(self):
        import tempfile

        from ipo_engine.store import Store
        self._dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._dir.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self._dir.cleanup()

    def test_symbol_and_name_resolve_to_one_identity(self):
        with_symbol = IPO(name="Bombay Coated and Special Steels Limited",
                          symbol="BCSSL")
        gmp_only = IPO(name="Bombay Coated and Special Steels")

        first = self.store.assign_identity(with_symbol)
        second = self.store.assign_identity(gmp_only)
        self.assertEqual(first, second)

    def test_identity_survives_losing_the_symbol(self):
        """NSE drops an issue after listing; the GMP row must not become new."""
        self.store.assign_identity(IPO(name="Acme Industries Limited", symbol="ACME"))
        later = IPO(name="Acme Industries")
        self.assertEqual(self.store.assign_identity(later), "ACME")

    def test_identity_survives_gaining_a_symbol(self):
        gmp_first = IPO(name="Acme Industries")
        original = self.store.assign_identity(gmp_first)
        with_symbol = IPO(name="Acme Industries Limited", symbol="ACME")
        self.assertEqual(self.store.assign_identity(with_symbol), original)

    def test_unrelated_issues_stay_separate(self):
        a = self.store.assign_identity(IPO(name="Alpha Textiles Limited", symbol="ALPHA"))
        b = self.store.assign_identity(IPO(name="Beta Chemicals Limited", symbol="BETA"))
        self.assertNotEqual(a, b)

    def test_alerts_are_not_refired_after_an_identity_change(self):
        first = IPO(name="Acme Industries Limited", symbol="ACME")
        self.store.assign_identity(first)
        self.store.record_alert(first.key, "oversubscribed", 20.0, "t", "b")

        later = IPO(name="Acme Industries")       # same issue, no symbol
        self.store.assign_identity(later)
        self.assertTrue(self.store.already_alerted(later.key, "oversubscribed", 20.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
