"""NSE adapter - the authoritative source for subscription numbers.

Two endpoints carry everything we need:

  /api/public-past-issues?index=ipo   the full issue calendar (~1500 rows,
                                      mainboard + SME, past and in-flight)
  /api/ipo-detail?symbol=X&series=Y   live category-wise bid table

Bid data survives listing. An earlier version of this file claimed NSE wipes
``bidDetails`` once an issue lists - that was a wrong generalisation from one
issue (MKJEWELS) that happens to return an empty table. Measured across 18
recently listed issues, 17 still serve their full category-wise book, which
is what makes historical calibration possible at all. The engine still
snapshots every run, because intraday subscription progress is not retained.

The ``series`` parameter returns *different projections* of the same issue:
one view carries subscription multiples, the other carries application
counts. We request both and merge field-wise.
"""
from __future__ import annotations

import logging

from ..models import IPO, CategoryBid
from ..util import parse_date, to_float, to_int

log = logging.getLogger(__name__)

CALENDAR_URL = "https://www.nseindia.com/api/public-past-issues?index=ipo"
DETAIL_URL = "https://www.nseindia.com/api/ipo-detail?symbol={symbol}&series={series}"
CALENDAR_REFERER = "https://www.nseindia.com/market-data/all-upcoming-issues-ipo"
DETAIL_REFERER = "https://www.nseindia.com/market-data/issue-information"

# NSE's srNo is far more stable than its label text, which varies between
# mainboard and SME tables. Sub-rows ("1(a)", "2.1(b)") are breakdowns we skip.
SR_NO_MAP = {
    "1": "QIB",
    "2": "NII",
    "2.1": "BNII",    # bids above Rs 10 lakh
    "2.2": "SNII",    # bids Rs 2-10 lakh
    "3": "RETAIL",
    "4": "EMPLOYEE",
    "5": "SHAREHOLDER",
}

# NSE has changed this vocabulary mid-flight: the calendar reported "EQ" and
# "BE" for mainboard issues and then switched to "Equity" within a day, which
# silently hid the entire mainboard segment. Matching is case-insensitive and
# the non-equity types are listed explicitly so that anything unrecognised is
# reported rather than quietly dropped.
MAINBOARD_TYPES = {"EQ", "BE", "EQUITY"}
SME_TYPES = {"SME", "SM", "SME EMERGE", "EMERGE"}
NON_EQUITY_TYPES = {"DEBT", "INVIT", "REIT", "NCD", "BOND", "MF", "ETF", "IV", "RR"}

PART_CATEGORIES = ("QIB", "NII", "RETAIL", "EMPLOYEE", "SHAREHOLDER")

# Indian IPOs list three working days after the issue closes. The calendar
# carries stale rows whose listingDate has been stamped with a recent date
# years after the issue (Ola Electric: closed Aug 2024, "listed" Nov 2026),
# so anything outside this window is treated as unknown rather than real.
MAX_DAYS_CLOSE_TO_LISTING = 30
# How far the stated issue price may sit outside its own band before the
# field is considered corrupt (Ola Electric: issuePrice 11, band 72-76).
BAND_TOLERANCE = 0.05


def _canonical_category(row: dict) -> str | None:
    sr = str(row.get("srNo") or "").strip()
    label = str(row.get("category") or "").strip().lower()

    if label == "total" or (not sr and "total" in label):
        return "TOTAL"
    if sr in SR_NO_MAP:
        return SR_NO_MAP[sr]
    if sr:                     # "1(a)", "2.1(b)" etc. are sub-breakdowns
        return None
    # Fall back to label sniffing if srNo is missing entirely.
    if "qualified institutional" in label:
        return "QIB"
    if "retail" in label or "bidding for 2 lots" in label:
        return "RETAIL"
    if "employee" in label:
        return "EMPLOYEE"
    if "non institutional" in label:
        return "NII"
    return None


def _merge_into(target: CategoryBid, other: CategoryBid) -> None:
    """Fill gaps in ``target`` from ``other`` without clobbering real values."""
    target.shares_offered = target.shares_offered or other.shares_offered
    target.shares_bid = target.shares_bid or other.shares_bid
    if target.times is None:
        target.times = other.times
    target.applications = target.applications or other.applications


