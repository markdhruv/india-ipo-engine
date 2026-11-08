"""One pass of the engine: fetch, merge, score, alert, persist, render."""
from __future__ import annotations

import logging
from datetime import timedelta

from . import alerts as alerts_mod
from . import dashboard
from . import probability
from .probability import Predictor
from .http import Fetcher
from .merge import merge
from .models import IPO
from .notify import Notifier
from .scoring import score_all
from .sources import gmp, nse
from .store import Store
from .util import today_ist

log = logging.getLogger(__name__)

# How far past its close date an issue stays interesting. Allotment lands a day
# or two after close and listing about three days after, so a week covers it.
STALE_AFTER_DAYS = 7
# Only issues opening within this window are worth fetching bid data for.
LOOKAHEAD_DAYS = 21


def _relevant(ipo: IPO) -> bool:
    """In-flight or just-finished issues only - not 1,400 historical rows."""
    today = today_ist()
    if ipo.close_date and ipo.close_date >= today - timedelta(days=STALE_AFTER_DAYS):
        return True
    if ipo.open_date and today <= ipo.open_date <= today + timedelta(days=LOOKAHEAD_DAYS):
        return True
    return False


def run_once(cfg: dict, *, notifier: Notifier | None = None,
             store: Store | None = None, quiet: bool = False) -> list[IPO]:
    """Fetch everything, score it, fire due alerts, write the dashboard."""
    owns_store = store is None
    store = store or Store(cfg["paths"]["db"])
    notifier = notifier or Notifier(cfg)
    fetcher = Fetcher(cfg)

    try:
        segments = cfg.get("segments", {})
        wanted = {s for s, on in (("MAINBOARD", segments.get("mainboard", True)),
                                  ("SME", segments.get("sme", True))) if on}

        calendar = [i for i in nse.fetch_calendar(fetcher) if _relevant(i)]
        log.info("%d in-flight issues from NSE calendar", len(calendar))

        for ipo in calendar:
            nse.enrich_with_bids(fetcher, ipo, cfg.get("quotas", {}))

        gmp_rows = gmp.fetch_all(fetcher)
        log.info("%d GMP rows", len(gmp_rows))

        ipos = [i for i in merge(calendar, gmp_rows) if i.segment in wanted]
        ipos = [i for i in ipos if _relevant(i) or i.is_open]
        ipos = score_all(ipos, cfg)

        # Odds from realised outcomes, where enough history exists. This
        # replaces the verdict with a probability band; without history the
        # legacy gain-based verdict stands in.
        predictor = Predictor.from_store(store, cfg)
        if predictor.ready:
            attached = probability.attach(ipos, predictor, cfg)
            log.info("odds attached to %d of %d issues", attached, len(ipos))
        else:
            log.info("only %d realised listings; need %d before quoting odds",
                     len(predictor.history), predictor.min_history)

        # Resolve identity before anything keyed on it runs, so history and
        # alert de-duplication survive an issue moving between sources.
        for ipo in ipos:
            store.assign_identity(ipo)
            store.save_snapshot(ipo)

        due = alerts_mod.build(ipos, cfg, store)
        fired = 0 if quiet else alerts_mod.dispatch(due, notifier, store)
        if quiet and due:
            log.info("quiet mode: %d alert(s) suppressed", len(due))

        if cfg.get("notify", {}).get("write_dashboard", True):
            path = dashboard.write(ipos, cfg, fired)
            log.info("dashboard written to %s", path)

        return ipos
    finally:
        if owns_store:
            store.close()
