"""Standalone HTML dashboard.

Desktop toasts vanish; this file is the durable record of what the engine saw
and why it said it. No CDN, no network - it has to open from disk offline.
"""
from __future__ import annotations

import html
from pathlib import Path

from .models import IPO
from .util import ROOT, inr, now_ist

_VERDICT_CLASS = {
    # Probability bands - these describe the odds, not a recommendation.
    "HIGH": "v-strong",
    "MODERATE": "v-act",
    "LOW": "v-watch",
    "VERY LOW": "v-avoid",
    "UNKNOWN": "v-none",
    # Legacy gain-based verdicts, used only before enough history exists.
    "STRONG": "v-strong",
    "ACTIONABLE": "v-act",
    "WATCH": "v-watch",
    "AVOID": "v-avoid",
    "NO DATA": "v-none",
}

_CSS = """
:root{
  --bg:#f7f8fa; --panel:#ffffff; --ink:#15181d; --muted:#6b7280;
  --line:#e4e7ec; --accent:#1f6feb;
  --pos:#0f7b3f; --posbg:#e7f6ed; --neg:#b4232c; --negbg:#fdeced;
  --warn:#9a6700; --warnbg:#fff8e1;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme=light]){
    --bg:#0f1115; --panel:#171a21; --ink:#e8eaed; --muted:#9aa3af;
    --line:#262b35; --accent:#5b9dff;
    --pos:#4ade80; --posbg:#10291b; --neg:#f87171; --negbg:#2b1416;
    --warn:#fbbf24; --warnbg:#2a2110;
  }
}
:root[data-theme=dark]{
  --bg:#0f1115; --panel:#171a21; --ink:#e8eaed; --muted:#9aa3af;
  --line:#262b35; --accent:#5b9dff;
  --pos:#4ade80; --posbg:#10291b; --neg:#f87171; --negbg:#2b1416;
  --warn:#fbbf24; --warnbg:#2a2110;
}
*{box-sizing:border-box}
body{margin:0;padding:24px 16px 56px;background:var(--bg);color:var(--ink);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13px;margin-bottom:22px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:12px;margin-bottom:24px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
.card .n{font-size:26px;font-weight:650;letter-spacing:-.02em}
.card .l{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  overflow:hidden;margin-bottom:22px}
.panel h2{font-size:14px;margin:0;padding:13px 16px;border-bottom:1px solid var(--line);
  text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{padding:10px 12px;text-align:right;white-space:nowrap;
  border-bottom:1px solid var(--line)}
th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);
  font-weight:600;background:var(--bg)}
th:first-child,td:first-child{text-align:left;white-space:normal;min-width:190px}
tbody tr:last-child td{border-bottom:none}
.name{font-weight:600}
.meta{color:var(--muted);font-size:11.5px}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;
  font-weight:650;letter-spacing:.03em}
.v-strong{background:var(--posbg);color:var(--pos)}
.v-act{background:var(--posbg);color:var(--pos)}
.v-watch{background:var(--warnbg);color:var(--warn)}
.v-avoid{background:var(--negbg);color:var(--neg)}
.v-none{background:var(--bg);color:var(--muted)}
.pos{color:var(--pos);font-weight:600}
.neg{color:var(--neg);font-weight:600}
.warn-ink{color:var(--warn);font-weight:600}
.thin{color:var(--muted)}
.bar{height:5px;border-radius:3px;background:var(--line);overflow:hidden;
  min-width:52px;margin-top:4px}
.bar i{display:block;height:100%;background:var(--accent)}
.note{background:var(--warnbg);border:1px solid var(--line);border-left:3px solid var(--warn);
  border-radius:8px;padding:13px 15px;color:var(--ink);font-size:12.5px;line-height:1.55}
.empty{padding:26px 16px;color:var(--muted);text-align:center;font-size:13.5px}
@media (max-width:640px){body{padding:16px 12px 40px}.card .n{font-size:21px}}
"""


