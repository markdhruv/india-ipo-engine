"""A local web app, so the engine has a face rather than a command line.

Python stays the back end for a concrete reason: a browser cannot fetch from
NSE or the grey-market sites at all, because neither sends CORS headers. The
scraping has to happen server-side. So this serves a page to localhost and
does the network work behind it.

Standard library only. Nothing to install beyond what the engine already
needs, which keeps "download and run" honest.

Changing the goal, confidence, capital or PAN count does **not** refetch.
Those only re-read the cached issues, because no threshold is baked into a
fit - the probability is a count over past outcomes, so a new goal is just a
different count. Refreshing live data is a separate, explicit action.
"""
from __future__ import annotations

import json
import logging
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import outcomes as outcomes_mod
from . import probability
from .engine import run_once
from .portfolio import build_plan
from .probability import Predictor
from .store import Store
from .ui import PAGE
from .util import now_ist

log = logging.getLogger(__name__)


class State:
    """Last fetched issues, so the UI stays instant between refreshes."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.ipos: list = []
        self.fetched_at: str | None = None
        self.refreshing = False
        self.error: str | None = None
        self._lock = threading.Lock()

    def refresh(self) -> None:
        with self._lock:
            if self.refreshing:
                return
            self.refreshing = True
        try:
            self.ipos = run_once(self.cfg, quiet=True)
            self.fetched_at = now_ist().strftime("%d %b %Y, %H:%M IST")
            self.error = None
        except Exception as exc:                        # pragma: no cover
            log.exception("refresh failed")
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.refreshing = False

    # ---------------- view ----------------

    def view(self, goal: float, confidence: float,
             capital: float, pans: int) -> dict:
        """Re-score the cached issues for these settings. No network."""
        cfg = json.loads(json.dumps(self.cfg))          # cheap deep copy
        cfg["triggers"]["min_expected_listing_gain_pct"] = goal
        cfg["triggers"]["min_confidence"] = confidence

        with Store(cfg["paths"]["db"]) as store:
            predictor = Predictor.from_store(store, cfg)
            if predictor.ready:
                for ipo in self.ipos:
                    ipo.reasons = [r for r in ipo.reasons
                                   if "most similar past issues" not in r]
                probability.attach(self.ipos, predictor, cfg, goal=goal)
            regime = outcomes_mod.regime(store, cfg, lookback=20)
            history = len(predictor.history)

        plan = build_plan(self.ipos, cfg, budget=capital, pans=pans)

        return {
            "fetched_at": self.fetched_at,
            "refreshing": self.refreshing,
            "error": self.error,
            "history": history,
            "model_ready": predictor.ready,
            "settings": {"goal": goal, "confidence": confidence,
                         "capital": capital, "pans": pans},
            "regime": {
                "label": regime.label, "sample": regime.sample,
                "median": regime.median_gain, "hit_rate": regime.hit_rate,
                "negative_rate": regime.negative_rate,
                "text": regime.describe(),
            },
            "issues": [_issue_json(i) for i in
                       sorted(self.ipos,
                              key=lambda x: (x.clear_probability or -1,
                                             x.expected_gain_pct or -999),
                              reverse=True)],
            "plan": _plan_json(plan),
        }


def _issue_json(ipo) -> dict:
    return {
        "name": ipo.name,
        "segment": ipo.segment,
        "exchange": ipo.exchange,
        "open": ipo.is_open,
        "closes_today": ipo.closes_today,
        "close_date": ipo.close_date.isoformat() if ipo.close_date else None,
        "band": ipo.verdict,
        "probability": ipo.clear_probability,
        "sample": ipo.probability_sample,
        "p25": ipo.outcome_p25,
        "median": ipo.outcome_median,
        "p75": ipo.outcome_p75,
        "expected_gain": ipo.expected_gain_pct,
        "gmp_pct": ipo.gmp_pct,
        "total_sub": ipo.total_subscription,
        "qib_sub": ipo.sub("QIB"),
        "retail_sub": ipo.sub("RETAIL"),
        "weakest": (f"{ipo.weakest_category} {ipo.weakest_category_sub:,.1f}x"
                    if ipo.weakest_category and ipo.weakest_category_sub is not None
                    else None),
        "allotment": ipo.allotment_prob.get("RETAIL"),
        "lot_value": ipo.capital_per_application,
        "ev": ipo.expected_value_inr,
        "confidence_score": ipo.confidence,
        "reasons": ipo.reasons,
    }


def _plan_json(plan) -> dict:
    return {
        "budget": plan.budget,
        "pans": plan.pans,
        "deployed": plan.deployed,
        "idle": plan.idle,
        "applications": plan.application_count,
        "p_none": plan.p_none,
        "p_any": plan.p_any,
        "expected_allotments": plan.expected_allotments,
        "expected_inr": plan.expected_inr,
        "return_pct": plan.return_on_budget_pct,
        "distribution": plan.distribution[:6],
        "rows": [{"name": a.ipo.name, "pans": a.pans, "lot": a.lot_value,
                  "capital": a.capital, "odds": a.odds, "gain": a.gain_pct,
                  "ev": a.expected_inr} for a in plan.applications],
        "skipped": [{"name": s.ipo.name, "reason": s.reason,
                     "lot": s.ipo.capital_per_application}
                    for s in plan.skipped_tail()],
    }


def _make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass                                        # keep the console clean

        def _send(self, body: bytes, content_type: str, status: int = 200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):                               # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)

            def number(name, default, cast=float):
                try:
                    return cast(query.get(name, [default])[0])
                except (TypeError, ValueError):
                    return default

            if parsed.path in ("/", "/index.html"):
                self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
                return

            if parsed.path == "/api/state":
                triggers = state.cfg["triggers"]
                capital = state.cfg.get("capital", {})
                payload = state.view(
                    goal=number("goal", triggers["min_expected_listing_gain_pct"]),
                    confidence=number("confidence",
                                      triggers.get("min_confidence", 0.6)),
                    capital=number("capital", capital.get("budget_inr", 100000)),
                    pans=number("pans", capital.get("pan_count", 1), int))
                self._send(json.dumps(payload, default=str).encode("utf-8"),
                           "application/json")
                return

            if parsed.path == "/api/refresh":
                threading.Thread(target=state.refresh, daemon=True).start()
                self._send(b'{"started": true}', "application/json")
                return

            self._send(b"not found", "text/plain", status=404)

    return Handler


def serve(cfg: dict, *, port: int = 8000, open_browser: bool = True) -> int:
    state = State(cfg)

    print("Fetching live IPO data (takes about a minute the first time)...")
    state.refresh()
    if state.error:
        print(f"  live fetch failed: {state.error}")
        print("  the app will still open and show stored data.")

    server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(state))
    url = f"http://127.0.0.1:{port}/"
    print(f"\n  IPO engine running at {url}")
    print("  Press Ctrl+C to stop.\n")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0
