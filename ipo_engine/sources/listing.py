"""Realised listing prices - the only way to know if any of this works.

Neither exchange will give these up: NSE's quote API answers 403 and its
historical archives 503, and BSE is behind Akamai entirely. Yahoo's chart
endpoint carries NSE-listed symbols as ``SYMBOL.NS`` and BSE as ``SYMBOL.BO``,
needs no key, and returns daily bars - enough to read the listing-day open.

Open, not close
---------------
Listing gain is measured against the **opening** print. That is the number
grey-market premium is a quote on, and the number someone selling into the
listing actually receives. The close is recorded too, because the gap between
them is itself informative (SFC Environmental opened +7.6% and closed +29.1%),
but calibration targets the open.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# events=split is essential, not optional: the provider back-adjusts historical
# OHLC for later splits, so an issue that split 10:1 after listing shows its
# listing-day open at a tenth of the traded price. Left uncorrected, Waterways
# Leisure read as -91.6% when it actually listed at -15.7%.
CHART_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
             "?period1={start}&period2={end}&interval=1d&events=split")

# A listing-day open this far below the issue price is not a market outcome,
# it is an unreported corporate action. Such rows are dropped rather than
# allowed to distort calibration.
MIN_PLAUSIBLE_GAIN_PCT = -75.0

# NSE first: mainboard issues resolve there. BSE catches issues that listed
# only on BSE.
SUFFIXES = (".NS", ".BO")

_WINDOW_BEFORE = dt.timedelta(days=4)
# The window has to run to the present, not just past the listing: a split is
# only reported if it falls inside the requested range, and the split that
# distorts a listing price usually happens months later (Waterways Leisure
# split 55 days after listing). Only the first bar is read either way.
_MIN_WINDOW_AFTER = dt.timedelta(days=10)


@dataclass
class ListingOutcome:
    symbol: str
    traded_on: dt.date
    open_price: float
    close_price: float | None
    source: str

    def gain_pct(self, issue_price: float | None) -> float | None:
        if not issue_price or issue_price <= 0:
            return None
        return (self.open_price - issue_price) / issue_price * 100.0

    def close_gain_pct(self, issue_price: float | None) -> float | None:
        if not issue_price or issue_price <= 0 or self.close_price is None:
            return None
        return (self.close_price - issue_price) / issue_price * 100.0


def _epoch(day: dt.date) -> int:
    return int(dt.datetime.combine(day, dt.time(), tzinfo=dt.UTC).timestamp())


def split_factor_after(payload: dict, day: dt.date) -> float:
    """Cumulative split ratio applied strictly after ``day``.

    Multiplying a back-adjusted price by this recovers what actually traded.
    """
    results = (payload.get("chart") or {}).get("result")
    if not results:
        return 1.0
    splits = ((results[0].get("events") or {}).get("splits") or {})

    factor = 1.0
    for event in splits.values():
        stamp = event.get("date")
        if stamp is None:
            continue
        split_day = dt.datetime.fromtimestamp(int(stamp), dt.UTC).date()
        if split_day <= day:
            continue                    # already reflected in the traded price
        numerator = float(event.get("numerator") or 0)
        denominator = float(event.get("denominator") or 0)
        if numerator > 0 and denominator > 0:
            factor *= numerator / denominator
    return factor


def _first_bar_from(payload: dict, listing_date: dt.date):
    """Earliest daily bar on or after the listing date, as actually traded."""
    results = (payload.get("chart") or {}).get("result")
    if not results:
        return None
    result = results[0]
    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    closes = quote.get("close") or []

    for i, stamp in enumerate(stamps):
        day = dt.datetime.fromtimestamp(stamp, dt.UTC).date()
        if day < listing_date or i >= len(opens):
            continue
        open_price = opens[i]
        if not open_price:
            continue
        close_price = closes[i] if i < len(closes) else None
        factor = split_factor_after(payload, day)
        return (day, float(open_price) * factor,
                (float(close_price) * factor if close_price else None))
    return None


def fetch_listing(fetcher, symbol: str, listing_date: dt.date) -> ListingOutcome | None:
    """Listing-day prices for one symbol, or None if nothing resolves.

    SME symbols frequently do not resolve at all - the provider's coverage of
    the NSE Emerge and BSE SME boards is patchy - so a miss is normal and
    logged at debug level rather than treated as a failure.
    """
    if not symbol or not listing_date:
        return None

    start = _epoch(listing_date - _WINDOW_BEFORE)
    end = _epoch(max(listing_date + _MIN_WINDOW_AFTER,
                     dt.datetime.now(dt.UTC).date() + dt.timedelta(days=1)))

    for suffix in SUFFIXES:
        ticker = f"{symbol.strip().upper()}{suffix}"
        payload = fetcher.get(CHART_URL.format(symbol=ticker, start=start, end=end),
                              as_json=True, referer="https://finance.yahoo.com/")
        if not isinstance(payload, dict):
            continue
        bar = _first_bar_from(payload, listing_date)
        if bar:
            traded_on, open_price, close_price = bar
            return ListingOutcome(symbol=ticker, traded_on=traded_on,
                                  open_price=open_price, close_price=close_price,
                                  source=f"yahoo{suffix}")

    log.debug("no listing price found for %s on %s", symbol, listing_date)
    return None