def _fmt_pct(value, digits=1, signed=True) -> str:
    if value is None:
        return '<span class="thin">—</span>'
    cls = "pos" if value > 0 else ("neg" if value < 0 else "thin")
    sign = "+" if signed and value > 0 else ""
    return f'<span class="{cls}">{sign}{value:.{digits}f}%</span>'


def _fmt_sub(value) -> str:
    if value is None:
        return '<span class="thin">—</span>'
    cls = "pos" if value >= 1 else "neg"
    return f'<span class="{cls}">{value:,.2f}x</span>'


def _fmt_probability(ipo: IPO) -> str:
    """Odds of clearing the goal, with the spread of similar past issues."""
    if ipo.clear_probability is None:
        return '<span class="thin">—</span>'
    pct = ipo.clear_probability * 100
    cls = "pos" if pct >= 60 else ("warn-ink" if pct >= 30 else "neg")
    spread = ""
    if ipo.outcome_p25 is not None and ipo.outcome_p75 is not None:
        spread = (f'<div class="meta">similar: {ipo.outcome_p25:+.0f}% to '
                  f'{ipo.outcome_p75:+.0f}%</div>')
    return f'<span class="{cls}">{pct:.0f}%</span>{spread}'


def _fmt_weakest(ipo: IPO) -> str:
    """The least-covered major leg - a strong total can hide a weak one."""
    name, value = ipo.weakest_category, ipo.weakest_category_sub
    if name is None or value is None:
        return '<span class="thin">—</span>'
    cls = "neg" if value < 1.0 else ("warn-ink" if value < 5.0 else "pos")
    return f'<span class="{cls}">{name} {value:,.1f}x</span>'


def _fmt_odds(value) -> str:
    if value is None:
        return '<span class="thin">—</span>'
    width = max(1.5, min(100.0, value * 100))
    return (f'{value*100:.2f}%<div class="bar"><i style="width:{width:.1f}%"></i></div>')


def _row(ipo: IPO) -> str:
    verdict = ipo.verdict or "NO DATA"
    cls = _VERDICT_CLASS.get(verdict, "v-none")
    meta = " · ".join(filter(None, [
        ipo.exchange or ipo.segment.title(),
        f"{ipo.close_date:%d %b}" if ipo.close_date else None,
        f"lot {ipo.lot_size:,}" if ipo.lot_size else None,
        f"{ipo.issue_size_cr:,.0f} Cr" if ipo.issue_size_cr else None,
    ]))
    return f"""<tr>
  <td><div class="name">{html.escape(ipo.name)}</div><div class="meta">{html.escape(meta)}</div></td>
  <td><span class="pill {cls}">{verdict}</span></td>
  <td>{_fmt_probability(ipo)}</td>
  <td>{_fmt_pct(ipo.expected_gain_pct)}</td>
  <td>{_fmt_pct(ipo.gmp_pct)}</td>
  <td>{_fmt_sub(ipo.total_subscription)}</td>
  <td>{_fmt_sub(ipo.sub("QIB"))}</td>
  <td>{_fmt_sub(ipo.sub("RETAIL"))}</td>
  <td>{_fmt_weakest(ipo)}</td>
  <td>{_fmt_odds(ipo.allotment_prob.get("RETAIL"))}</td>
  <td>{inr(ipo.capital_per_application) if ipo.capital_per_application else '<span class="thin">—</span>'}</td>
  <td>{inr(ipo.expected_value_inr) if ipo.expected_value_inr is not None else '<span class="thin">—</span>'}</td>
  <td class="thin">{ipo.confidence if ipo.confidence is not None else '—'}</td>
</tr>"""


_HEADERS = ("Issue", "Odds", "Chance of goal", "Est. listing", "GMP",
            "Total sub", "QIB", "Retail", "Weakest leg",
            "Retail allotment odds", "Capital / lot", "Expected value", "Conf.")


