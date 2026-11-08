# India IPO Oversubscription & Listing-Gain Engine

Watches every open mainboard and SME IPO in India, tracks category-wise
oversubscription live, estimates the listing-day gain, and raises a Windows
desktop alert when an issue clears a configurable bar (default **+10%**).

It answers three questions separately, because merging them is how people lose
money on IPOs:

| Question | Field |
|---|---|
| If allotted, how much is this likely to gain on listing? | `expected_gain_pct` |
| What are the odds of being allotted at all? | `allotment_prob` |
| So what is one application actually worth? | `expected_value_inr` |

A worked example from the first live run:

> **Maxwell Engineering** — 491x subscribed, GMP +66%, estimated listing **+45.8%**.
> Retail allotment odds: **0.36%**. Capital blocked per application: ₹1.26 L.
> **Expected value: ₹208.**

The headline number and the useful number are not the same number. The engine
always shows both.

---

## Quick start

**Open this folder in Claude and say "run it".** Claude reads `CLAUDE.md`,
sets up the environment and starts the app. Nothing else is needed.

Or run it yourself:

```bash
start.bat
```

```bash
./start.sh
```

Either creates the virtual environment on first run, fetches live data (about
a minute) and opens the app at <http://127.0.0.1:8000/>. Goal, confidence,
capital and PAN count are all set in the browser, and changing them is
instant — no refetch, because no threshold is baked into a fit.

Python does the fetching for a concrete reason: a browser cannot read NSE or
the grey-market sites at all, since neither sends CORS headers. So the repo
ships a local server rather than a static page.

The database of ~190 realised listings is committed, so the odds model works
on first launch rather than after a day of collection.

| Command | What it does |
|---|---|
| `run.py app` | The app — start here. |
| `run.py plan --capital 100000 --pans 6` | What to actually apply for, and the odds it produces. |
| `run.py outcomes --backfill 200` | Capture realised listing prices; report market regime and prediction accuracy. |
| `run.py once` | One pass: fetch, score, alert, write dashboard. Use this from Task Scheduler. |
| `run.py once --quiet` | Score and render, send no alerts. |
| `run.py watch --interval 30` | Stay running, re-check every 30 minutes. |
| `run.py dashboard` | Refresh and open the dashboard in a browser. |
| `run.py test-notify` | Fire a test toast to confirm alerts work. |
| `run.py alerts` | Show what has already been sent. |

The dashboard is written to `data/dashboard.html` and opens offline.

## Scheduling

```bash
powershell -ExecutionPolicy Bypass -File install_task.ps1
```

Registers a task running every 30 minutes from 09:30 to 17:30, using
`pythonw.exe` so no console window appears. Remove it with `-Remove`.

> **The one real limitation.** Desktop toasts only reach you while this PC is
> awake and you are logged on. The UPI mandate cutoff on an IPO's closing day
> is around **5 PM IST**, and a missed cutoff cannot be undone. If the machine
> sleeps during market hours, you will miss alerts. `notify.py` is a thin seam
> for exactly this reason — adding Telegram is one `Channel` subclass.

---

## The headline is a probability, not a number

The model's typical error is ~11 percentage points against outcomes whose
median is +7%. So "+13.1%" implies a precision that does not exist. What can
honestly be said is how issues *like this one* have behaved:

```
Maxwell Engineering    HIGH      87% chance of clearing 10%
Bombay Coated          LOW       33%      similar issues: -1% to +18%
```

Each issue is scored from its book, its ~30 nearest historical matches are
found by that score, and the answer is read off what those issues actually
did. No threshold is baked into a fit, so **any goal is answerable instantly**:

```bash
python backtest.py probability --goal 20
```

### You set two things

| setting | meaning | default |
|---|---|---|
| `min_expected_listing_gain_pct` | your goal | 10% |
| `min_confidence` | how sure before alerting | 0.60 |

Base rate is 43%, so 0.60 means clearly better than average. Out-of-sample,
on the newer 30% of history:

| goal | flagged | cleared it | base rate | median outcome |
|---|---|---|---|---|
| 10% | 18 of 47 | **89%** | 45% | +29.2% |
| 20% | 7 of 47 | **100%** | 30% | +65.3% |

Raise the goal for a bigger payoff from fewer issues; raise the confidence
for fewer, stronger alerts.

### Is it honest?

The test a point estimate cannot take — of the issues called 70%, did ~70%
deliver?

