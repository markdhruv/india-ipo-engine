"""The unified IPO record every source feeds into and the scorer reads from."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date


@dataclass
class CategoryBid:
    """One row of the exchange's bid table."""
    name: str                       # canonical: QIB / NII / BNII / SNII / RETAIL / EMPLOYEE / TOTAL
    raw_label: str = ""
    shares_offered: float | None = None
    shares_bid: float | None = None
    times: float | None = None      # subscription multiple
    applications: int | None = None
    # True when shares_offered was reconstructed from the regulatory quota
    # rather than reported by the exchange, so any ratio built on it is a
    # rough estimate and must not drive a penalty.
    offer_derived: bool = False

    def subscription(self) -> float | None:
        # NSE reports a literal "0.00" multiple in the projections that do not
        # carry it, so a zero here means "absent", not "nobody bid".
        if self.times and self.times > 0:
            return self.times
        if self.shares_offered and self.shares_bid and self.shares_offered > 0:
            return self.shares_bid / self.shares_offered
        return None


@dataclass
class IPO:
    """A single issue, merged across NSE + GMP sources."""
    name: str
    symbol: str | None = None
    segment: str = "MAINBOARD"          # MAINBOARD | SME
    exchange: str | None = None         # NSE / BSE / NSE SME / BSE SME
    industry: str | None = None

    open_date: date | None = None
    close_date: date | None = None
    listing_date: date | None = None

    price: float | None = None          # issue price / upper band
    lot_size: int | None = None
    issue_size_cr: float | None = None
    total_shares_offered: float | None = None

    gmp: float | None = None            # rupees
    gmp_pct: float | None = None        # over issue price
    gmp_source: str | None = None
    gmp_updated: str | None = None      # freshness of the grey-market quote
    boa_date: date | None = None        # basis of allotment
    has_anchor: bool | None = None      # anchor-investor book present

    bids: dict[str, CategoryBid] = field(default_factory=dict)
    subscription_updated: str | None = None
    sources: list[str] = field(default_factory=list)

    # --- filled in by scoring ---
    expected_gain_pct: float | None = None
    confidence: int | None = None
    allotment_prob: dict[str, float] = field(default_factory=dict)
    capital_per_application: float | None = None
    expected_value_inr: float | None = None
    verdict: str | None = None
    reasons: list[str] = field(default_factory=list)
    # Least-covered major category - the headline total can hide a weak leg.
    weakest_category: str | None = None
    weakest_category_sub: float | None = None
    # Resolved by Store.assign_identity so an issue keeps one identity even
    # as it moves between the exchange calendar and the GMP sites.
    canonical_key: str | None = None

    # Filled in from realised outcomes of similar past issues. This, not
    # expected_gain_pct, is the headline: the point estimate carries an error
    # larger than the median outcome it is estimating.
    # Whether retail allotment is a draw for one lot (applicants outnumber
    # lots) or proportionate, where one application can win several. The
    # "bid one lot only" rule holds in the first case and not the second.
    retail_lottery: bool | None = None
    expected_retail_lots: float | None = None

    clear_probability: float | None = None
    probability_sample: int | None = None
    outcome_median: float | None = None
    outcome_p25: float | None = None
    outcome_p75: float | None = None

    # ---------- convenience ----------
    def sub(self, category: str) -> float | None:
        bid = self.bids.get(category)
        return bid.subscription() if bid else None

    @property
    def total_subscription(self) -> float | None:
        return self.sub("TOTAL")

    @property
    def is_open(self) -> bool:
        from .util import today_ist
        today = today_ist()
        if not self.open_date or not self.close_date:
            return False
        return self.open_date <= today <= self.close_date

    @property
    def closes_today(self) -> bool:
        from .util import today_ist
        return self.close_date == today_ist()

    @property
    def natural_keys(self) -> list[str]:
        """Every identifier this issue could be recognised by.

        An issue carries an NSE symbol only while the exchange still lists it
        in the calendar. Before and after that window the GMP sites are the
        only source, and they know it by name alone - so identity has to be
        the union of both, resolved through the alias table in store.py.
        """
        from .util import norm_name
        keys = []
        if self.symbol:
            keys.append(self.symbol.strip().upper())
        name_key = norm_name(self.name)
        if name_key:
            keys.append(name_key)
        return keys

    @property
    def key(self) -> str:
        """Stable identity. Falls back to the natural key until resolved."""
        if self.canonical_key:
            return self.canonical_key
        keys = self.natural_keys
        return keys[0] if keys else ""

    def to_dict(self) -> dict:
        out = asdict(self)
        for field_name in ("open_date", "close_date", "listing_date", "boa_date"):
            if out[field_name] is not None:
                out[field_name] = out[field_name].isoformat()
        return out
