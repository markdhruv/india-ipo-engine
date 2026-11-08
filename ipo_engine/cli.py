"""Command line entry point."""
from __future__ import annotations

import argparse
import logging
import sys
import time
import webbrowser
from pathlib import Path

from . import dashboard
from .engine import run_once
from .notify import Notifier
from .store import Store
from .util import ROOT, force_utf8, inr, load_config, now_ist


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _print_table(ipos, cfg) -> None:
    live = [i for i in ipos if i.is_open]
    other = [i for i in ipos if not i.is_open]

    def show(rows, heading):
        print(f"\n{heading}")
        if not rows:
            print("  (none)")
            return
        goal = cfg["triggers"]["min_expected_listing_gain_pct"]
        print(f"  {'ISSUE':<34}{'ODDS':<11}{f'P({goal:g}%)':>8}{'EST':>8}{'GMP':>8}"
              f"{'TOTAL':>9}{'QIB':>9}{'ALLOT':>8}{'CAPITAL':>13}{'EV':>11}")
        print("  " + "-" * 121)
        for i in sorted(rows, key=lambda x: (x.clear_probability
                        if x.clear_probability is not None else -1,
                        x.expected_gain_pct or -999), reverse=True):
            prob = (f"{i.clear_probability*100:.0f}%"
                    if i.clear_probability is not None else "-")
            est = f"{i.expected_gain_pct:+.1f}%" if i.expected_gain_pct is not None else "-"
            gmp_s = f"{i.gmp_pct:+.1f}%" if i.gmp_pct is not None else "-"
            tot = f"{i.total_subscription:.2f}x" if i.total_subscription is not None else "-"
            qib = f"{i.sub('QIB'):.1f}x" if i.sub("QIB") is not None else "-"
            odds = i.allotment_prob.get("RETAIL")
            odds_s = f"{odds*100:.2f}%" if odds is not None else "-"
            cap = inr(i.capital_per_application) if i.capital_per_application else "-"
            ev = inr(i.expected_value_inr) if i.expected_value_inr is not None else "-"
            print(f"  {i.name[:33]:<34}{(i.verdict or '-'):<11}{prob:>8}{est:>8}"
                  f"{gmp_s:>8}{tot:>9}{qib:>9}{odds_s:>8}{cap:>13}{ev:>11}")

    show(live, "OPEN FOR SUBSCRIPTION")
    show(other[:10], "RECENTLY CLOSED / AWAITING LISTING")
    print("\n  Informational only - not investment advice. GMP is unofficial and unregulated.")


def cmd_once(args, cfg) -> int:
    ipos = run_once(cfg, quiet=args.quiet)
    _print_table(ipos, cfg)
    if args.open:
        path = Path(cfg["paths"]["dashboard"])
        webbrowser.open((path if path.is_absolute() else ROOT / path).as_uri())
    return 0


def cmd_watch(args, cfg) -> int:
    interval = args.interval * 60
    print(f"Watching every {args.interval} min. Ctrl+C to stop.")
    with Store(cfg["paths"]["db"]) as store:
        notifier = Notifier(cfg)
        while True:
            started = now_ist()
            try:
                ipos = run_once(cfg, notifier=notifier, store=store, quiet=args.quiet)
                live = sum(1 for i in ipos if i.is_open)
                print(f"[{started:%H:%M:%S}] {len(ipos)} tracked, {live} open")
            except Exception:
                logging.exception("run failed; continuing")
            time.sleep(interval)


def cmd_dashboard(args, cfg) -> int:
    """Refresh from live sources, re-render, and open in the browser.

    Alerts are suppressed so that opening the dashboard never fires a toast.
    """
    ipos = run_once(cfg, quiet=True)
    path = dashboard.write(ipos, cfg)
    print(f"Dashboard: {path}")
    webbrowser.open(path.as_uri())
    return 0


def cmd_app(args, cfg) -> int:
    """Run the local app. Python does the fetching; the browser is the face."""
    from .webapp import serve
    return serve(cfg, port=args.port, open_browser=not args.no_browser)