def _table(ipos: list[IPO], title: str, empty: str) -> str:
    if not ipos:
        return f'<div class="panel"><h2>{title}</h2><div class="empty">{empty}</div></div>'
    head = "".join(f"<th>{h}</th>" for h in _HEADERS)
    body = "\n".join(_row(i) for i in ipos)
    return (f'<div class="panel"><h2>{title}</h2><div class="scroll">'
            f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
            f"</div></div>")


def _plan_panel(ipos: list[IPO], cfg: dict) -> str:
    """What the configured budget and PAN count can actually apply for."""
    from .portfolio import build_plan

    plan = build_plan(ipos, cfg)
    header = (f'<h2>Your plan &middot; {inr(plan.budget)} across {plan.pans} PAN'
              f'{"s" if plan.pans != 1 else ""}</h2>')

    if not plan.applications:
        tail = plan.skipped_tail()
        note = ("Nothing is open that clears the bar within this budget."
                if not tail else
                f"{len(tail)} issue(s) clear the bar but cost more than the budget.")
        return f'<div class="panel">{header}<div class="empty">{note}</div></div>'

    rows = "".join(
        f"<tr><td><div class='name'>{html.escape(a.ipo.name)}</div>"
        f"<div class='meta'>{html.escape(a.ipo.exchange or a.ipo.segment.title())}</div></td>"
        f"<td>{a.pans}</td><td>{inr(a.lot_value)}</td><td>{inr(a.capital)}</td>"
        f"<td>{a.odds*100:.2f}%</td><td>{_fmt_pct(a.gain_pct)}</td>"
        f"<td>{inr(a.expected_inr)}</td></tr>"
        for a in plan.applications)

    head = "".join(f"<th>{h}</th>" for h in
                   ("Issue", "PANs", "1 lot", "Capital", "Odds", "Est. gain", "E[value]"))

    dist = plan.distribution
    outcomes = "".join(
        f'<div class="card"><div class="n">{value}</div><div class="l">{label}</div></div>'
        for label, value in (
            ("No allotment", f"{plan.p_none*100:.0f}%"),
            ("One or more", f"{plan.p_any*100:.0f}%"),
            ("Expected allotments", f"{plan.expected_allotments:.2f}"),
            ("Expected value", inr(plan.expected_inr)),
        ))

    tail = plan.skipped_tail()
    tail_html = ""
    if tail:
        items = "; ".join(
            f"{html.escape(s.ipo.name)} ({inr(s.ipo.capital_per_application)})"
            for s in tail[:4])
        tail_html = (f'<div class="meta" style="padding:0 16px 14px">'
                     f'Out of reach on this budget: {items}</div>')

    return (f'<div class="panel">{header}<div class="scroll">'
            f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>"
            f'</div><div class="cards" style="padding:14px 16px 4px">{outcomes}</div>'
            f'{tail_html}'
            f'<div class="meta" style="padding:0 16px 14px">'
            f'One lot per PAN &mdash; extra lots do not improve allotment odds. '
            f'Deployed {inr(plan.deployed)}, idle {inr(plan.idle)}.</div></div>')


_REGIME_CLASS = {"HOT": "v-strong", "NORMAL": "v-watch",
                 "COLD": "v-avoid", "UNKNOWN": "v-none"}


