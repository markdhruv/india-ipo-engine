"""Turn raw subscription and GMP data into a decision.

Three questions get answered separately, because conflating them is the
classic way retail investors lose money on IPOs:

  1. If allotted, how much is this likely to gain on listing?
     -> expected_gain_pct
  2. What are the odds of being allotted at all?
     -> allotment_prob, per category
  3. So what is one application actually worth in rupees?
     -> expected_value_inr = P(allot) x gain per application

A 250x-subscribed issue with a 23% premium sounds superb until you notice
94,000 retail applications chasing 600 lots: a 0.6% chance of allotment. The
engine always reports both halves.

Nothing here is investment advice, and GMP is an unofficial, unregulated
quote that can be thin or deliberately manipulated. See README.md.
"""
from __future__ import annotations

import math
from datetime import timedelta

from .models import IPO
from .util import today_ist

# Rupee thresholds that define the non-institutional sub-categories.
SNII_MIN_VALUE = 200_000      # sNII: Rs 2 lakh and above
BNII_MIN_VALUE = 1_000_000    # bNII: Rs 10 lakh and above

RETAIL_MAX_VALUE = 200_000    # a retail application cannot exceed Rs 2 lakh


# --------------------------------------------------------------------------
# 1. Expected listing gain
# --------------------------------------------------------------------------

def _gmp_component(ipo: IPO, cfg: dict) -> tuple[float | None, str | None]:
    """Shrink the grey-market premium toward reality.

    GMP systematically overstates the realised listing gain, and it overstates
    it *more* at the top end: a quoted 100% premium historically converts to
    far less than 100%. The shrink is therefore concave, not linear.
    """
    if ipo.gmp_pct is None:
        return None, None
    base = (cfg["gmp_shrink_sme"] if ipo.segment == "SME"
            else cfg["gmp_shrink_mainboard"])
    reversion = min(cfg["gmp_mean_reversion_cap"],
                    abs(ipo.gmp_pct) / cfg["gmp_mean_reversion_scale"])
    value = base * ipo.gmp_pct * (1.0 - reversion)
    note = (f"GMP {ipo.gmp_pct:+.1f}% shrunk to {value:+.1f}% "
            f"(x{base:.2f}, -{reversion*100:.0f}% mean reversion)")
    return value, note


# The old tier-bonus scheme lived here. It capped subscription's whole
# contribution at about 8 percentage points, while the measured spread
# between a 1-5x book and a 100x+ book is nearly 37. It is replaced by
# subscription_estimate(), a curve fitted to realised outcomes. Coverage
# below 1x in any major category is handled by the thin-leg rule in
# _composition_component, so nothing is lost by its removal.


# Categories big enough for their coverage to say anything. Employee and
# shareholder portions are small reservations that routinely go unfilled
# without meaning a thing.
MAJOR_CATEGORIES = ("QIB", "NII", "RETAIL")


def weakest_major_category(ipo: IPO) -> tuple[str | None, float | None]:
    """The least-covered category that actually carries signal."""
    covered = [(name, ipo.sub(name)) for name in MAJOR_CATEGORIES
               if ipo.sub(name) is not None]
    # A quota-derived QIB ratio is our own arithmetic, not a reported fact.
    covered = [(name, value) for name, value in covered
               if not (name == "QIB" and (ipo.bids.get("QIB")
                                          and ipo.bids["QIB"].offer_derived))]
    if not covered:
        return None, None
    return min(covered, key=lambda pair: pair[1])


def composition_thresholds(ipo: IPO, cfg: dict) -> dict:
    """Per-segment composition thresholds.

    The two books genuinely behave differently. Measured across live issues,
    mainboard NII/QIB runs roughly 4.5-7.7 while SME runs 0.8-3.6, because
    leveraged HNI funding is a deep and mature mainboard market. Applying one
    threshold to both would either tax every healthy mainboard issue or let
    an SME outlier through unflagged.
    """
    block = cfg.get("composition", {})
    return block.get("sme" if ipo.segment == "SME" else "mainboard", {})