def _parse_bid_rows(rows: list[dict]) -> dict[str, CategoryBid]:
    """NSE uses several spellings for the same field across its projections."""
    out: dict[str, CategoryBid] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        # The activeCat projection repeats its header as the first data row.
        if str(row.get("category", "")).strip() == "Category":
            continue
        name = _canonical_category(row)
        if not name:
            continue

        offered = to_float(row.get("noOfSharesOffered", row.get("noOfShareOffered")))
        bid = to_float(row.get("noOfsharesBid",
                       row.get("noOfshareBid",
                       row.get("noOfSharesBid", row.get("noOfShareBid")))))
        times = to_float(row.get("noOfTime", row.get("noOfTotalMeant")))
        apps = to_int(row.get("noofapplication", row.get("noOfApplication")))

        parsed = CategoryBid(name=name, raw_label=str(row.get("category") or ""),
                             shares_offered=offered, shares_bid=bid,
                             times=times, applications=apps)
        if name in out:
            _merge_into(out[name], parsed)
        else:
            out[name] = parsed
    return out


def _price_band(row: dict) -> tuple[float | None, float | None]:
    text = str(row.get("priceRange") or "").replace("Rs.", " ")
    numbers = [n for n in (to_float(p) for p in text.split("to")) if n]
    if not numbers:
        return None, None
    return min(numbers), max(numbers)


def _issue_price(row: dict) -> float | None:
    """The cut-off price, cross-checked against the issue's own band.

    The calendar carries rows where issuePrice contradicts priceRange
    outright. Where they disagree the band is believed, since it is the
    field that matches the prospectus.
    """
    low, high = _price_band(row)
    stated = to_float(row.get("issuePrice"))

    if stated is not None and low is not None:
        if low * (1 - BAND_TOLERANCE) <= stated <= high * (1 + BAND_TOLERANCE):
            return stated
        log.debug("issuePrice %s outside band %s-%s for %s; using the band",
                  stated, low, high, row.get("symbol"))
        return high
    return stated if stated is not None else high


def _plausible_listing_date(close_date, listing_date, name):
    """Drop listing dates that cannot belong to this issue."""
    if listing_date is None or close_date is None:
        return listing_date
    gap = (listing_date - close_date).days
    if gap < 0 or gap > MAX_DAYS_CLOSE_TO_LISTING:
        log.debug("implausible listing date for %s: closed %s, listed %s (%d days)",
                  name, close_date, listing_date, gap)
        return None
    return listing_date


def fetch_calendar(fetcher) -> list[IPO]:
    """Every equity issue NSE knows about, as IPO records (no bid data yet)."""
    raw = fetcher.get(CALENDAR_URL, as_json=True,
                      referer=CALENDAR_REFERER, warm=CALENDAR_REFERER)
    if not isinstance(raw, list):
        log.error("NSE calendar unavailable")
        return []

    ipos: list[IPO] = []
    unknown_types: dict[str, int] = {}
    for row in raw:
        sec_type = str(row.get("securityType") or "").strip().upper()
        if sec_type in MAINBOARD_TYPES:
            segment = "MAINBOARD"
        elif sec_type in SME_TYPES:
            segment = "SME"
        else:
            if sec_type and sec_type not in NON_EQUITY_TYPES:
                unknown_types[sec_type] = unknown_types.get(sec_type, 0) + 1
            continue          # debt, InvITs, REITs, NCDs - not our business

        name = row.get("companyName") or row.get("company")
        if not name:
            continue

        price = _issue_price(row)
        close_date = parse_date(row.get("ipoEndDate"))
        listing_date = _plausible_listing_date(
            close_date, parse_date(row.get("listingDate")), name)

        ipos.append(IPO(
            name=str(name).strip(),
            symbol=(str(row.get("symbol")).strip() if row.get("symbol") else None),
            segment=segment,
            exchange="NSE SME" if segment == "SME" else "NSE",
            open_date=parse_date(row.get("ipoStartDate")),
            close_date=close_date,
            listing_date=listing_date,
            price=price,
            sources=["nse-calendar"],
        ))

    if unknown_types:
        log.warning("NSE calendar has unrecognised securityType values %s - "
                    "the vocabulary may have changed again", unknown_types)

    # A whole segment disappearing is how the EQ/BE -> Equity rename hid every
    # mainboard issue for a day. A large calendar with none of one segment is
    # a source change, not a quiet market.
    if len(raw) > 100:
        for segment in ("MAINBOARD", "SME"):
            if not any(i.segment == segment for i in ipos):
                log.error("NSE calendar returned %d rows but no %s issues at all "
                          "- securityType vocabulary has probably changed (seen: %s)",
                          len(raw), segment,
                          sorted({str(r.get('securityType')) for r in raw})[:12])
    return ipos