```
 predicted   actual    n
       11%       0%    9
       13%       0%    9
       32%      44%    9
       63%      78%    9
       75%      89%    9
```

Directionally sound and, in the upper bands, **under**-confident: it says 63%
and delivers 78%. That is the safer error, and it reflects a test period that
ran hotter than the training period.

Bands (`HIGH` ≥70%, `MODERATE` 40–70%, `LOW` 15–40%, `VERY LOW` <15%)
describe the odds. They are not recommendations.

> GMP is deliberately **excluded from the score**. No historical GMP exists to
> match live issues against, so including it would make them unmatchable
> against the only outcomes available. It still informs the point estimate —
> it just has no say in which past issues this one resembles. Once enough
> live GMP readings have been recorded alongside outcomes, it can join.

## How it decides

### Expected listing gain

```
expected_gain = shrink(GMP%) + subscription_adjustment - risk_discount
```

**GMP is shrunk, never taken at face value.** Grey Market Premium is the
strongest short-term predictor available, but it systematically overstates the
realised gain, and it overstates it *more* at the top end — so the shrink is
concave, not linear. Mainboard keeps 80%, SME 70% (SME grey markets are
thinner and easier to move), with a further mean-reversion haircut that grows
with the premium.

**Subscription is the other estimate, not a nudge**, and the two are
**averaged, never added** — summing would double-count the demand both
reflect. The curve maps final total subscription to expected gain, fitted on
156 issues that listed between May 2025 and Nov 2026 (rank correlation
+0.715):

```
<1x  0.0%  |  3x  +1.4%  |  10x  +6.8%  |  32x  +13.3%  |  70x  +23.5%  |  150x+  +36.9%
```

Medians, not means: at 100x+ the mean is +50% against a +37% median because a
few issues double.

Out-of-sample — curve refit on the older 70%, scored on the newer 30%:

| model | MAE | calls the bar right |
|---|---|---|
| **rebalanced curve** | **11.4pp** | **79%** |
| predict the median | 16.0pp | 55% |
| what the old tier scheme could reach | 15.9pp | 55% |

The scheme this replaced capped subscription's entire contribution at ~8pp
while the real spread is ~37pp, so a strong book could never clear the bar on
its own. It now can.

**A partial book is down-weighted.** The curve is fitted on *final*
subscription, and Indian retail and HNI money arrives on the closing day, so
a day-one book would otherwise drag every estimate to zero. Subscription
earns its weight as the book fills (25% → 50% → full on the closing day).

**Composition is judged separately from size**, because the headline total is
the quota-weighted mean of the categories and can be carried entirely by one
of them. Three failure shapes are penalised:

| Shape | Why it matters |
|---|---|
| QIB lagging a big book (< 25% of total, total ≥ 10x) | Institutions are the price-sensitive, diligence-doing money, and their anchor tranche is lock-in bound. Their absence removes both the quality signal and a brake on listing-day supply. |
| NII ≥ 5x QIB and ≥ 50x | Large HNI books are substantially funded by short-term NBFC borrowing repayable within days. That money is structurally obliged to sell into the listing. |
| Any major leg below 1x | A category that never filled, which a healthy headline easily hides. |

Only QIB, NII and retail count as "major". An unfilled employee or
shareholder reservation says nothing about the issue. A quota-derived QIB
ratio is our own arithmetic, not a reported fact, so it never triggers a
penalty.

**These were verified within subscription buckets, and that step changed the
answer.** Pooled across all 156 issues, NII leverage shows a gap of **0.0pp**
and looks worthless. Held at constant book size it is worth **−10 to −25pp**:

```
bucket      flagged   median    others   median      gap
5-20x             2   -17.3%        20    +7.6%   -24.9p
20-50x            6    +4.8%        26   +15.5%   -10.7p
50-100x           3   +15.0%        30   +30.4%   -15.4p
POOLED           12    +9.1%       122    +9.1%    -0.0p   <- hides it entirely
```

A textbook Simpson's paradox: leveraged books are also *large* books, and
size is already priced by the curve. QIB lag behaves the same way, from
−1.5pp at 1–5x up to −16.8pp at 100x+. The penalties applied are far smaller
than the measured separation, because the buckets where the effect is biggest
hold only one to three issues each.

Two issues with an identical 60x headline therefore separate:

```
QIB 40x, NII 90x, retail 55x   -> +14.8%  ACTIONABLE
QIB  2x, NII 90x, retail 120x  ->  +6.8%  WATCH   (below the 10% bar)
```

