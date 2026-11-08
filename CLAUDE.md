# Running this project

This repo is meant to be opened in Claude and run from there. The person
using it may not write Python. If they ask to run, start, open or use the
tool — or just say "run it" — do the whole setup yourself without asking them
to type commands.

## Start the app

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows
.venv/bin/python -m pip install -r requirements.txt       # macOS / Linux
.venv/Scripts/python run.py app                           # Windows
.venv/bin/python run.py app                               # macOS / Linux
```

`run.py app` fetches live data (about a minute), serves
<http://127.0.0.1:8000/> and opens a browser. Everything is in the browser
from then on: goal, confidence, capital, PAN count, the issue table and the
application plan.

Skip the venv steps if `.venv` already exists. If port 8000 is taken, pass
`--port 8001`.

## What to tell them if things go wrong

- **"No IPO is open right now"** is normal, not a bug. Indian IPOs run in
  bursts; the table still shows recently closed issues.
- **A source returned nothing.** The engine logs a warning and keeps going
  with what it has. Check the `Conf.` column — it drops when data is missing.
- **Odds show as blank.** The probability model needs 50+ realised listings.
  `run.py outcomes --backfill 200 --books 200` collects them; the shipped
  database already has ~190.
- **Scraping breaks.** NSE has changed its field vocabulary mid-day before
  (`EQ` → `Equity`, which silently hid every mainboard issue). If a whole
  segment vanishes the engine now logs an error rather than looking like a
  quiet market. Check `ipo_engine/sources/nse.py`.

## Other commands

| command | what it does |
|---|---|
| `run.py app` | the app — start here |
| `run.py once` | one pass in the terminal, fires desktop alerts |
| `run.py outcomes --backfill 200 --books 200` | collect realised listing results |
| `backtest.py probability` | check the odds model is calibrated |
| `backtest.py subscription` | how well the book predicts the listing |
| `python -m unittest discover -s tests` | 132 tests |

## Things worth knowing before changing code

- **Averaged, never added.** GMP and subscription are two readings of the
  same demand. Summing them double-counts.
- **The score excludes GMP on purpose.** No historical GMP exists, so a score
  containing it could not be matched against past outcomes.
- **Sort history by score alone.** Sorting `(score, gain)` makes the outcome
  itself the tie-break, which biases every tie toward the worst neighbours.
  The curve caps, so ties are common.
- **HTML-unescape before parsing numbers.** `&#8377;22.98 Cr` reads as the
  number 8377 otherwise.
- **`"0.00"` from NSE means absent, not zero.**
- **Yahoo back-adjusts for splits.** The fetch window must reach the present
  or the split event is not returned and the correction silently does nothing.

## Tone

This tool reports probabilities, not recommendations. Do not describe its
output as advice, and keep the caveats attached: the history is a single
18-month regime, mainboard-heavy, and the GMP half of the model has never
been validated against outcomes.