def _composition_component(ipo: IPO, cfg: dict) -> tuple[float, list[str]]:
    """Judge *who* subscribed, not just how much.

    The headline total is the quota-weighted mean of the categories, so an
    issue can print a large multiple while the money that matters stays away.
    Three distinct failure shapes are worth separating:

      QIB lagging the book   institutions declined while retail and HNI money
                             carried the total. They are the price-sensitive,
                             diligence-doing participants, and their anchor
                             tranche is lock-in bound, so their absence
                             removes both the quality signal and the
                             restraint on listing-day supply.
      NII dwarfing QIB       large non-institutional books are substantially
                             funded by short-term NBFC borrowing, which has to
                             be repaid within days. That money is structurally
                             obliged to sell into the listing. Suppressed when
                             QIB is independently deep: alongside a strong
                             institutional book, heavy NII is enthusiasm
                             rather than a warning.

    Both were verified against 156 realised listings *within* subscription
    buckets, which matters: pooled across all issues the NII-leverage gap is
    0.0pp and the rule looks useless, because leveraged books are also large
    books and size is already priced by the curve. Held at constant book
    size the gap is -10 to -25pp. The penalties applied are much smaller
    than that, because the buckets where the effect is largest hold only a
    handful of issues each.
      A thin major category  a leg that failed to fill at all, which a healthy
                             headline can easily disguise.
    """
    notes: list[str] = []
    points = 0.0
    limits = composition_thresholds(ipo, cfg)

    total = ipo.sub("TOTAL")
    qib = ipo.sub("QIB")
    nii = ipo.sub("NII")

    qib_bid = ipo.bids.get("QIB")
    qib_reported = qib is not None and not (qib_bid and qib_bid.offer_derived)

    # 1. Institutions lagging a well-subscribed book.
    if (qib_reported and total is not None and limits
            and total >= limits["qib_lag_min_total"]
            and qib < total * limits["qib_lag_ratio"]):
        points += limits["qib_lag_penalty"]
        notes.append(f"QIB {qib:.1f}x lags the {total:.1f}x book - carried by "
                     f"retail/HNI money ({limits['qib_lag_penalty']:+.1f})")

    # 2. Leveraged HNI demand that must exit on listing day.
    if (qib_reported and nii is not None and qib > 0 and limits
            and nii >= limits["nii_leverage_min"]
            and nii >= qib * limits["nii_leverage_ratio"]
            and qib < limits["nii_leverage_qib_deep"]):
        points += limits["nii_leverage_penalty"]
        notes.append(f"NII {nii:.0f}x is {nii/qib:.1f}x a shallow {qib:.1f}x QIB "
                     f"book - funded applications sell into listing "
                     f"({limits['nii_leverage_penalty']:+.1f})")

    # 3. A major leg that never filled.
    name, value = weakest_major_category(ipo)
    if (name is not None and value < cfg["thin_category_threshold"]
            and (total is None or total >= 1.0)):
        points += cfg["thin_category_penalty"]
        notes.append(f"{name} undersubscribed at {value:.2f}x despite the "
                     f"headline ({cfg['thin_category_penalty']:+.1f})")

    return points, notes


def _risk_discount(ipo: IPO, cfg: dict) -> tuple[float, list[str]]:
    notes: list[str] = []
    discount = 0.0
    if ipo.segment == "SME":
        discount += cfg["sme_risk_discount"]
        notes.append(f"SME illiquidity risk (-{cfg['sme_risk_discount']:.1f})")
    if ipo.issue_size_cr and ipo.issue_size_cr >= cfg["large_issue_cr_threshold"]:
        discount += abs(cfg["large_issue_penalty"])
        notes.append(f"large issue {ipo.issue_size_cr:,.0f} Cr absorbs demand "
                     f"({cfg['large_issue_penalty']:+.1f})")
    return discount, notes


def subscription_estimate(ipo: IPO, cfg: dict) -> tuple[float | None, str | None]:
    """Expected listing gain implied by the size of the book alone.

    Measured on 156 issues that listed between May 2025 and Nov 2026: final
    total subscription has a rank correlation of +0.715 with the listing-day
    gain, and the relationship is cleanly monotonic. Below about 5x the
    median outcome is zero and roughly 45% list down; above 50x not one issue
    in the sample listed below its issue price.

    Interpolated in log space, because the buckets are multiplicative.
    """
    total = ipo.sub("TOTAL")
    if total is None or total <= 0:
        return None, None

    curve = cfg["scoring"]["subscription_curve"]
    points = sorted((float(x), float(y)) for x, y in curve["points"])
    cap = curve["max_extrapolated_gain"]

    if total <= points[0][0]:
        value = points[0][1]
    elif total >= points[-1][0]:
        value = min(cap, points[-1][1])
    else:
        value = points[-1][1]
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            if x0 <= total <= x1:
                span = math.log10(x1) - math.log10(x0)
                frac = (math.log10(total) - math.log10(x0)) / span if span else 0.0
                value = y0 + frac * (y1 - y0)
                break

    return value, f"book {total:,.1f}x implies {value:+.1f}%"