def cmd_plan(args, cfg) -> int:
    """What to actually apply for, given a budget and a PAN count."""
    from . import outcomes as outcomes_mod
    from .portfolio import build_plan

    ipos = run_once(cfg, quiet=True)
    plan = build_plan(ipos, cfg, budget=args.capital, pans=args.pans)

    print(f"\nBudget {inr(plan.budget)} across {plan.pans} PAN(s)"
          f"   |   bar: {cfg['triggers']['min_expected_listing_gain_pct']:g}% "
          f"estimated listing gain")

    # How recent issues actually listed is the cheapest context there is for
    # deciding whether to participate at all this month.
    with Store(cfg["paths"]["db"]) as store:
        reading = outcomes_mod.regime(store, cfg, lookback=20)
    if reading.sample:
        print(f"Market regime: {reading.describe()}")
    print("One lot per PAN - bidding extra lots does not improve allotment odds.\n")

    if not plan.applications:
        print("  Nothing to apply for right now.")
    else:
        print(f"  {'ISSUE':<30}{'PANS':>6}{'LOT':>12}{'CAPITAL':>12}"
              f"{'ODDS':>8}{'EST':>8}{'E[Rs]':>10}")
        print("  " + "-" * 86)
        for a in plan.applications:
            print(f"  {a.ipo.name[:29]:<30}{a.pans:>6}{inr(a.lot_value):>12}"
                  f"{inr(a.capital):>12}{a.odds*100:7.2f}%{a.gain_pct:+7.1f}%"
                  f"{inr(a.expected_inr):>10}")
        print("  " + "-" * 86)
        print(f"  {'deployed':<30}{plan.application_count:>6}{'':>12}"
              f"{inr(plan.deployed):>12}   idle {inr(plan.idle)}")

    tail = plan.skipped_tail()
    if tail:
        print(f"\n  Out of reach on this budget ({len(tail)}):")
        for s in tail:
            lot = s.ipo.capital_per_application
            print(f"    {s.ipo.name[:30]:<32}{inr(lot) if lot else '-':>11}"
                  f"  est {s.ipo.expected_gain_pct:+.1f}%   {s.reason}")

    if plan.applications:
        dist = plan.distribution
        print(f"\n  OUTCOME over these {plan.application_count} application(s)")
        print(f"    no allotment at all .... {plan.p_none*100:5.1f}%")
        for k in range(1, min(4, len(dist))):
            print(f"    exactly {k} allotment{'s' if k > 1 else ' '} ...... {dist[k]*100:5.1f}%")
        if len(dist) > 4:
            print(f"    4 or more .............. {sum(dist[4:])*100:5.1f}%")
        print(f"\n    expected allotments .... {plan.expected_allotments:.2f}")
        print(f"    expected value ......... {inr(plan.expected_inr)}")
        print(f"    on budget deployed ..... {plan.return_on_budget_pct:.2f}% "
              f"of {inr(plan.budget)}")
        print("\n  Allotment is a draw of lots; the single most likely outcome "
              "of one cycle is nothing.")
    print("\n  Informational only - not investment advice.\n")
    return 0


def cmd_outcomes(args, cfg) -> int:
    """Capture realised listing prices and report what they say."""
    from . import outcomes as outcomes_mod
    from .http import Fetcher
    from .sources import nse

    with Store(cfg["paths"]["db"]) as store:
        if args.backfill:
            fetcher = Fetcher(cfg)
            calendar = nse.fetch_calendar(fetcher)
            print(f"Backfilling up to {args.backfill} listings "
                  f"from {len(calendar)} calendar rows...")
            stats = outcomes_mod.backfill(fetcher, store, calendar,
                                          limit=args.backfill)
            print(f"  tried {stats['tried']}, captured {stats['found']}, "
                  f"unresolved {stats['missed']}, already held {stats['skipped']}")
            for segment, counts in sorted(stats["by_segment"].items()):
                total = counts["found"] + counts["missed"]
                rate = counts["found"] / total * 100 if total else 0.0
                print(f"    {segment:<10} captured {counts['found']:>3} of {total:>3}"
                      f"  ({rate:.0f}% resolved)")

        if args.books:
            fetcher = Fetcher(cfg)
            calendar = nse.fetch_calendar(fetcher)
            print(f"Re-fetching final subscription books for up to "
                  f"{args.books} listed issues...")
            stats = outcomes_mod.backfill_books(fetcher, store, calendar,
                                                limit=args.books,
                                                quotas=cfg.get("quotas", {}))
            print(f"  tried {stats['tried']}, captured {stats['found']}, "
                  f"empty {stats['empty']}, already held {stats['skipped']}")

        rows = store.outcomes(limit=args.limit)
        if not rows:
            print("\nNo listing outcomes recorded yet. Run with --backfill N.")
            return 0

        print(f"\nMost recent {min(len(rows), args.limit)} listings")
        print(f"  {'ISSUE':<32}{'SEG':<11}{'ISSUE PX':>10}{'OPEN':>10}"
              f"{'GAIN':>9}{'DAY1':>9}")
        print("  " + "-" * 82)
        for r in rows[:args.limit]:
            close_gain = (f"{r['gain_close']:+.1f}%"
                          if r["gain_close"] is not None else "-")
            print(f"  {(r['name'] or '')[:31]:<32}{(r['segment'] or '?'):<11}"
                  f"{r['issue_price']:>10.1f}{r['listing_open']:>10.1f}"
                  f"{r['gain_open']:>8.1f}%{close_gain:>9}")

        reg = outcomes_mod.regime(store, cfg, lookback=args.limit)
        print(f"\n  MARKET REGIME - {reg.describe()}")

        acc = outcomes_mod.accuracy(store, cfg)
        if acc["sample"]:
            print(f"\n  PREDICTION ACCURACY over {acc['sample']} matched issue(s)")
            print(f"    mean absolute error .. {acc['mean_abs_error']:.1f} pp")
            print(f"    bias ................. {acc['bias']:+.1f} pp "
                  f"({'over' if acc['bias'] > 0 else 'under'}-estimating)")
            if acc["flagged"]:
                print(f"    flagged >= bar ....... {acc['flagged_hits']}/{acc['flagged']} "
                      f"actually delivered")
        else:
            print("\n  No prediction/outcome pairs yet - the engine must see an "
                  "issue open and then list.")
    return 0