def _fill_offered_from_quota(ipo: IPO, quotas: dict) -> None:
    """Reconstruct per-category shares offered when NSE reports them as 0.

    The SME projection routinely returns ``noOfSharesOffered: "0"`` while still
    giving real bid quantities, which would leave every category unscored. The
    regulatory quota split against the total issue size is a sound stand-in.
    """
    total = ipo.total_shares_offered
    if not total or total <= 0:
        return
    split = quotas.get("sme" if ipo.segment == "SME" else "mainboard_book_built", {})
    for category, share in (("QIB", split.get("qib")),
                            ("NII", split.get("nii")),
                            ("RETAIL", split.get("retail"))):
        bid = ipo.bids.get(category)
        if bid and share and not bid.shares_offered:
            bid.shares_offered = total * share
            bid.offer_derived = True

    # bNII / sNII split the NII book two-thirds / one-third by regulation.
    nii = ipo.bids.get("NII")
    nii_offered = nii.shares_offered if nii else None
    if nii_offered:
        for category, fraction in (("BNII", 2 / 3), ("SNII", 1 / 3)):
            bid = ipo.bids.get(category)
            if bid and not bid.shares_offered:
                bid.shares_offered = nii_offered * fraction
                bid.offer_derived = True


def _rebuild_total(ipo: IPO) -> None:
    """Always derive the total from its parts.

    NSE's own ``Total`` row is unreliable - it has been observed carrying a
    subscription multiple in the shares-bid field (2.514288 for a 231x issue).
    Only its application count is trustworthy, so that is all we keep.
    """
    bid_for = sum(b.shares_bid or 0 for n, b in ipo.bids.items() if n in PART_CATEGORIES)
    if bid_for <= 0:
        return
    existing = ipo.bids.get("TOTAL")
    apps = existing.applications if existing else None
    offered = ipo.total_shares_offered or sum(
        b.shares_offered or 0 for n, b in ipo.bids.items() if n in PART_CATEGORIES)
    ipo.bids["TOTAL"] = CategoryBid(
        "TOTAL", raw_label="derived from categories",
        shares_offered=offered or None, shares_bid=bid_for, applications=apps)


def enrich_with_bids(fetcher, ipo: IPO, quotas: dict | None = None, *,
                     need_applications: bool = True) -> IPO:
    """Attach the category-wise bid table to one IPO.

    Two ``series`` projections are requested because they carry different
    fields: one has the subscription multiples, the other the application
    counts that drive allotment odds. (The parameter is in fact ignored by
    the server, but the two responses still differ.) A historical backfill
    only needs the ratios, so ``need_applications=False`` halves the requests.
    """
    if not ipo.symbol:
        return ipo

    merged: dict[str, CategoryBid] = {}
    for series in ("EQ", "SME"):
        payload = fetcher.get(DETAIL_URL.format(symbol=ipo.symbol, series=series),
                              as_json=True, referer=DETAIL_REFERER, warm=DETAIL_REFERER)
        if not isinstance(payload, dict):
            continue

        rows = list(payload.get("bidDetails") or [])
        rows += list((payload.get("activeCat") or {}).get("dataList") or [])
        for name, bid in _parse_bid_rows(rows).items():
            if name in merged:
                _merge_into(merged[name], bid)
            else:
                merged[name] = bid

        meta = payload.get("metaInfo") or {}
        ipo.industry = ipo.industry or meta.get("industry")
        ipo.listing_date = ipo.listing_date or parse_date(meta.get("listingDate"))

        total_size = to_float((payload.get("demandGraph") or {}).get("totalIssueSize"))
        if total_size:
            ipo.total_shares_offered = ipo.total_shares_offered or total_size

        stamp = (payload.get("activeCat") or {}).get("updateTime")
        if stamp and "null" not in str(stamp):
            ipo.subscription_updated = str(stamp).replace("Updated as on", "").strip()

        total = merged.get("TOTAL")
        if not need_applications and total and total.subscription():
            break      # ratios are all this caller wanted

    if merged:
        ipo.bids = merged
        ipo.sources.append("nse-bids")
        _rebuild_total(ipo)
        _fill_offered_from_quota(ipo, quotas or {})
    return ipo