def book_maturity(ipo: IPO, cfg: dict) -> float:
    """How far the book has formed, 0-1.

    The curve is fitted on *final* subscription. Applying it to a day-one
    book - which is nearly empty, since Indian retail and HNI money arrives
    on the closing day - would drag every live estimate toward zero. So the
    subscription view earns its weight as the book fills.
    """
    blend = cfg["scoring"]["blend"]
    if not ipo.open_date or not ipo.close_date:
        return blend["maturity_final"]      # a closed or undated book is final

    today = today_ist()
    if today >= ipo.close_date:
        return blend["maturity_final"]
    if today >= ipo.close_date - timedelta(days=1):
        return blend["maturity_penultimate_day"]
    return blend["maturity_first_days"]


def issue_score(ipo: IPO, cfg: dict) -> float | None:
    """One number ranking an issue by the strength of its book.

    Deliberately excludes GMP. The score exists to find an issue's nearest
    historical matches, and no historical GMP exists to match against - the
    grey market is only observable live. Including it would make live issues
    unmatchable against the only outcomes we have.

    GMP still informs the point estimate in ``expected_gain``; it just has no
    say in which past issues this one resembles.
    """
    subscription, _note = subscription_estimate(ipo, cfg)
    if subscription is None:
        return None
    composition, _notes = _composition_component(ipo, cfg["scoring"])
    return subscription + composition * book_maturity(ipo, cfg)


def expected_gain(ipo: IPO, cfg: dict) -> tuple[float | None, list[str]]:
    """Estimated listing-day gain, in percent of issue price.

    GMP and subscription are two readings of the same underlying demand, so
    they are **averaged, not added**. Summing them would double-count: an
    issue with a hot grey market almost always has a hot book too, and
    adding both views produced estimates far above anything observed.
    """
    scoring = cfg["scoring"]
    blend = scoring["blend"]
    reasons: list[str] = []

    gmp_value, gmp_note = _gmp_component(ipo, scoring)
    if gmp_note:
        reasons.append(gmp_note)

    sub_value, sub_note = subscription_estimate(ipo, cfg)
    maturity = book_maturity(ipo, cfg)
    if sub_note:
        reasons.append(sub_note + ("" if maturity >= 1.0
                                   else f" (book {maturity*100:.0f}% formed)"))

    if gmp_value is None and sub_value is None:
        return None, reasons + ["no GMP and no subscription data"]

    if gmp_value is None:
        blended = sub_value
    elif sub_value is None:
        blended = gmp_value
    else:
        # An immature book is a weak reading, so weight shifts toward GMP.
        weight = blend["subscription_weight"] * maturity
        blended = weight * sub_value + (1.0 - weight) * gmp_value
        reasons.append(f"blend {weight*100:.0f}% book / "
                       f"{(1-weight)*100:.0f}% GMP")

    # Composition and risk are adjustments to that estimate, in percentage
    # points. Composition depends on the book, so it scales with maturity.
    composition, composition_notes = _composition_component(ipo, scoring)
    reasons.extend(composition_notes)

    discount, risk_notes = _risk_discount(ipo, scoring)
    reasons.extend(risk_notes)

    return blended + composition * maturity - discount, reasons


# --------------------------------------------------------------------------
# 2. Allotment probability
# --------------------------------------------------------------------------

def min_application_shares(ipo: IPO, category: str) -> int | None:
    """Smallest number of shares a valid application in this category buys.

    Retail applies for one lot. The NII sub-categories are defined by rupee
    value, so their minimum is however many whole lots clear the threshold.
    """
    lot = ipo.lot_size
    if not lot:
        return None
    if category in ("RETAIL", "EMPLOYEE", "SHAREHOLDER"):
        return lot
    if not ipo.price:
        return lot
    lot_value = lot * ipo.price
    if lot_value <= 0:
        return lot
    threshold = {"SNII": SNII_MIN_VALUE, "BNII": BNII_MIN_VALUE}.get(category)
    if threshold is None:
        return lot
    return lot * max(1, math.ceil(threshold / lot_value))