The dashboard shows the **weakest leg** on every row for this reason. Note
that breadth is not the same as depth: every category sitting at 1.1x is
uniformly covered and still predicts nothing good.

#### Thresholds are per segment

The two books have different normal ranges, so one threshold would either tax
healthy mainboard issues or wave through SME outliers. Measured across live
issues:

| | Mainboard | SME |
|---|---|---|
| NII / QIB observed | 4.5 – 7.7 | 0.8 – 3.6 |
| QIB / total observed | 0.36 – 0.58 | 0.45 – 5.22 |

Deep leveraged HNI funding is a mature *mainboard* market, so mainboard
tolerates far more NII skew before it means anything. Institutions declining
a mainboard issue is meanwhile the louder failure, so it is penalised harder.

| | Mainboard | SME |
|---|---|---|
| QIB lag fires below | 25% of total (total ≥ 10x) | 20% of total (total ≥ 20x) |
| QIB lag penalty | −4.0 | −2.5 |
| NII leverage fires at | ≥ 10× QIB, NII ≥ 40x | ≥ 6× QIB, NII ≥ 75x |
| NII leverage penalty | −2.0 | −1.5 |

The leverage flag is suppressed when QIB is independently deep (≥ 25x
mainboard, ≥ 60x SME) — heavy NII alongside a strong institutional book is
enthusiasm, not a warning. The same trap book therefore costs −8.0pp on
mainboard and −6.0pp on SME.

Every threshold sits clear of the observed healthy range, and tests assert
that no live book in either segment trips one. The sample behind these
numbers is small; revisit once `backtest.py` has outcomes.

**Risk is deducted.** SME issues take a flat discount for illiquidity; very
large issues take one for absorbing their own demand.

### The application plan

Two inputs decide everything (`config.json` → `capital`, overridable per run):

- **`budget_inr`** — what you can block at once. This gates **reach**: SME lots
  run ₹1.1–1.5 lakh against roughly ₹15,000 on the mainboard, so ₹1 lakh
  cannot see the SME market at all.
- **`pan_count`** — PANs that can genuinely fund an application. This gates
  **odds**, and it is the stronger lever.

```
applications to issue i = min( pans , floor(capital_remaining / lot_value_i) )
```

#### Never bid more than one lot

When a retail category is oversubscribed, SEBI allots the **minimum lot by a
draw of lots**. Bidding five lots gives exactly the same chance as bidding
one, while blocking five times the capital. Every application the planner
produces is one lot; the way to use more money is more PANs and more issues,
never a bigger bid. A PAN may apply to many different issues but only once
per issue, so `pans` caps applications *per issue*, not in total.

#### What that does to the odds

Same ₹1 lakh, same issues, differing only in PAN count:

```
1 PAN   2 applications   P(no allotment) 63.7%   E[value]  ₹716  (0.72% of budget)
6 PANs  6 applications   P(no allotment)  9.5%   E[value] ₹3,734 (3.73% of budget)
```

And a result worth sitting with — **more capital lowers your percentage
return**, because deal flow, not money, is the binding constraint:

```
₹1 L / 6 PANs  ->  3.73% of budget
₹3 L / 6 PANs  ->  1.43%
₹5 L / 6 PANs  ->  0.97%
```

The extra money chases worse odds and then sits idle. PANs only beat capital
while few issues are open; with twenty concurrent issues capital binds
instead, and both cases are pinned by tests.

Applications are ranked by **expected return per rupee blocked**, which is the
only sane ordering once capital binds. The consequence is that SME almost
never wins — 0.36% odds on a ₹1.26 lakh lot loses to 5.65% on ₹15,000 whatever
the SME headline says — so unreachable issues are listed under *out of reach*
rather than silently dropped.

Outcomes are reported as a distribution, not a point estimate. Each
application is an independent draw with its own probability, so the count is
Poisson-binomial and computed exactly rather than approximated.

> **A caveat the software cannot solve.** ASBA blocks funds in the
> *applicant's own* bank account, linked to their own PAN and demat. Splitting
> a budget across PANs means the money genuinely sitting in those people's
> accounts beforehand. Gains are taxed in each holder's hands, and transfers
> to a spouse or minor child can be clubbed back to you under Section 64. Set
> `pan_count` to PANs you can actually fund.

### Allotment probability

When a category is oversubscribed, SEBI requires allotment of at least the
minimum lot by a **draw of lots**. So the honest estimate is:

```
P(allotment) = lots available / applications received
```

Not `1 / subscription`, which silently assumes every applicant bid one lot.
The exchange's per-category application counts make the real calculation
possible, and no other source publishes them.

Minimum application size is modelled per category: retail is one lot, sNII is
however many whole lots clear ₹2 lakh, bNII ₹10 lakh.

### Expected value

```
capital   = min_application_shares x price
EV        = P(allotment) x capital x expected_gain%
```

### Confidence

A 0–100 data-quality score, not a conviction score. It falls when GMP is
missing, when subscription did not come from the exchange, when application
counts are absent (so odds are an approximation), and when a GMP of exactly
zero probably means "no quote" rather than "no premium".

---

## Where the data comes from

| Source | Provides | Notes |
|---|---|---|
| NSE `public-past-issues` | Official calendar, mainboard + SME | ~1,500 rows, one request |
| NSE `ipo-detail` | Category bids **and application counts** | Application counts are unique to NSE and drive allotment odds |
| investorgain `live-ipo-gmp` | GMP, GMP %, lot, issue size, anchor flag | Also covers **BSE-SME** issues absent from NSE's API |
| investorgain `mainboard-ipo-list` | Published category subscription multiples | Authoritative where NSE withholds the per-category offer size |
| ipowatch | GMP cross-check | Fallback if investorgain changes its markup |

**BSE's own API is unusable** — `api.bseindia.com` returns 403 behind Akamai
and every `www.bseindia.com` path serves a JavaScript shell. BSE-SME coverage
therefore comes via investorgain, and BSE bid data arrives inside NSE's
`demandDataBSE` field.

### Three data traps this code handles

These were all found against live data and are pinned by tests:

1. **HTML entities.** `&#8377;22.98 Cr` is ₹22.98 Cr. Parsed without
   unescaping, it reads as the number **8377**. Every cell is unescaped before
   any number is extracted.
2. **`"0.00"` means absent, not zero.** NSE returns a literal zero subscription
   multiple in the projections that do not carry the field. Treating that as
   real demand marks healthy issues as undersubscribed.
3. **Incoherent published ratios.** A 1.85x-subscribed issue was published with
   "QIB 327x". Since the total is the quota-weighted mean of the categories, no
   category can exceed roughly 8x the total; anything beyond that is rejected.
4. **A vocabulary that changes under you.** NSE's `securityType` reported
   mainboard issues as `EQ` and `BE`, then switched to `Equity` — *within a
   single day*. An exact-match filter silently hid all 577 mainboard issues,
   the only segment reachable on a ₹1 lakh budget. Matching is now
   case-insensitive over known aliases, unrecognised types are logged, and an
   entire segment vanishing from a large calendar raises an error rather than
   looking like a quiet market. The `index=` parameter on that endpoint is
   ignored by the server, so it cannot be used to narrow the query.

**NSE keeps the subscription book after listing.** An earlier version of this
file claimed the opposite, generalising from one issue (MKJEWELS) that returns
an empty table; measured across 18 recent listings, 17 still serve their full
category-wise book. That is what makes historical calibration possible.

Every run still writes a snapshot, because two things genuinely are
unrecoverable: how subscription *built* across the three days, and the
grey-market premium as quoted at the time.

---

## Alerts

| Kind | Fires when |
|---|---|
| `oversubscribed` | Total crosses 1x / 5x / 20x / 50x / 100x — once per level |
| `opportunity` | Estimated gain clears the bar while the issue is still open |
| `closing_today` | An actionable issue closes today, after 1 PM IST — marked urgent |

De-duplicated in SQLite, so a 250x issue polled every 30 minutes does not
produce a stream of identical toasts.

## Configuration

Everything tunable lives in `config.json`:

```json
"triggers": {
  "min_expected_listing_gain_pct": 10.0,
  "oversubscription_alert_levels": [1.0, 5.0, 20.0, 50.0, 100.0],
  "close_day_urgent_hour_ist": 13
},
"segments": { "mainboard": true, "sme": true }
```

Set `"sme": false` to track mainboard only.

## Outcomes and market regime

```bash
.venv\Scripts\python run.py outcomes --backfill 200
```

Neither exchange will hand over listing prices — NSE's quote API answers 403
and its archives 503, BSE is behind Akamai. Yahoo's chart endpoint carries
NSE symbols as `SYMBOL.NS` and BSE as `SYMBOL.BO`, needs no key, and gives
daily bars.

