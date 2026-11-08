#!/usr/bin/env python
"""Calibrate the scoring weights against realised listing outcomes.

Two halves of the model calibrate differently.

The **subscription side can be measured now**. NSE retains an issue's final
book after it lists, so pairing that with the realised listing gain gives a
historical dataset hundreds of issues deep - see ``subscription``.

The **GMP shrink cannot**. No free source publishes a retrospective series of
grey-market premium *as quoted on the closing day*, so that input only exists
if it was recorded while the issue was open. ``calibrate`` therefore fits it
from pairs this engine accumulates going forward, and until there are enough
the shipped shrink factors remain informed priors.

Usage:
    python backtest.py status
    python backtest.py subscription
    python backtest.py record "HGS (India)" 148.5
    python backtest.py calibrate
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys

from ipo_engine.store import Store
from ipo_engine.util import ROOT, force_utf8, load_config, norm_name

MIN_PAIRS_FOR_CALIBRATION = 30


def _shrunk(gmp_pct: float, shrink: float, cfg: dict) -> float:
    reversion = min(cfg["gmp_mean_reversion_cap"],
                    abs(gmp_pct) / cfg["gmp_mean_reversion_scale"])
    return shrink * gmp_pct * (1.0 - reversion)


def cmd_status(args, cfg) -> int:
    with Store(cfg["paths"]["db"]) as store:
        snaps = store.conn.execute(
            "SELECT COUNT(*) c, COUNT(DISTINCT ipo_key) k FROM snapshots").fetchone()
        outcomes = store.conn.execute(
            "SELECT COUNT(*) c FROM outcomes WHERE listing_gain IS NOT NULL").fetchone()
        pairs = store.calibration_rows()

    print(f"Snapshots recorded ...... {snaps['c']} across {snaps['k']} issues")
    print(f"Listing outcomes ........ {outcomes['c']}")
    print(f"Usable calibration pairs  {len(pairs)}")
    print(f"Needed to calibrate ..... {MIN_PAIRS_FOR_CALIBRATION}")
    if len(pairs) < MIN_PAIRS_FOR_CALIBRATION:
        short = MIN_PAIRS_FOR_CALIBRATION - len(pairs)
        print(f"\nNot enough history yet - {short} more listed issues needed.")
        print("The engine records a snapshot on every run; add each listing price")
        print('with:  python backtest.py record "<company>" <listing price>')
    return 0


def cmd_record(args, cfg) -> int:
    key = norm_name(args.name)
    with Store(cfg["paths"]["db"]) as store:
        row = store.conn.execute(
            """SELECT ipo_key, name, payload FROM snapshots
               WHERE ipo_key = ? ORDER BY captured_at DESC LIMIT 1""",
            (key,)).fetchone()
        if row is None:
            row = store.conn.execute(
                """SELECT ipo_key, name, payload FROM snapshots
                   WHERE name LIKE ? ORDER BY captured_at DESC LIMIT 1""",
                (f"%{args.name}%",)).fetchone()
        if row is None:
            print(f"No snapshot found for {args.name!r}. "
                  "The engine must have seen the issue while it was open.")
            return 1

        issue_price = json.loads(row["payload"]).get("price")
        if not issue_price:
            print(f"No issue price stored for {row['name']}.")
            return 1

        store.record_outcome(row["ipo_key"], row["name"], issue_price, args.listing_price)
        gain = (args.listing_price - issue_price) / issue_price * 100
        print(f"Recorded {row['name']}: {issue_price} -> {args.listing_price} "
              f"({gain:+.2f}%)")
    return 0


def cmd_calibrate(args, cfg) -> int:
    scoring = cfg["scoring"]
    with Store(cfg["paths"]["db"]) as store:
        pairs = [r for r in store.calibration_rows() if r["gmp_pct"] is not None]

    if len(pairs) < MIN_PAIRS_FOR_CALIBRATION:
        print(f"Only {len(pairs)} calibration pairs; need "
              f"{MIN_PAIRS_FOR_CALIBRATION}.")
        print("Run 'python backtest.py status' for what is missing.")
        return 1

    results = {}
    for segment in ("MAINBOARD", "SME"):
        rows = [r for r in pairs if r["segment"] == segment]
        if len(rows) < 10:
            print(f"{segment}: only {len(rows)} pairs, skipping.")
            continue

        best, best_err = None, float("inf")
        for step in range(20, 121):                    # shrink 0.20 .. 1.20
            shrink = step / 100.0
            err = statistics.fmean(
                abs(_shrunk(r["gmp_pct"], shrink, scoring) - r["listing_gain"])
                for r in rows)
            if err < best_err:
                best, best_err = shrink, err

        current = (scoring["gmp_shrink_sme"] if segment == "SME"
                   else scoring["gmp_shrink_mainboard"])
        current_err = statistics.fmean(
            abs(_shrunk(r["gmp_pct"], current, scoring) - r["listing_gain"])
            for r in rows)
        results[segment] = (best, best_err, current, current_err, len(rows))

        print(f"\n{segment}  ({len(rows)} listed issues)")
        print(f"  current shrink {current:.2f} -> mean abs error {current_err:5.2f} pp")
        print(f"  fitted  shrink {best:.2f} -> mean abs error {best_err:5.2f} pp")

    # Did the gain bar actually select winners?
    gate = cfg["triggers"]["min_expected_listing_gain_pct"]
    flagged = [r for r in pairs if (r["expected_gain"] or -999) >= gate]
    if flagged:
        hits = sum(1 for r in flagged if r["listing_gain"] >= gate)
        median = statistics.median(r["listing_gain"] for r in flagged)
        print(f"\nGate >= {gate:g}%: {len(flagged)} flagged, {hits} reached it "
              f"({hits/len(flagged)*100:.0f}% hit rate), median outcome "
              f"{median:+.1f}%")

    if results and args.write:
        for segment, (best, *_rest) in results.items():
            key = "gmp_shrink_sme" if segment == "SME" else "gmp_shrink_mainboard"
            cfg["scoring"][key] = best
        (ROOT / "config.json").write_text(
            json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        print("\nconfig.json updated with fitted shrink factors.")
    elif results:
        print("\nRe-run with --write to apply the fitted values.")
    return 0


def _median(values):
    return statistics.median(values) if values else None


def _spearman(xs, ys) -> float | None:
    """Rank correlation - listing gains are far too skewed for Pearson."""
    n = len(xs)
    if n < 5:
        return None

    def ranks(values):
        order = sorted(range(n), key=lambda i: values[i])
        out = [0.0] * n
        i = 0
        while i < n:                      # average ties
            j = i
            while j + 1 < n and values[order[j + 1]] == values[order[i]]:
                j += 1
            shared = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                out[order[k]] = shared
            i = j + 1
        return out

    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else None


def _bucket_report(rows, field, label, edges, bar):
    print(f"\n  Listing gain by {label}")
    print(f"    {'bucket':<16}{'n':>5}{'median':>10}{'mean':>10}"
          f"{'>= bar':>9}{'negative':>10}")
    for low, high in zip([0] + edges, edges + [float("inf")]):
        group = [r for r in rows if r[field] is not None
                 and low <= r[field] < high]
        if not group:
            continue
        gains = [r["gain_open"] for r in group]
        name = f"{low:g}-{high:g}x" if high != float("inf") else f"{low:g}x+"
        print(f"    {name:<16}{len(group):>5}{_median(gains):>9.1f}%"
              f"{statistics.fmean(gains):>9.1f}%"
              f"{sum(1 for g in gains if g >= bar)/len(gains)*100:>8.0f}%"
              f"{sum(1 for g in gains if g < 0)/len(gains)*100:>9.0f}%")


def cmd_subscription(args, cfg) -> int:
    """Does the subscription book actually predict the listing gain?"""
    bar = cfg["triggers"]["min_expected_listing_gain_pct"]
    with Store(cfg["paths"]["db"]) as store:
        rows = store.historical_pairs()

    if len(rows) < 20:
        print(f"Only {len(rows)} issues with both a final book and a listing "
              f"gain. Run:  python run.py outcomes --backfill 200 --books 200")
        return 1

    gains = [r["gain_open"] for r in rows]
    print(f"{len(rows)} issues with a final book and a realised listing gain")
    print(f"  median {_median(gains):+.1f}%   mean {statistics.fmean(gains):+.1f}%   "
          f"{sum(1 for g in gains if g >= bar)/len(gains)*100:.0f}% cleared {bar:g}%")

    print("\n  Rank correlation with listing gain")
    for field, label in (("final_total_sub", "total subscription"),
                         ("final_qib_sub", "QIB"),
                         ("final_nii_sub", "NII"),
                         ("final_retail_sub", "retail")):
        pairs = [(r[field], r["gain_open"]) for r in rows if r[field] is not None]
        rho = _spearman([p[0] for p in pairs], [p[1] for p in pairs])
        if rho is not None:
            print(f"    {label:<20} rho {rho:+.3f}   (n={len(pairs)})")

    _bucket_report(rows, "final_total_sub", "total subscription",
                   [1, 5, 20, 50, 100], bar)
    _bucket_report(rows, "final_qib_sub", "QIB subscription",
                   [1, 5, 20, 50, 100], bar)

    # The composition rules were reasoned, not measured. Test them.
    print("\n  Composition rules, measured")
    lagging, healthy = [], []
    for r in rows:
        total, qib = r["final_total_sub"], r["final_qib_sub"]
        if total is None or qib is None or total < 10:
            continue
        (lagging if qib < total * 0.25 else healthy).append(r["gain_open"])
    if lagging and healthy:
        print(f"    QIB lagging (<25% of a 10x+ book): n={len(lagging):<4} "
              f"median {_median(lagging):+.1f}%")
        print(f"    QIB healthy                      : n={len(healthy):<4} "
              f"median {_median(healthy):+.1f}%")
    else:
        print(f"    QIB lag: too few cases to judge "
              f"(lagging={len(lagging)}, healthy={len(healthy)})")

    levered, normal = [], []
    for r in rows:
        qib, nii = r["final_qib_sub"], r["final_nii_sub"]
        if not qib or nii is None or nii < 40:
            continue
        (levered if nii >= qib * 10 else normal).append(r["gain_open"])
    if levered and normal:
        print(f"    NII >= 10x QIB (NII 40x+)        : n={len(levered):<4} "
              f"median {_median(levered):+.1f}%")
        print(f"    NII normal                       : n={len(normal):<4} "
              f"median {_median(normal):+.1f}%")
    else:
        print(f"    NII leverage: too few cases to judge "
              f"(levered={len(levered)}, normal={len(normal)})")
    return 0


def _fit_curve(rows, edges=(1, 5, 20, 50, 100)):
    """Bucket medians as curve points, at the geometric midpoint of each."""
    import math

    buckets: dict[int, list[float]] = {}
    for r in rows:
        value = r["final_total_sub"]
        index = next((i for i, e in enumerate(edges) if value < e), len(edges))
        buckets.setdefault(index, []).append(r["gain_open"])

    bounds = [0.5] + list(edges) + [edges[-1] * 1.5]
    points = []
    for index, gains in sorted(buckets.items()):
        low, high = bounds[index], bounds[index + 1]
        points.append([math.sqrt(max(low, 0.5) * high), statistics.median(gains)])
    return points


def cmd_validate(args, cfg) -> int:
    """Does the rebalanced curve beat what it replaced, out of sample?

    The shipped curve was fitted on every issue, so scoring it against those
    same issues would flatter it. This refits on the older 70% and scores the
    newer 30%, using the engine's own ``subscription_estimate`` rather than a
    reimplementation, so what is measured is what actually ships.
    """
    import copy

    from ipo_engine.models import IPO, CategoryBid
    from ipo_engine.scoring import subscription_estimate

    bar = cfg["triggers"]["min_expected_listing_gain_pct"]
    with Store(cfg["paths"]["db"]) as store:
        rows = [r for r in store.historical_pairs()
                if r["final_total_sub"] and r["gain_open"] is not None]
    if len(rows) < 40:
        print(f"Only {len(rows)} pairs; need 40 to split meaningfully.")
        return 1

    rows.sort(key=lambda r: r["listing_date"])
    split = int(len(rows) * 0.7)
    train, test = rows[:split], rows[split:]
    print(f"train {len(train)} to {train[-1]['listing_date']}   "
          f"test {len(test)} from {test[0]['listing_date']}")

    fitted = copy.deepcopy(cfg)
    fitted["scoring"]["subscription_curve"]["points"] = _fit_curve(train)

    def estimate(row, config):
        ipo = IPO(name=row["name"], segment=row["segment"] or "MAINBOARD")
        ipo.bids["TOTAL"] = CategoryBid("TOTAL", times=row["final_total_sub"])
        value, _note = subscription_estimate(ipo, config)
        return value

    train_median = statistics.median([r["gain_open"] for r in train])
    # What the old scheme could do at its very best: a flat few points.
    old_ceiling = 8.5

    results = {}
    for label, predictor in (
            ("rebalanced curve (refit on train)", lambda r: estimate(r, fitted)),
            ("shipped curve (fitted on all)", lambda r: estimate(r, cfg)),
            ("predict the median", lambda r: train_median),
            ("old scheme ceiling", lambda r: old_ceiling)):
        preds, acts = [], []
        for r in test:
            value = predictor(r)
            if value is None:
                continue
            preds.append(value)
            acts.append(r["gain_open"])
        mae = statistics.fmean(abs(p - a) for p, a in zip(preds, acts))
        bias = statistics.fmean(p - a for p, a in zip(preds, acts))
        hits = sum(1 for p, a in zip(preds, acts) if (p >= bar) == (a >= bar))
        results[label] = (mae, bias, hits / len(acts) * 100)

    print(f"\n  {'model':<36}{'MAE':>8}{'bias':>9}{'bar call':>11}")
    print("  " + "-" * 64)
    for label, (mae, bias, acc) in results.items():
        print(f"  {label:<36}{mae:7.1f}p{bias:+8.1f}p{acc:10.0f}%")

    clean = results["rebalanced curve (refit on train)"][0]
    base = results["predict the median"][0]
    print(f"\n  Out-of-sample improvement over the baseline: "
          f"{(base - clean) / base * 100:.0f}%")
    print("  Bar call = how often it agrees with reality on clearing the bar.")
    return 0


def cmd_probability(args, cfg) -> int:
    """Is the probability model honest? Out-of-sample, and calibrated.

    Accuracy alone would not settle it. A model that says 70% must be right
    about 70% of the time, or the number is decoration.
    """
    from ipo_engine.probability import Predictor, calibration
    from ipo_engine.probability import _ipo_from_history
    from ipo_engine.scoring import issue_score

    goal = args.goal or cfg["triggers"]["min_expected_listing_gain_pct"]
    confidence = cfg["triggers"].get("min_confidence", 0.6)

    with Store(cfg["paths"]["db"]) as store:
        rows = [r for r in store.historical_pairs() if r["gain_open"] is not None]

    scored = []
    for row in rows:
        score = issue_score(_ipo_from_history(row), cfg)
        if score is not None:
            scored.append((row["listing_date"], score, float(row["gain_open"])))
    if len(scored) < 60:
        print(f"Only {len(scored)} scored issues; need 60 to split.")
        return 1

    scored.sort()
    split = int(len(scored) * 0.7)
    train = [(s, g) for _d, s, g in scored[:split]]
    test = [(s, g) for _d, s, g in scored[split:]]
    print(f"train {len(train)}   test {len(test)}   goal {goal:g}%   "
          f"confidence {confidence:.0%}")

    predictor = Predictor(cfg, train)
    if not predictor.ready:
        print(f"Training set below min_history ({predictor.min_history}).")
        return 1

    base_rate = sum(1 for _s, g in test if g >= goal) / len(test)
    print(f"\n  base rate in the test period: {base_rate:.0%} cleared {goal:g}%")

    print(f"\n  Calibration - does '70%' mean 70%?")
    print(f"    {'predicted':>10}{'actual':>9}{'n':>5}")
    rows_cal = calibration(predictor, test, goal)
    errors = []
    for bucket in rows_cal:
        errors.append(abs(bucket["predicted"] - bucket["actual"]))
        print(f"    {bucket['predicted']:9.0%}{bucket['actual']:9.0%}{bucket['n']:5d}")
    if errors:
        print(f"    mean calibration error {statistics.fmean(errors):.1%}")

    # Acting on it: what would the engine have told you to do?
    flagged = [(s, g) for s, g in test
               if (predictor.predict_score(s, goal) or
                   type("x", (), {"probability": 0})).probability >= confidence]
    print(f"\n  Acting at >= {confidence:.0%} confidence")
    if flagged:
        hits = sum(1 for _s, g in flagged if g >= goal)
        print(f"    flagged {len(flagged)} of {len(test)} issues; "
              f"{hits} cleared the goal ({hits/len(flagged):.0%})")
        print(f"    versus {base_rate:.0%} if you applied to everything")
        print(f"    median outcome when flagged: "
              f"{statistics.median([g for _s, g in flagged]):+.1f}%")
    else:
        print("    nothing cleared the confidence bar in the test period")
    return 0


def main(argv=None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    status = sub.add_parser("status", help="how much calibration data exists")
    status.set_defaults(func=cmd_status)

    record = sub.add_parser("record", help="record a realised listing price")
    record.add_argument("name")
    record.add_argument("listing_price", type=float)
    record.set_defaults(func=cmd_record)

    subs = sub.add_parser("subscription",
                          help="measure how well the book predicts the listing")
    subs.set_defaults(func=cmd_subscription)

    val = sub.add_parser("validate",
                         help="score the shipped curve out of sample")
    val.set_defaults(func=cmd_validate)

    prob = sub.add_parser("probability",
                          help="check the probability model is calibrated")
    prob.add_argument("--goal", type=float,
                      help="listing gain to test against (default from config)")
    prob.set_defaults(func=cmd_probability)

    calibrate = sub.add_parser("calibrate", help="fit the GMP shrink factors")
    calibrate.add_argument("--write", action="store_true",
                           help="write fitted values back to config.json")
    calibrate.set_defaults(func=cmd_calibrate)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args((argv or []) + ["status"])
    return args.func(args, load_config())


if __name__ == "__main__":
    sys.exit(main())