def _regime_panel(cfg: dict) -> str:
    """How recent IPOs actually listed - the market's temperature."""
    from . import outcomes as outcomes_mod
    from .store import Store

    try:
        with Store(cfg["paths"]["db"]) as store:
            reg = outcomes_mod.regime(store, cfg, lookback=20)
            recent = store.outcomes(limit=10)
    except Exception:                                  # pragma: no cover
        return ""

    if not reg.sample:
        return ('<div class="panel"><h2>Market regime</h2>'
                '<div class="empty">No listing outcomes captured yet &mdash; '
                'run <code>run.py outcomes --backfill 100</code>.</div></div>')

    bar = cfg["triggers"]["min_expected_listing_gain_pct"]
    cards = "".join(
        f'<div class="card"><div class="n">{value}</div><div class="l">{label}</div></div>'
        for label, value in (
            ("Median listing gain", f"{reg.median_gain:+.1f}%"),
            (f"Cleared {bar:g}%", f"{reg.hit_rate*100:.0f}%"),
            ("Listed below issue", f"{reg.negative_rate*100:.0f}%"),
            ("Sample", str(reg.sample)),
        ))

    chips = "".join(
        f'<span class="pill {"v-act" if (r["gain_open"] or 0) >= bar else "v-avoid"}" '
        f'style="margin:0 6px 6px 0;display:inline-block">'
        f'{html.escape((r["name"] or "")[:18])} {r["gain_open"]:+.0f}%</span>'
        for r in recent)

    klass = _REGIME_CLASS.get(reg.label, "v-none")
    return (f'<div class="panel"><h2>Market regime &middot; '
            f'<span class="pill {klass}">{reg.label}</span></h2>'
            f'<div class="cards" style="padding:14px 16px 0">{cards}</div>'
            f'<div style="padding:4px 16px 14px">{chips}</div></div>')


def render(ipos: list[IPO], cfg: dict, alerts_fired: int = 0) -> str:
    def sort_key(ipo: IPO):
        return (ipo.expected_gain_pct if ipo.expected_gain_pct is not None else -999)

    live = sorted([i for i in ipos if i.is_open], key=sort_key, reverse=True)
    pending = sorted([i for i in ipos if not i.is_open], key=sort_key, reverse=True)[:15]

    gate = cfg["triggers"]["min_expected_listing_gain_pct"]
    actionable = [i for i in live if i.verdict in ("ACTIONABLE", "STRONG")]
    stamp = now_ist().strftime("%d %b %Y, %H:%M IST")

    cards = [
        ("Open now", str(len(live))),
        (f"Clearing {gate:g}%", str(len(actionable))),
        ("Closing today", str(len([i for i in live if i.closes_today]))),
        ("Alerts fired", str(alerts_fired)),
    ]
    card_html = "".join(
        f'<div class="card"><div class="n">{value}</div><div class="l">{label}</div></div>'
        for label, value in cards)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>India IPO Engine</title><style>{_CSS}</style></head>
<body><div class="wrap">
<h1>India IPO Oversubscription &amp; Listing-Gain Engine</h1>
<div class="sub">Last run {stamp} · gain bar {gate:g}% · mainboard + SME · NSE bid data, GMP from investorgain / ipowatch</div>
<div class="cards">{card_html}</div>
{_regime_panel(cfg)}
{_plan_panel(ipos, cfg)}
{_table(live, "Open for subscription", "No IPO is open right now. The engine will alert you when one opens.")}
{_table(pending, "Recently closed / awaiting listing", "Nothing recent.")}
<div class="note">
<b>Read this before acting.</b> &quot;Est. listing&quot; is a model estimate, not a forecast, built from
<b>Grey Market Premium</b> — an unofficial, unregulated, often thin over-the-counter quote that can be
manipulated and regularly fails to predict the open — plus subscription momentum. Allotment odds are
derived from lots available ÷ applications received and shift until the book closes.
A high subscription multiple and a high allotment chance are <i>opposites</i>: the most oversubscribed
issues are the hardest to get. Listing gains are realised only if you sell into listing-day liquidity,
which SME counters frequently lack. This tool is informational software, not investment advice, and
carries no recommendation to apply for any issue.
</div>
</div></body></html>"""


def write(ipos: list[IPO], cfg: dict, alerts_fired: int = 0) -> Path:
    target = Path(cfg["paths"]["dashboard"])
    if not target.is_absolute():
        target = ROOT / target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(ipos, cfg, alerts_fired), encoding="utf-8")
    return target
