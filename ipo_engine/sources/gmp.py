"""Grey Market Premium adapters.

GMP is the single strongest short-term predictor of an Indian IPO's listing
price, but it is an *unofficial, unregulated* over-the-counter quote. It is
treated here as a noisy signal to be shrunk (see scoring.py), never as truth.

Two independent sources are parsed so one site changing its markup cannot
blind the engine:

  investorgain  rich table - GMP, GMP %, subscription, lot, issue size and,
                crucially, the exchange, so BSE-SME issues that never appear
                in NSE's API are still covered.
  ipowatch      simpler table, used as a fallback / cross-check.

Both are plain server-rendered HTML tables, so no browser is needed.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta

from ..models import IPO
from ..util import clean_text, to_float, to_int, today_ist

log = logging.getLogger(__name__)

INVESTORGAIN_URL = "https://www.investorgain.com/report/live-ipo-gmp/331/"
IPOWATCH_URL = "https://ipowatch.in/ipo-grey-market-premium-latest-ipo-gmp/"

_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
_TABLE_RE = re.compile(r"<table.*?</table>", re.S)

_PCT_IN_PARENS = re.compile(r"\(\s*(-?[\d.]+)\s*%\s*\)")
_RUPEE_AMOUNT = re.compile(r"₹\s*(-?[\d,]+(?:\.\d+)?)")
_LEADING_DATE = re.compile(r"^\s*(\d{1,2})\s*-\s*([A-Za-z]{3,})")

# Trailing tokens the sites append to the company name.
_NAME_NOISE = re.compile(
    r"\b(BSE\s*SME|NSE\s*SME|BSE|NSE|SME|LT|Closed|Listed|Upcoming|Open|Allotment"
    r"|Mainboard|IPO)\b", re.I)


def _tables_with_header(page: str, *required: str) -> list[tuple[list[str], list[list[str]]]]:
    """Split into (header, data rows) pairs for tables containing the given columns.

    Data rows keep their **raw** cell HTML, because the company name is only
    reliably available from the anchor's ``title`` attribute - the visible text
    carries exchange badges and status words glued on.
    """
    found = []
    for table in _TABLE_RE.findall(page or ""):
        raw_rows = [_CELL_RE.findall(r) for r in _ROW_RE.findall(table)]
        raw_rows = [r for r in raw_rows if r]
        if not raw_rows:
            continue
        header = [clean_text(h).lower() for h in raw_rows[0]]
        if all(any(req in h for h in header) for req in required):
            found.append((header, raw_rows[1:]))
    return found


def _column_index(header: list[str], *candidates: str) -> int | None:
    for candidate in candidates:
        for i, name in enumerate(header):
            if candidate in name:
                return i
    return None


def _infer_year(day: int, month_name: str, reference: date | None = None) -> date | None:
    """GMP tables print '2-Nov' with no year. Pick the nearest plausible one."""
    reference = reference or today_ist()
    for fmt in ("%b", "%B"):
        try:
            from datetime import datetime
            month = datetime.strptime(month_name[:3] if fmt == "%b" else month_name, fmt).month
            break
        except ValueError:
            continue
    else:
        return None
    for year in (reference.year, reference.year - 1, reference.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if abs((candidate - reference).days) <= 180:
            return candidate
    return None


def _parse_short_date(text: str) -> date | None:
    m = _LEADING_DATE.match(text or "")
    return _infer_year(int(m.group(1)), m.group(2)) if m else None


_TITLE_ATTR = re.compile(r'title\s*=\s*"([^"]{3,})"')


def _strip_name(raw_cell: str) -> tuple[str, str | None]:
    """Pull a clean company name and exchange out of the name cell.

    'HGS (India) NSE SME LT' -> ('HGS (India)', 'NSE SME')
    """
    text = clean_text(raw_cell)
    exchange = None
    upper = text.upper()
    for token in ("BSE SME", "NSE SME", "BSE", "NSE"):
        if token in upper:
            exchange = token
            break

    # The anchor's title attribute is the pristine name when it is present.
    title = _TITLE_ATTR.search(raw_cell or "")
    if title:
        name = clean_text(title.group(1))
        name = re.sub(r"\s+IPO$", "", name, flags=re.I).strip()
        if len(name) >= 3:
            return name, exchange

    # Otherwise drop noise tokens from the tail only, so a real 'India' or
    # 'SME' inside the middle of a company name survives.
    parts = text.split()
    while parts and (_NAME_NOISE.fullmatch(parts[-1]) or len(parts[-1]) == 1):
        parts.pop()
    name = " ".join(parts).strip(" -–|,")
    return (name or text), exchange


def fetch_investorgain(fetcher) -> list[IPO]:
    html = fetcher.get(INVESTORGAIN_URL, referer="https://www.investorgain.com/")
    if not html:
        log.warning("investorgain unreachable")
        return []

    tables = _tables_with_header(html, "name", "gmp")
    if not tables:
        log.warning("investorgain markup changed - no GMP table found")
        return []

    header, rows = tables[0]
    name_col = _column_index(header, "name", "company")
    idx = {
        "name": 0 if name_col is None else name_col,
        "gmp": _column_index(header, "gmp"),
        "sub": _column_index(header, "sub"),
        "price": _column_index(header, "price"),
        "size": _column_index(header, "ipo size"),
        "lot": _column_index(header, "lot"),
        "open": _column_index(header, "open"),
        "close": _column_index(header, "close"),
        "boa": _column_index(header, "boa"),
        "listing": _column_index(header, "listing"),
        "updated": _column_index(header, "updated"),
        "anchor": _column_index(header, "anchor"),
    }

    out: list[IPO] = []
    for cells in rows:
        if idx["name"] >= len(cells):
            continue
        name, exchange = _strip_name(cells[idx["name"]])
        if not name or len(name) < 3:
            continue

        def cell(key: str) -> str:
            i = idx.get(key)
            return clean_text(cells[i]) if i is not None and i < len(cells) else ""

        gmp_cell = cell("gmp")
        pct_match = _PCT_IN_PARENS.search(gmp_cell)
        rupee_match = _RUPEE_AMOUNT.search(gmp_cell)
        gmp = to_float(rupee_match.group(1)) if rupee_match else None
        gmp_pct = to_float(pct_match.group(1)) if pct_match else None

        # The 'Anchor' column shows a tick or a cross glyph; the cross is the
        # one rendered in red, which survives as a style attribute.
        anchor_raw = (cells[idx["anchor"]] if idx["anchor"] is not None
                      and idx["anchor"] < len(cells) else "")
        has_anchor = None
        if anchor_raw:
            has_anchor = "❌" not in anchor_raw and "color:red" not in anchor_raw

        segment = "SME" if exchange and "SME" in exchange else "MAINBOARD"
        ipo = IPO(
            name=name,
            segment=segment,
            exchange=exchange,
            price=to_float(cell("price")),
            lot_size=to_int(cell("lot")),
            issue_size_cr=to_float(cell("size")),
            gmp=gmp,
            gmp_pct=gmp_pct,
            gmp_source="investorgain",
            gmp_updated=cell("updated") or None,
            open_date=_parse_short_date(cell("open")),
            close_date=_parse_short_date(cell("close")),
            boa_date=_parse_short_date(cell("boa")),
            listing_date=_parse_short_date(cell("listing")),
            has_anchor=has_anchor,
            sources=["investorgain"],
        )
        sub = to_float(cell("sub"))
        if sub is not None:
            from ..models import CategoryBid
            ipo.bids["TOTAL"] = CategoryBid("TOTAL", raw_label="investorgain", times=sub)
        out.append(ipo)
    return out


def fetch_ipowatch(fetcher) -> list[IPO]:
    html = fetcher.get(IPOWATCH_URL, referer="https://ipowatch.in/")
    if not html:
        log.warning("ipowatch unreachable")
        return []

    out: list[IPO] = []
    for header, rows in _tables_with_header(html, "gmp"):
        name_col = _column_index(header, "company", "name")
        idx = {
            "name": 0 if name_col is None else name_col,
            "gmp": _column_index(header, "gmp"),
            "price": _column_index(header, "price"),
            "gain": _column_index(header, "gain"),
        }
        for cells in rows:
            if idx["name"] >= len(cells):
                continue
            name, exchange = _strip_name(cells[idx["name"]])
            if not name or len(name) < 3:
                continue

            def cell(key: str) -> str:
                i = idx.get(key)
                return clean_text(cells[i]) if i is not None and i < len(cells) else ""

            gain_match = _PCT_IN_PARENS.search(cell("gain"))
            out.append(IPO(
                name=name,
                exchange=exchange,
                price=to_float(cell("price")),
                gmp=to_float(cell("gmp")),
                gmp_pct=to_float(gain_match.group(1)) if gain_match else None,
                gmp_source="ipowatch",
                sources=["ipowatch"],
            ))
    return out


SUBSCRIPTION_URL = "https://www.investorgain.com/report/mainboard-ipo-list/333/"

# The report's column names mapped onto our canonical categories.
_SUB_COLUMNS = {
    "total": "TOTAL",
    "qib": "QIB",
    "shni": "SNII",     # small HNI == sNII (Rs 2-10 lakh)
    "bhni": "BNII",     # big HNI   == bNII (above Rs 10 lakh)
    "nii": "NII",
    "rii": "RETAIL",
}


def fetch_category_subscription(fetcher) -> list[IPO]:
    """Published category-wise subscription multiples.

    Worth a separate request because the exchange does not report the
    per-category *offer* size for SME issues, forcing us to reconstruct it
    from the regulatory quota - which was measured against this report to be
    off by roughly a third. These are the published ratios, so they are
    preferred wherever our own figure would be quota-derived guesswork.
    """
    html = fetcher.get(SUBSCRIPTION_URL, referer="https://www.investorgain.com/")
    if not html:
        log.warning("investorgain subscription report unreachable")
        return []

    tables = _tables_with_header(html, "total", "qib")
    if not tables:
        log.warning("investorgain subscription report markup changed")
        return []

    from ..models import CategoryBid
    header, rows = tables[0]
    name_col = _column_index(header, "name", "company")
    name_col = 0 if name_col is None else name_col
    col_for = {canonical: _column_index(header, label)
               for label, canonical in _SUB_COLUMNS.items()}

    out: list[IPO] = []
    for cells in rows:
        if name_col >= len(cells):
            continue
        name, exchange = _strip_name(cells[name_col])
        if not name or len(name) < 3:
            continue

        ipo = IPO(name=name, exchange=exchange,
                  segment="SME" if exchange and "SME" in exchange else "MAINBOARD",
                  sources=["investorgain-subs"])
        for canonical, index in col_for.items():
            if index is None or index >= len(cells):
                continue
            value = to_float(clean_text(cells[index]))
            if value is not None and value > 0:
                ipo.bids[canonical] = CategoryBid(
                    canonical, raw_label="investorgain-subs", times=value)
        if ipo.bids:
            out.append(ipo)
    return out


def fetch_all(fetcher) -> list[IPO]:
    """investorgain first (richest); ipowatch fills whatever it missed."""
    primary = fetch_investorgain(fetcher)
    index = {ipo.key: ipo for ipo in primary}

    # Fold the published category multiples into the matching GMP rows.
    for subs in fetch_category_subscription(fetcher):
        target = index.get(subs.key)
        if target is None:
            index[subs.key] = subs
            primary.append(subs)
            continue
        for category, bid in subs.bids.items():
            existing = target.bids.get(category)
            if existing is None or existing.times is None:
                target.bids[category] = bid
        if "investorgain-subs" not in target.sources:
            target.sources.append("investorgain-subs")

    for ipo in fetch_ipowatch(fetcher):
        if ipo.key not in index:
            primary.append(ipo)
    return primary