def cmd_test_notify(args, cfg) -> int:
    notifier = Notifier(cfg)
    channels = ", ".join(c.name for c in notifier.channels)
    print(f"Channels: {channels}")
    delivered = notifier.send(
        "IPO Engine test",
        "If you can see this, desktop alerts are working.\n"
        "Example: ACME Ltd 42.1x subscribed | est. listing +18.3% | "
        "retail allotment 2.10%",
        urgent=False)
    print(f"Delivered on {delivered} channel(s).")
    return 0


def cmd_alerts(args, cfg) -> int:
    with Store(cfg["paths"]["db"]) as store:
        rows = store.recent_alerts(args.limit)
        if not rows:
            print("No alerts recorded yet.")
            return 0
        for row in rows:
            level = f" @{row['level']:g}x" if row["level"] is not None else ""
            print(f"\n[{row['fired_at']}] {row['kind']}{level}\n  {row['title']}")
    return 0


def main(argv=None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(
        prog="ipo-engine",
        description="Alerts on oversubscribed Indian IPOs with an estimated "
                    "listing gain above a configured bar.")
    parser.add_argument("-c", "--config", help="path to config.json")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    once = sub.add_parser("once", help="single pass (use this from Task Scheduler)")
    once.add_argument("--quiet", action="store_true", help="score but send no alerts")
    once.add_argument("--open", action="store_true", help="open the dashboard afterwards")
    once.set_defaults(func=cmd_once)

    watch = sub.add_parser("watch", help="run continuously in the foreground")
    watch.add_argument("--interval", type=int, default=30, help="minutes (default 30)")
    watch.add_argument("--quiet", action="store_true")
    watch.set_defaults(func=cmd_watch)

    dash = sub.add_parser("dashboard", help="refresh and open the dashboard")
    dash.set_defaults(func=cmd_dashboard)

    app = sub.add_parser("app", help="open the app in your browser (start here)")
    app.add_argument("--port", type=int, default=8000)
    app.add_argument("--no-browser", action="store_true")
    app.set_defaults(func=cmd_app)

    plan = sub.add_parser("plan", help="what to apply for, given capital and PANs")
    plan.add_argument("--capital", type=float,
                      help="rupees you can block at once (default from config)")
    plan.add_argument("--pans", type=int,
                      help="PANs you can genuinely fund (default from config)")
    plan.set_defaults(func=cmd_plan)

    out = sub.add_parser("outcomes", help="capture and review realised listing gains")
    out.add_argument("--backfill", type=int, metavar="N",
                     help="fetch listing prices for up to N past issues")
    out.add_argument("--books", type=int, metavar="N",
                     help="re-fetch the final subscription book for up to N "
                          "already-captured listings")
    out.add_argument("--limit", type=int, default=20,
                     help="how many recent listings to show (default 20)")
    out.set_defaults(func=cmd_outcomes)

    test = sub.add_parser("test-notify", help="fire a test notification")
    test.set_defaults(func=cmd_test_notify)

    recent = sub.add_parser("alerts", help="show alert history")
    recent.add_argument("--limit", type=int, default=20)
    recent.set_defaults(func=cmd_alerts)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args((argv or []) + ["once"])

    _setup_logging(args.verbose)
    cfg = load_config(args.config)
    try:
        return args.func(args, cfg)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