**Gain is measured against the listing-day open**, not the close. That is the
price GMP is a quote on and the price someone selling into the listing
actually receives. The close is stored too, because the gap is informative —
SFC Environmental opened +7.6% and closed +29.1%.

### What the history says

Base rates matter more than any single prediction:

```
median listing gain   +5.5%
cleared 10%             33%
cleared 20%             23%
listed below issue      13%
p10 -1.9%   p25 +0.6%   median +5.5%   p75 +13.8%   p90 +65.4%
```

**A 10% bar excludes roughly two issues in three.** The distribution is also
badly skewed — the mean (+15.4%) is nearly three times the median, because a
handful of issues double. Regime labelling therefore uses the median, and a
test pins that one +300% listing cannot make a flat market read HOT.

The `MARKET REGIME` line (HOT / NORMAL / COLD) is the honest answer to "what
is sentiment doing": how recent IPOs *actually* listed. It is reported, not
yet fed into scoring — GMP already prices in much of the same sentiment, and
double-counting it on 30 observations would be guessing.

### Two coverage limits

- **SME resolves poorly.** Yahoo's coverage of NSE Emerge and BSE SME is
  patchy, so most SME listings cannot be captured. Base rates are therefore
  mainboard-weighted; treat the SME figures as thin.
- **GMP alone cannot be rebuilt.** Listing prices are fetchable and so is the
  final subscription book, so the subscription side *can* be calibrated
  historically (`backtest.py subscription`). Nobody publishes what GMP was
  quoted at on a given closing day, so that shrink can only be fitted from
  pairs recorded going forward.

### Guards on the source

NSE's calendar carries stale rows that would quietly poison all of this:

```
OLAPP   issuePrice 11   band Rs.72-76    closed Aug 2024, "listed" Nov 2026
BAHETI  issuePrice 774  band Rs.45       closed Nov 2022, "listed" Nov 2026
```

An issue price outside its own band is replaced by the band; a listing more
than 30 days after the close is discarded. Left unguarded, Ola Electric alone
contributed a fictional +86.4%.

## Calibration

```bash
python backtest.py status
python backtest.py record "HGS (India)" 148.5
python backtest.py calibrate --write
```

```bash
python backtest.py subscription
```

**The GMP shrink is still an informed prior, not a fitted value**, because no
free source publishes historical GMP as quoted on a given closing day. That
part can only be fitted from pairs recorded going forward.

**The subscription side can be measured now.** NSE retains the final book
after listing, so `backtest.py subscription` pairs it with the realised gain
and reports rank correlations, gain by subscription bucket, and whether the
composition rules (QIB lag, NII leverage) actually separate outcomes — they
were reasoned from market mechanics and deserve testing, not trust.

Once ~30 issues have been observed open and then listed, `calibrate` fits the
GMP shrink factors to realised outcomes and reports whether the gain bar
actually selected winners. Until then the engine is running on reasoned
defaults, and `confidence` is the honest guide to each row.

## Tests

```bash
.venv\Scripts\python -m unittest discover -s tests -v
```

132 tests covering the parsing traps, calendar guards, allotment maths, GMP
shrink behaviour, per-segment subscription composition, the coherence guard,
stable IPO identity across sources, budget/PAN allocation, the
Poisson-binomial outcome spread, listing-outcome capture, regime reading,
nearest-neighbour probability and its calibration,
alert de-duplication and end-to-end scoring.

---

## Important

This is **informational software, not investment advice**, and carries no
recommendation to apply for any issue.

- **GMP is unofficial and unregulated.** It is a thin over-the-counter quote
  that can be manipulated and regularly fails to predict the open. The model
  shrinks it heavily for exactly this reason, and it remains the single largest
  source of error.
- **High subscription and good allotment odds are opposites.** The most
  oversubscribed issues are the hardest to get. A 500x issue can be worth less
  in expectation than a 20x one.
- **Listing gains are only realised if you can sell.** SME counters often lack
  listing-day liquidity, and SME lots run ₹1–2 lakh against ~₹15,000 on the
  mainboard.
- Capital is blocked under ASBA from application until allotment, typically
  five to seven days.
- Scraped sources can change markup without notice. The engine logs a warning
  and degrades rather than guessing — check `confidence` and the dashboard
  before acting on any row.

## Licence

MIT — see [LICENSE](LICENSE). Informational software only, provided without
warranty; it is not investment advice.
