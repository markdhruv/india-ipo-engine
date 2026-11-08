"""Decide what is worth interrupting the user for.

Three distinct triggers, because they answer different questions:

  oversubscribed  the book crossed 1x / 5x / 20x / 50x / 100x - momentum news,
                  fired once per level so a 250x issue does not spam five
                  times per level per hour.
  opportunity     expected listing gain cleared the configured bar (default
                  10%) while the issue is still open and therefore actionable.
  closing_today   an actionable issue closes today - this is the only alert
                  that is genuinely time-critical, because the UPI mandate
                  cutoff is ~5 PM IST and misses cannot be undone.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import IPO
from .store import Store
from .util import inr, now_ist, pct


@dataclass
class Alert:
    ipo_key: str
    kind: str
    level: float | None
    title: str
    body: str
    urgent: bool = False


def _headline(ipo: IPO, goal: float) -> str:
    """Lead with the odds where they exist, the estimate where they do not."""
    if ipo.clear_probability is not None:
        return f"{ipo.clear_probability*100:.0f}% chance of clearing {goal:g}%"
    if ipo.expected_gain_pct is not None:
        return f"est. {ipo.expected_gain_pct:+.1f}% on listing"
    return "see dashboard"


def _summary(ipo: IPO) -> str:
    bits = []
    if ipo.clear_probability is not None and ipo.outcome_median is not None:
        bits.append(f"similar issues: median {ipo.outcome_median:+.1f}% "
                    f"(p25 {ipo.outcome_p25:+.0f}% / p75 {ipo.outcome_p75:+.0f}%)")
    if ipo.total_subscription is not None:
        bits.append(f"{ipo.total_subscription:.2f}x subscribed")
    if ipo.gmp_pct is not None:
        bits.append(f"GMP {ipo.gmp_pct:+.1f}%")
    if ipo.expected_gain_pct is not None:
        bits.append(f"est. listing {ipo.expected_gain_pct:+.1f}%")
    odds = ipo.allotment_prob.get("RETAIL")
    if odds is not None:
        bits.append(f"retail allotment {odds*100:.2f}%")
    if ipo.capital_per_application:
        bits.append(f"1 lot = {inr(ipo.capital_per_application)}")
    return " | ".join(bits)


def _detail(ipo: IPO) -> str:
    lines = [_summary(ipo)]
    categories = [(name, ipo.sub(name)) for name in ("QIB", "NII", "BNII", "SNII", "RETAIL")]
    shown = [f"{name} {value:.1f}x" for name, value in categories if value is not None]
    if shown:
        lines.append("Categories: " + ", ".join(shown))
    if ipo.weakest_category and ipo.weakest_category_sub is not None:
        lines.append(f"Weakest leg: {ipo.weakest_category} "
                     f"{ipo.weakest_category_sub:,.1f}x")
    if ipo.expected_value_inr is not None:
        lines.append(f"Expected value per application: {inr(ipo.expected_value_inr)}")
    if ipo.close_date:
        lines.append(f"Closes {ipo.close_date:%d %b %Y}"
                     + (f" | lists {ipo.listing_date:%d %b}" if ipo.listing_date else ""))
    if ipo.reasons:
        lines.append("Why: " + "; ".join(ipo.reasons[:3]))
    lines.append(f"Confidence {ipo.confidence}/100 - informational only, not advice.")
    return "\n".join(lines)


def _crossed_levels(current: float, levels: list[float]) -> list[float]:
    return [level for level in sorted(levels) if current >= level]


def build(ipos: list[IPO], cfg: dict, store: Store) -> list[Alert]:
    """Every alert that should fire now, already de-duplicated against history."""
    triggers = cfg["triggers"]
    gate = triggers["min_expected_listing_gain_pct"]
    min_sub = triggers["min_total_subscription"]
    min_confidence = triggers.get("min_confidence", 0.60)
    only_open = triggers.get("alert_only_while_open", True)
    urgent_hour = triggers.get("close_day_urgent_hour_ist", 13)

    out: list[Alert] = []
    for ipo in ipos:
        if only_open and not ipo.is_open:
            continue

        label = f"{ipo.name} ({ipo.segment.title()})"

        # --- oversubscription milestones ---
        # Only the highest newly-crossed level fires. An issue first seen at
        # 62x has technically crossed 1x, 5x, 20x and 50x, and announcing all
        # four is noise; the lower ones are marked spent so they stay quiet on
        # later runs.
        total = ipo.total_subscription
        if total is not None:
            pending = [level for level
                       in _crossed_levels(total, triggers["oversubscription_alert_levels"])
                       if not store.already_alerted(ipo.key, "oversubscribed", level)]
            if pending:
                for superseded in pending[:-1]:
                    store.record_alert(
                        ipo.key, "oversubscribed", superseded,
                        f"{ipo.name}: {superseded:g}x subscribed",
                        "superseded by a higher level crossed in the same run")
                highest = pending[-1]
                out.append(Alert(
                    ipo_key=ipo.key, kind="oversubscribed", level=highest,
                    title=f"{ipo.name}: {highest:g}x subscribed",
                    body=_detail(ipo)))

        # --- the actual decision signal ---
        # Two settings decide this: the goal, and how sure the engine must be
        # before interrupting. Where no odds exist (too little history) the
        # point estimate stands in, with its old verdict labels.
        gain = ipo.expected_gain_pct
        if ipo.clear_probability is not None:
            worth_saying = ipo.clear_probability >= min_confidence
        else:
            worth_saying = (gain is not None and gain >= gate
                            and ipo.verdict in ("ACTIONABLE", "STRONG"))

        if worth_saying and (total is None or total >= min_sub):
            if not store.already_alerted(ipo.key, "opportunity", None):
                out.append(Alert(
                    ipo_key=ipo.key, kind="opportunity", level=None,
                    title=f"{label}: {_headline(ipo, gate)}",
                    body=_detail(ipo)))

            if ipo.closes_today:
                hour = now_ist().hour
                if hour >= urgent_hour and not store.already_alerted(
                        ipo.key, "closing_today", None):
                    out.append(Alert(
                        ipo_key=ipo.key, kind="closing_today", level=None,
                        title=f"CLOSES TODAY - {ipo.name} {_headline(ipo, gate)}",
                        body="Apply before the UPI mandate cutoff (~5 PM IST).\n"
                             + _detail(ipo),
                        urgent=True))
    return out


def dispatch(alerts: list[Alert], notifier, store: Store) -> int:
    """Send each alert once and persist that it went out."""
    sent = 0
    for alert in alerts:
        if not store.record_alert(alert.ipo_key, alert.kind, alert.level,
                                  alert.title, alert.body):
            continue        # lost a race with a concurrent run
        notifier.send(alert.title, alert.body, urgent=alert.urgent)
        sent += 1
    return sent
