"""Fuse the NSE and GMP views of the same issue into one record.

NSE owns the subscription numbers and the official calendar; the GMP sites own
the grey-market premium, lot size, issue size and - importantly - BSE-SME
issues that never appear in NSE's API at all. Neither side alone is enough.
"""
from __future__ import annotations

import logging

from .models import IPO
from .util import norm_name

log = logging.getLogger(__name__)

_MIN_FUZZY_LEN = 8


def _index(ipos: list[IPO]) -> dict[str, IPO]:
    return {norm_name(ipo.name): ipo for ipo in ipos if ipo.name}


def _find_match(key: str, index: dict[str, IPO]) -> IPO | None:
    """Exact normalised match, then a conservative prefix match.

    'Maxwell Engineering' (ipowatch) must still reach 'Maxwell Engineering
    Solutions Limited' (NSE), but short keys are never fuzzy-matched - 'hgs'
    would collide with half the market.
    """
    if key in index:
        return index[key]
    if len(key) < _MIN_FUZZY_LEN:
        return None
    candidates = [other for other in index
                  if len(other) >= _MIN_FUZZY_LEN
                  and (other.startswith(key) or key.startswith(other))]
    if len(candidates) == 1:
        return index[candidates[0]]
    return None


# The total is the quota-weighted mean of the category multiples, so no single
# category can exceed total / its_quota. The smallest quota in play is ~15%,
# giving a hard ceiling near 6.7x the total; 8x leaves room for rounding and
# for reserved portions we do not model.
_MAX_CATEGORY_OVER_TOTAL = 8.0


def _coherent(category: str, value: float, total: float | None) -> bool:
    """Reject a published multiple that cannot coexist with the total.

    Observed in the wild: a 1.85x-subscribed issue listed with 'QIB 327x'.
    Propagating that would inflate both the score and the allotment odds.
    """
    if category == "TOTAL" or total is None or total <= 0:
        return True
    return value <= total * _MAX_CATEGORY_OVER_TOTAL


def _reconcile_subscription(target: IPO, source: IPO) -> None:
    """Prefer published multiples wherever our own ratio is quota-derived.

    The exchange withholds the per-category offer size for SME issues, so the
    engine reconstructs it from the regulatory quota - which measurement
    against the published report showed to be off by roughly a third. When a
    published multiple is available for such a category we adopt it *and* work
    backwards to the implied offer size, because the allotment-odds
    calculation divides by that figure and would inherit the same error.

    The exchange's application counts are never discarded: no other source
    publishes them, and they are what make allotment odds real rather than a
    1/subscription hand-wave.
    """
    published_total = source.sub("TOTAL") or target.total_subscription

    for category, published in source.bids.items():
        if published.times is None or published.times <= 0:
            continue
        if not _coherent(category, published.times, published_total):
            log.warning("%s: ignoring incoherent %s subscription %.2fx against "
                        "a %.2fx total", target.name, category,
                        published.times, published_total or 0.0)
            continue
        ours = target.bids.get(category)

        if ours is None:
            target.bids[category] = published
            continue

        # A ratio the exchange reported directly outranks a scraped one.
        if not ours.offer_derived and ours.subscription() is not None:
            continue

        ours.times = published.times
        if ours.shares_bid and published.times > 0:
            ours.shares_offered = ours.shares_bid / published.times
            ours.offer_derived = False      # now implied by a published ratio
        ours.raw_label = f"{ours.raw_label} [ratio: {published.raw_label}]".strip()


def _take_from_gmp(target: IPO, source: IPO) -> None:
    """GMP sites win on commercial terms; NSE wins on subscription and dates."""
    target.gmp = source.gmp if source.gmp is not None else target.gmp
    target.gmp_pct = source.gmp_pct if source.gmp_pct is not None else target.gmp_pct
    target.gmp_source = source.gmp_source or target.gmp_source
    target.gmp_updated = source.gmp_updated or target.gmp_updated
    target.lot_size = target.lot_size or source.lot_size
    target.issue_size_cr = target.issue_size_cr or source.issue_size_cr
    target.price = target.price or source.price
    target.exchange = target.exchange or source.exchange
    target.boa_date = target.boa_date or source.boa_date
    target.listing_date = target.listing_date or source.listing_date
    if target.has_anchor is None:
        target.has_anchor = source.has_anchor

    _reconcile_subscription(target, source)

    for name in source.sources:
        if name not in target.sources:
            target.sources.append(name)


def merge(nse_ipos: list[IPO], gmp_ipos: list[IPO]) -> list[IPO]:
    """Return one record per issue, NSE-anchored, GMP-enriched."""
    merged = list(nse_ipos)
    index = _index(merged)
    matched: set[int] = set()

    for gmp_ipo in gmp_ipos:
        target = _find_match(norm_name(gmp_ipo.name), index)
        if target is not None:
            _take_from_gmp(target, gmp_ipo)
            matched.add(id(gmp_ipo))

    # Whatever the GMP sites know about and NSE does not is almost always a
    # BSE-only SME issue. Keep it: those are frequently the biggest pops.
    for gmp_ipo in gmp_ipos:
        if id(gmp_ipo) not in matched:
            gmp_ipo.sources.append("gmp-only")
            merged.append(gmp_ipo)

    log.info("merged %d NSE + %d GMP rows into %d issues",
             len(nse_ipos), len(gmp_ipos), len(merged))
    return merged
