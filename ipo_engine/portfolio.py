"""Turn a cash budget and a PAN count into an application plan.

Two inputs decide everything:

  capital   how much can be blocked at once. This gates *reach* - SME lots run
            Rs 1.1-1.5 lakh against roughly Rs 15,000 on the mainboard, so a
            small budget cannot see the SME market at all.
  pans      how many separate PANs can genuinely fund an application. This
            gates *odds*, and it is by far the stronger lever: Rs 1 lakh across
            six PANs beats Rs 5 lakh on one, because allotment is a draw per
            application rather than per rupee.

The one rule that matters
-------------------------
**Never bid more than one lot.** When a retail category is oversubscribed,
SEBI allots the minimum lot by draw of lots, so bidding five lots gives
exactly the same chance as bidding one while blocking five times the capital.
Every application this planner produces is therefore one lot, and the way to
use more money is more PANs and more issues - never a bigger bid.

A PAN may apply to several different issues at once, but only once per issue
(the registrar rejects duplicates on a PAN). So ``pans`` caps applications
*per issue*, not in total.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import IPO
from .scoring import allotment_probability

# Reasons an otherwise-interesting issue cannot be applied for.
OVER_BUDGET = "one lot costs more than the whole budget"
BELOW_BAR = "estimated listing gain is under the bar"
NOT_OPEN = "not open for subscription"
NO_ODDS = "no allotment odds available"
NO_CAPITAL_LEFT = "budget exhausted by higher-value issues"


@dataclass
class Application:
    """One lot, on one PAN, in one issue - repeated across `pans` PANs."""
    ipo: IPO
    pans: int
    lot_value: float
    odds: float
    gain_pct: float

    @property
    def capital(self) -> float:
        return self.pans * self.lot_value

    @property
    def expected_allotments(self) -> float:
        return self.pans * self.odds

    @property
    def expected_inr(self) -> float:
        return self.expected_allotments * self.lot_value * self.gain_pct / 100.0

    @property
    def value_per_rupee(self) -> float:
        return self.odds * self.gain_pct / 100.0


@dataclass
class Skipped:
    ipo: IPO
    reason: str


@dataclass
class Plan:
    budget: float
    pans: int
    applications: list[Application] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    distribution: list[float] = field(default_factory=list)
    # True when built against closed issues because nothing is open. Indian
    # IPOs arrive in bursts, so an empty plan is the normal state most days -
    # and an empty panel makes the settings look broken. A preview shows what
    # they would do against the last batch. Never actionable.
    preview: bool = False

    @property
    def deployed(self) -> float:
        return sum(a.capital for a in self.applications)

    @property
    def idle(self) -> float:
        return self.budget - self.deployed

    @property
    def application_count(self) -> int:
        return sum(a.pans for a in self.applications)

    @property
    def expected_allotments(self) -> float:
        return sum(a.expected_allotments for a in self.applications)

    @property
    def expected_inr(self) -> float:
        return sum(a.expected_inr for a in self.applications)

    @property
    def p_none(self) -> float:
        """Probability of not being allotted anything at all."""
        return self.distribution[0] if self.distribution else 1.0

    @property
    def p_any(self) -> float:
        return 1.0 - self.p_none

    @property
    def return_on_budget_pct(self) -> float | None:
        return self.expected_inr / self.budget * 100.0 if self.budget else None

    @property
    def return_on_deployed_pct(self) -> float | None:
        deployed = self.deployed
        return self.expected_inr / deployed * 100.0 if deployed else None

    def skipped_tail(self) -> list[Skipped]:
        """Issues cleared the bar but were unreachable - worth seeing, not hiding."""
        return [s for s in self.skipped
                if s.reason in (OVER_BUDGET, NO_CAPITAL_LEFT)]


def allotment_distribution(odds: list[float]) -> list[float]:
    """Exact probability of each allotment count (Poisson binomial).

    Each application is an independent draw with its own probability, so the
    count is not binomial and a normal approximation would be poor at these
    sizes. The direct convolution is exact and trivially cheap here.
    """
    dp = [1.0]
    for q in odds:
        q = min(max(q, 0.0), 1.0)
        nxt = [0.0] * (len(dp) + 1)
        for k, p in enumerate(dp):
            nxt[k] += p * (1.0 - q)
            nxt[k + 1] += p * q
        dp = nxt
    return dp


def _candidate(ipo: IPO, cfg: dict, budget: float,
               allow_closed: bool = False) -> tuple[float, float, str | None]:
    """(lot value, retail odds, skip reason) for one issue."""
    gate = cfg["triggers"]["min_expected_listing_gain_pct"]
    min_confidence = cfg["triggers"].get("min_confidence", 0.60)

    if not ipo.is_open and not allow_closed:
        return 0.0, 0.0, NOT_OPEN

    # Eligibility is the confidence test where odds exist: a point estimate
    # just above the goal is not evidence the goal will be met.
    if ipo.clear_probability is not None:
        if ipo.clear_probability < min_confidence:
            return 0.0, 0.0, BELOW_BAR
    elif ipo.expected_gain_pct is None or ipo.expected_gain_pct < gate:
        return 0.0, 0.0, BELOW_BAR

    lot_value = ipo.capital_per_application
    if not lot_value:
        return 0.0, 0.0, NO_ODDS
    if lot_value > budget:
        return lot_value, 0.0, OVER_BUDGET

    odds = ipo.allotment_prob.get("RETAIL")
    if odds is None:
        odds = allotment_probability(ipo, "RETAIL")
    if odds is None:
        return lot_value, 0.0, NO_ODDS

    return lot_value, odds, None


def build_plan(ipos: list[IPO], cfg: dict, *, budget: float | None = None,
               pans: int | None = None, allow_closed: bool = False) -> Plan:
    """Greedy allocation of a budget across the issues that clear the bar.

    Ranked by expected return per rupee blocked, which is the only sane
    ordering once capital binds. Note the consequence: SME issues almost
    never win that comparison - 0.36% odds on a Rs 1.26 lakh lot loses to
    5.65% on Rs 15,000 however large the SME headline gain is - so they are
    reported under `skipped_tail` rather than silently dropped.
    """
    capital_cfg = cfg.get("capital", {})
    budget = float(budget if budget is not None else capital_cfg.get("budget_inr", 100_000))
    pans = int(pans if pans is not None else capital_cfg.get("pan_count", 1))
    pans = max(1, pans)

    plan = Plan(budget=budget, pans=pans)

    plan.preview = allow_closed

    ranked: list[tuple[float, IPO, float, float]] = []
    for ipo in ipos:
        lot_value, odds, reason = _candidate(ipo, cfg, budget, allow_closed)
        if reason:
            plan.skipped.append(Skipped(ipo, reason))
            continue
        ranked.append((odds * ipo.expected_gain_pct, ipo, lot_value, odds))

    ranked.sort(key=lambda row: row[0], reverse=True)

    remaining = budget
    for _value, ipo, lot_value, odds in ranked:
        affordable = int(remaining // lot_value)
        count = min(pans, affordable)
        if count <= 0:
            plan.skipped.append(Skipped(ipo, NO_CAPITAL_LEFT))
            continue
        plan.applications.append(Application(
            ipo=ipo, pans=count, lot_value=lot_value,
            odds=odds, gain_pct=ipo.expected_gain_pct))
        remaining -= count * lot_value

    odds_list: list[float] = []
    for application in plan.applications:
        odds_list.extend([application.odds] * application.pans)
    plan.distribution = allotment_distribution(odds_list)
    return plan
