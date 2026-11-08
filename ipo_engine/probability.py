"""Answer the only question that matters: how likely is this to hit my goal?

A point estimate is the wrong output here. The model's typical error is about
11 percentage points against outcomes whose median is +7%, so "+13.1%"
implies a precision that does not exist. What can honestly be said is how
issues *like this one* have historically behaved.

So each issue is scored (see ``scoring.issue_score``), its nearest historical
matches are found by that score, and the answer is read straight off what
those issues actually did:

    of the 30 most similar past issues, 19 cleared 10%  ->  63%

That has three properties a fitted classifier lacks. Any goal can be answered
from one model, because the threshold is applied to outcomes rather than
baked into a fit. The percentiles come free, which is what the portfolio
simulation needs. And it explains itself without reference to coefficients.

It is not magic. All the history comes from a single eighteen-month stretch
of one market, so the neighbours stop being representative if conditions
turn - which is what the regime readout is for.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass

from .models import IPO
from .scoring import issue_score

log = logging.getLogger(__name__)

# Bands describe the probability, not a recommendation. The engine does not
# tell anyone what to do with a 68% chance.
BANDS = ((0.70, "HIGH"), (0.40, "MODERATE"), (0.15, "LOW"))
UNKNOWN = "UNKNOWN"


def band_for(probability: float | None) -> str:
    if probability is None:
        return UNKNOWN
    for threshold, label in BANDS:
        if probability >= threshold:
            return label
    return "VERY LOW"


@dataclass
class Prediction:
    """What issues resembling this one went on to do."""
    goal_pct: float
    probability: float
    sample: int
    median: float
    p25: float
    p75: float
    score: float
    worst: float
    negative_rate: float

    @property
    def band(self) -> str:
        return band_for(self.probability)

    def describe(self) -> str:
        return (f"{self.probability*100:.0f}% chance of clearing "
                f"{self.goal_pct:g}% - of the {self.sample} most similar past "
                f"issues, {round(self.probability*self.sample)} did "
                f"(median {self.median:+.1f}%, {self.negative_rate*100:.0f}% "
                f"listed down)")


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def _ipo_from_history(row) -> IPO:
    """Rebuild just enough of an IPO to score a historical final book."""
    from .models import CategoryBid

    ipo = IPO(name=row["name"] or "", segment=row["segment"] or "MAINBOARD")
    for category, column in (("TOTAL", "final_total_sub"), ("QIB", "final_qib_sub"),
                             ("NII", "final_nii_sub"), ("RETAIL", "final_retail_sub"),
                             ("BNII", "final_bnii_sub"), ("SNII", "final_snii_sub")):
        value = row[column]
        if value:
            ipo.bids[category] = CategoryBid(category, times=float(value))
    return ipo


class Predictor:
    """Holds the scored history. Build once per run, query per issue."""

    def __init__(self, cfg: dict, history: list[tuple[float, float]]):
        self.cfg = cfg
        # Sort on score ALONE. Sorting the tuples would order ties by the
        # realised gain, and since neighbour selection is a stable sort on
        # distance, every tie would then resolve toward the worst outcomes.
        # Ties are not hypothetical: the curve caps, so every issue above
        # 150x scores identically.
        self.history = sorted(history, key=lambda row: row[0])
        settings = cfg.get("probability", {})
        self.neighbours = int(settings.get("neighbours", 30))
        self.min_history = int(settings.get("min_history", 50))

    @classmethod
    def from_store(cls, store, cfg: dict) -> "Predictor":
        history: list[tuple[float, float]] = []
        for row in store.historical_pairs():
            if row["gain_open"] is None:
                continue
            score = issue_score(_ipo_from_history(row), cfg)
            if score is not None:
                history.append((score, float(row["gain_open"])))
        log.info("probability model built from %d realised listings", len(history))
        return cls(cfg, history)

    @property
    def ready(self) -> bool:
        return len(self.history) >= self.min_history

    def predict(self, ipo: IPO, goal_pct: float | None = None) -> Prediction | None:
        """Nearest-neighbour outcome distribution for one issue."""
        if not self.ready:
            return None
        score = issue_score(ipo, self.cfg)
        if score is None:
            return None
        return self.predict_score(score, goal_pct)

    def predict_score(self, score: float, goal_pct: float | None = None) -> Prediction | None:
        if not self.ready:
            return None
        if goal_pct is None:
            goal_pct = self.cfg["triggers"]["min_expected_listing_gain_pct"]

        nearest = sorted(self.history, key=lambda row: abs(row[0] - score))
        gains = [gain for _score, gain in nearest[:self.neighbours]]
        if not gains:
            return None

        cleared = sum(1 for gain in gains if gain >= goal_pct)
        return Prediction(
            goal_pct=float(goal_pct),
            probability=cleared / len(gains),
            sample=len(gains),
            median=statistics.median(gains),
            p25=_percentile(gains, 0.25),
            p75=_percentile(gains, 0.75),
            score=score,
            worst=min(gains),
            negative_rate=sum(1 for g in gains if g < 0) / len(gains),
        )


def attach(ipos: list[IPO], predictor: Predictor, cfg: dict,
           goal: float | None = None) -> int:
    """Write the odds onto each issue. Returns how many got one.

    ``goal`` can be overridden per call, which is what lets the app answer a
    different target instantly: no threshold is baked into a fit, so changing
    it only changes which outcomes count as a hit.
    """
    if goal is None:
        goal = cfg["triggers"]["min_expected_listing_gain_pct"]
    attached = 0
    for ipo in ipos:
        prediction = predictor.predict(ipo, goal)
        if prediction is None:
            continue
        ipo.clear_probability = prediction.probability
        ipo.probability_sample = prediction.sample
        ipo.outcome_median = prediction.median
        ipo.outcome_p25 = prediction.p25
        ipo.outcome_p75 = prediction.p75
        ipo.verdict = prediction.band
        ipo.reasons.append(prediction.describe())
        attached += 1
    return attached


def calibration(predictor: Predictor, holdout: list[tuple[float, float]],
                goal_pct: float, buckets: int = 5) -> list[dict]:
    """Did issues called 70% actually deliver 70% of the time?

    The test a point estimate cannot take, and the whole reason for
    reporting probabilities instead of numbers.
    """
    rows = []
    for score, actual in holdout:
        prediction = predictor.predict_score(score, goal_pct)
        if prediction is not None:
            rows.append((prediction.probability, actual >= goal_pct))
    if not rows:
        return []

    rows.sort()
    size = max(1, len(rows) // buckets)
    out = []
    for start in range(0, len(rows), size):
        chunk = rows[start:start + size]
        if len(chunk) < 2:
            continue
        out.append({
            "predicted": statistics.fmean(p for p, _hit in chunk),
            "actual": sum(1 for _p, hit in chunk if hit) / len(chunk),
            "n": len(chunk),
        })
    return out
