"""Capture realised listing gains, and read the market's temperature from them.

This closes the loop the engine was missing. Two things come out of it:

  validation   predictions can finally be checked against what happened.
  regime       how recent IPOs have actually listed is the cheapest strong
               predictor available for the next one, and it is the honest
               answer to "what is sentiment doing right now".

What can and cannot be rebuilt
------------------------------
Listing prices are fetchable, and so is the final subscription book - NSE
keeps it after listing, contrary to an earlier assumption in this codebase.
Those two together make real historical calibration of the subscription side
possible over hundreds of past issues.

Grey-market premium is the exception: nobody publishes what it was quoted at
on a given closing day, so the GMP shrink can only be fitted from pairs this
engine records going forward.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from .models import IPO
from .sources import listing as listing_source
from .util import today_ist

log = logging.getLogger(__name__)

# An issue needs to have actually traded before its outcome means anything.
_SETTLED_AFTER_DAYS = 1


@dataclass
class Regime:
    """What recent listings have been doing."""
    sample: int
    median_gain: float | None
    mean_gain: float | None
    hit_rate: float | None          # share clearing the configured bar
    negative_rate: float | None     # share that listed below issue price
    label: str

    def describe(self) -> str:
        if not self.sample:
            return "no listing history yet"
        return (f"{self.label}: last {self.sample} listings, median "
                f"{self.median_gain:+.1f}%, {self.hit_rate*100:.0f}% cleared the bar, "
                f"{self.negative_rate*100:.0f}% listed below issue")


def backfill(fetcher, store, calendar: list[IPO], *, limit: int = 60,
             skip_existing: bool = True) -> dict:
    """Fetch listing prices for issues that have already listed.

    Walks the calendar newest first, because recent listings carry the regime
    signal and older ones only add base rate.
    """
    cutoff = today_ist() - timedelta(days=_SETTLED_AFTER_DAYS)
    candidates = [i for i in calendar
                  if i.listing_date and i.listing_date <= cutoff
                  and i.price and i.symbol]
    candidates.sort(key=lambda i: i.listing_date, reverse=True)

    stats = {"tried": 0, "found": 0, "missed": 0, "skipped": 0,
             "by_segment": {}}

    for ipo in candidates:
        if stats["tried"] >= limit:
            break
        key = store.assign_identity(ipo)
        if skip_existing and store.has_outcome(key):
            stats["skipped"] += 1
            continue

        stats["tried"] += 1
        bucket = stats["by_segment"].setdefault(ipo.segment, {"found": 0, "missed": 0})

        result = listing_source.fetch_listing(fetcher, ipo.symbol, ipo.listing_date)
        if result is None:
            stats["missed"] += 1
            bucket["missed"] += 1
            continue

        # Guard against corporate actions the provider did not report as
        # splits. Exchange circuit limits make a day-one collapse of this
        # size impossible, so such a row is bad data, not a bad IPO.
        gain = result.gain_pct(ipo.price)
        if gain is not None and gain < listing_source.MIN_PLAUSIBLE_GAIN_PCT:
            log.warning("discarding implausible listing gain for %s: "
                        "issue %.2f, open %.2f (%.1f%%)",
                        ipo.symbol, ipo.price, result.open_price, gain)
            stats["implausible"] = stats.get("implausible", 0) + 1
            continue

        store.record_outcome(
            key, ipo.name, ipo.price, result.open_price,
            listing_close=result.close_price, segment=ipo.segment,
            listing_date=ipo.listing_date, source=result.source)
        stats["found"] += 1
        bucket["found"] += 1

    return stats


def backfill_books(fetcher, store, calendar: list[IPO], *, limit: int = 200,
                   quotas: dict | None = None) -> dict:
    """Re-fetch the final subscription book for issues that already listed.

    This is only possible because NSE keeps the book after listing. Paired
    with the realised gain already on the row, it turns the scoring rules
    from reasoning into something measurable.
    """
    from .sources import nse

    by_key: dict[str, IPO] = {}
    for ipo in calendar:
        if ipo.symbol and ipo.listing_date:
            by_key[store.assign_identity(ipo)] = ipo

    stats = {"tried": 0, "found": 0, "empty": 0, "skipped": 0}
    for row in store.outcomes(limit=10_000):
        if stats["tried"] >= limit:
            break
        key = row["ipo_key"]
        if store.has_final_book(key):
            stats["skipped"] += 1
            continue
        ipo = by_key.get(key)
        if ipo is None:
            continue

        stats["tried"] += 1
        fresh = IPO(name=ipo.name, symbol=ipo.symbol, segment=ipo.segment,
                    price=ipo.price, lot_size=ipo.lot_size,
                    total_shares_offered=None)
        nse.enrich_with_bids(fetcher, fresh, quotas or {},
                             need_applications=False)
        subs = {name: fresh.sub(name)
                for name in ("TOTAL", "QIB", "NII", "RETAIL", "BNII", "SNII")}
        if not subs.get("TOTAL"):
            stats["empty"] += 1
            continue
        store.record_final_book(key, subs)
        stats["found"] += 1
    return stats


def regime(store, cfg: dict, *, lookback: int = 20,
           segment: str | None = None) -> Regime:
    """Read recent listing performance as a market-temperature signal."""
    import statistics

    bar = cfg["triggers"]["min_expected_listing_gain_pct"]
    rows = store.outcomes(limit=lookback, segment=segment)
    gains = [r["gain_open"] for r in rows if r["gain_open"] is not None]

    if not gains:
        return Regime(0, None, None, None, None, "UNKNOWN")

    median = statistics.median(gains)
    mean = statistics.fmean(gains)
    hit_rate = sum(1 for g in gains if g >= bar) / len(gains)
    negative_rate = sum(1 for g in gains if g < 0) / len(gains)

    # Thresholds describe the market, they do not predict it. A cold tape is
    # the cheapest reason to sit a month out.
    if median >= bar and hit_rate >= 0.5:
        label = "HOT"
    elif median <= 0 or negative_rate >= 0.5:
        label = "COLD"
    else:
        label = "NORMAL"

    return Regime(len(gains), median, mean, hit_rate, negative_rate, label)


def accuracy(store, cfg: dict) -> dict:
    """How close predictions have been to reality, where both are known."""
    import statistics

    rows = store.calibration_rows()
    pairs = [(r["expected_gain"], r["listing_gain"]) for r in rows
             if r["expected_gain"] is not None and r["listing_gain"] is not None]
    if not pairs:
        return {"sample": 0}

    errors = [abs(p - a) for p, a in pairs]
    bar = cfg["triggers"]["min_expected_listing_gain_pct"]
    flagged = [(p, a) for p, a in pairs if p >= bar]
    return {
        "sample": len(pairs),
        "mean_abs_error": statistics.fmean(errors),
        "median_abs_error": statistics.median(errors),
        "bias": statistics.fmean([p - a for p, a in pairs]),
        "flagged": len(flagged),
        "flagged_hits": sum(1 for _p, a in flagged if a >= bar),
    }
