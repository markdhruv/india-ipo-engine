"""Shared helpers: UTF-8 console, IST clock, tolerant number/date parsing."""
from __future__ import annotations

import html
import io
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# India has never observed DST, so a fixed +05:30 offset is exact and needs no
# tzdata package (Windows ships none).
IST = timezone(timedelta(hours=5, minutes=30), name="IST")
ROOT = Path(__file__).resolve().parent.parent


def force_utf8() -> None:
    """Windows consoles default to cp1252 and die on the rupee sign."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if hasattr(stream, "buffer") and getattr(stream, "encoding", "").lower() != "utf-8":
            setattr(sys, name, io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace"))


def now_ist() -> datetime:
    return datetime.now(IST)


def today_ist() -> date:
    return now_ist().date()


def load_config(path: str | Path | None = None) -> dict:
    p = Path(path) if path else ROOT / "config.json"
    return json.loads(p.read_text(encoding="utf-8"))


_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def to_float(value, default=None):
    """Pull the first number out of messy strings: '₹1,234.5', '12.79x', '1.9 x', '-'."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").strip()
    if text in ("", "-", "--", "NA", "N/A", "nan"):
        return default
    m = _NUM.search(text)
    return float(m.group(0)) if m else default


def to_int(value, default=None):
    f = to_float(value, None)
    return int(f) if f is not None else default


_DATE_FORMATS = ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y", "%d-%b-%y")


def parse_date(value) -> date | None:
    if not value or str(value).strip() in ("-", "--", ""):
        return None
    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def clean_text(value) -> str:
    """Strip tags, decode entities, drop the arrows/emoji the GMP tables inject.

    Order matters. Tags go first so that an escaped ``&lt;b&gt;`` cannot turn
    into a real tag, then entities are decoded - without this step a cell
    reading ``&#8377;22.98 Cr`` parses as the number 8377 rather than 22.98.
    Emoji are stripped only after decoding, since they arrive as entities too
    (``&#128293;`` is the flame the rating column uses).
    """
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = html.unescape(text)
    text = text.replace("\u25b2", " ").replace("\u25bc", " ")
    text = re.sub(r"[\U0001F300-\U0001FAFF\u2190-\u21FF\u2600-\u27BF\u2B00-\u2BFF]", " ", text)
    text = text.replace("[email protected]", " ")
    return re.sub(r"\s+", " ", text).strip()


def norm_name(name: str) -> str:
    """Normalise a company name so NSE and the GMP sites can be matched up.

    'M. K. Sons Fine Jewels Limited' and 'MK Sons Fine Jewels' -> 'mksonsfinejewels'
    """
    text = clean_text(name).lower()
    text = re.sub(r"\b(limited|ltd|private|pvt|india|indian|the)\b", " ", text)
    text = re.sub(r"\b(ipo|sme|nse|bse|listed|closed|open|upcoming|lt)\b", " ", text)
    return re.sub(r"[^a-z0-9]", "", text)


def pct(value, digits=1) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}%"


def inr(value) -> str:
    if value is None:
        return "n/a"
    value = float(value)
    if abs(value) >= 1e7:
        return f"\u20b9{value/1e7:,.2f} Cr"
    if abs(value) >= 1e5:
        return f"\u20b9{value/1e5:,.2f} L"
    return f"\u20b9{value:,.0f}"