def allotment_probability(ipo: IPO, category: str) -> float | None:
    """Odds that one minimum-size application receives an allotment.

    When a category is oversubscribed, SEBI requires allotment of at least the
    minimum lot by a draw of lots, so the honest estimate is
    (lots available / applications received) - not 1/subscription, which
    silently assumes every applicant bid exactly one lot.
    """
    bid = ipo.bids.get(category)
    if bid is None:
        return None

    sub = bid.subscription()
    if sub is not None and sub <= 1.0:
        return 1.0                      # not oversubscribed: everyone gets filled

    min_app = min_application_shares(ipo, category)
    if bid.applications and bid.shares_offered and min_app:
        lots_available = bid.shares_offered / min_app
        if bid.applications > 0:
            return max(0.0, min(1.0, lots_available / bid.applications))

    if sub and sub > 0:                 # coarse fallback
        return min(1.0, 1.0 / sub)
    return None


# --------------------------------------------------------------------------
# 3. Capital and expected value
# --------------------------------------------------------------------------

def capital_per_application(ipo: IPO, category: str = "RETAIL") -> float | None:
    shares = min_application_shares(ipo, category)
    if shares and ipo.price:
        return shares * ipo.price
    return None


# --------------------------------------------------------------------------
# Confidence
# --------------------------------------------------------------------------

def confidence(ipo: IPO, cfg: dict) -> int:
    """How much to trust this row, 0-100, based purely on data quality."""
    score = 100
    scoring = cfg["scoring"]

    if ipo.gmp_pct is None:
        score -= scoring["no_gmp_confidence_penalty"]
    if ipo.total_subscription is None:
        score -= 25
    if not any(s.startswith("nse") for s in ipo.sources):
        score -= 15          # subscription not from the exchange itself
    if ipo.bids.get("RETAIL") is None or ipo.bids["RETAIL"].applications is None:
        score -= 10          # allotment odds are an approximation
    if not ipo.lot_size or not ipo.price:
        score -= 10
    if ipo.gmp_pct is not None and abs(ipo.gmp_pct) < 0.01:
        score -= 10          # a flat GMP is often just an absent quote
    return max(0, min(100, int(round(score))))


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------

def _verdict(ipo: IPO, cfg: dict) -> str:
    gate = cfg["triggers"]["min_expected_listing_gain_pct"]
    gain = ipo.expected_gain_pct
    total = ipo.total_subscription
    qib = ipo.sub("QIB")

    if gain is None:
        return "NO DATA"
    qib_bid = ipo.bids.get("QIB")
    qib_reliable = qib is not None and not (qib_bid and qib_bid.offer_derived)
    if (total is not None and total < 1.0) or (
            qib_reliable and qib < 1.0 and ipo.segment == "MAINBOARD"):
        return "AVOID"
    if gain >= gate * 2 and (total or 0) >= 10 and (ipo.confidence or 0) >= 60:
        return "STRONG"
    if gain >= gate:
        return "ACTIONABLE"
    if gain >= gate / 2:
        return "WATCH"
    return "AVOID"


def score(ipo: IPO, cfg: dict) -> IPO:
    """Fill every derived field on one IPO. Idempotent."""
    gain, reasons = expected_gain(ipo, cfg)
    ipo.expected_gain_pct = gain
    ipo.reasons = reasons
    ipo.confidence = confidence(ipo, cfg)
    ipo.weakest_category, ipo.weakest_category_sub = weakest_major_category(ipo)

    ipo.allotment_prob = {}
    for category in ("RETAIL", "SNII", "BNII", "QIB"):
        probability = allotment_probability(ipo, category)
        if probability is not None:
            ipo.allotment_prob[category] = probability

    ipo.capital_per_application = capital_per_application(ipo, "RETAIL")
    retail_odds = ipo.allotment_prob.get("RETAIL")
    if (gain is not None and ipo.capital_per_application and retail_odds is not None):
        gain_if_allotted = ipo.capital_per_application * gain / 100.0
        ipo.expected_value_inr = retail_odds * gain_if_allotted

    ipo.verdict = _verdict(ipo, cfg)
    return ipo


def score_all(ipos: list[IPO], cfg: dict) -> list[IPO]:
    return [score(ipo, cfg) for ipo in ipos]
