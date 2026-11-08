"""SQLite persistence: snapshot history and alert de-duplication.

Snapshots record what the engine saw at a point in time. The final book
survives on NSE after listing and can be re-fetched, but the *path* to it -
how subscription built through the three days - is not retained anywhere, and
neither is the grey-market premium as quoted at the time. Those only exist if
they were written down as they happened, which is what every run does.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from .models import IPO
from .util import ROOT, now_ist

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at     TEXT NOT NULL,
    ipo_key         TEXT NOT NULL,
    name            TEXT NOT NULL,
    segment         TEXT,
    close_date      TEXT,
    total_sub       REAL,
    qib_sub         REAL,
    nii_sub         REAL,
    retail_sub      REAL,
    retail_apps     INTEGER,
    gmp_pct         REAL,
    expected_gain   REAL,
    retail_odds     REAL,
    confidence      INTEGER,
    verdict         TEXT,
    payload         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_key ON snapshots(ipo_key, captured_at);

CREATE TABLE IF NOT EXISTS alerts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fired_at      TEXT NOT NULL,
    ipo_key       TEXT NOT NULL,
    kind          TEXT NOT NULL,
    level         REAL,
    title         TEXT,
    body          TEXT,
    UNIQUE(ipo_key, kind, level)
);

-- Maps every alias an issue has ever been seen under (NSE symbol, normalised
-- company name) onto one canonical id. Without this an issue changes identity
-- the moment it leaves the exchange calendar and survives only as a GMP row,
-- which splits its history and can re-fire spent alerts.
CREATE TABLE IF NOT EXISTS identities (
    alias      TEXT PRIMARY KEY,
    canonical  TEXT NOT NULL,
    first_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_identities_canonical ON identities(canonical);

-- Realised listing prices. gain_open is the headline: it is what GMP is a
-- quote on and what someone selling into the listing actually receives.
CREATE TABLE IF NOT EXISTS outcomes (
    ipo_key       TEXT PRIMARY KEY,
    name          TEXT,
    segment       TEXT,
    listing_date  TEXT,
    issue_price   REAL,
    listing_open  REAL,
    listing_close REAL,
    gain_open     REAL,
    gain_close    REAL,
    source        TEXT,
    recorded_at   TEXT
);
"""

# Indexes over columns that may still be arriving via migration, so they are
# created only once the ALTER TABLE statements have run.
POST_MIGRATION_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_outcomes_listing ON outcomes(listing_date);
"""

# Columns added after the first release, applied to existing databases.
_MIGRATIONS = {
    "outcomes": {
        "segment": "TEXT", "listing_date": "TEXT", "listing_open": "REAL",
        "listing_close": "REAL", "gain_open": "REAL", "gain_close": "REAL",
        "source": "TEXT",
        # The final book, re-fetched from NSE after listing. Paired with
        # gain_open this is the historical calibration set.
        "final_total_sub": "REAL", "final_qib_sub": "REAL",
        "final_nii_sub": "REAL", "final_retail_sub": "REAL",
        "final_bnii_sub": "REAL", "final_snii_sub": "REAL",
        "book_fetched_at": "TEXT",
    },
}


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_absolute():
            self.path = ROOT / self.path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        # A scheduled run can fire while a long backfill still holds the
        # database. WAL lets the reader through, and the busy timeout makes a
        # writer wait rather than raising "database is locked".
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.executescript(POST_MIGRATION_SCHEMA)
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        for table, columns in _MIGRATIONS.items():
            existing = {row["name"] for row in
                        self.conn.execute(f"PRAGMA table_info({table})")}
            for column, kind in columns.items():
                if column not in existing:
                    self.conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
                    log.info("added %s.%s", table, column)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    # ---------------- identity ----------------

    def assign_identity(self, ipo: IPO) -> str:
        """Give this issue the one identity it will keep for its whole life.

        An NSE symbol is only available while the exchange still carries the
        issue in its calendar; outside that window the GMP sites know it by
        name alone. Keying on whichever happened to be present at write time
        made a single IPO fragment into two (observed: BCSSL and
        bombaycoatedandspecialsteels were the same company on consecutive
        days), splitting its snapshot history and resetting alert
        de-duplication.

        Any alias already on file wins, so an issue first seen by name keeps
        that id even after a symbol appears. Aliases seen together are merged
        onto the oldest canonical.
        """
        aliases = ipo.natural_keys
        if not aliases:
            return ""

        placeholders = ",".join("?" * len(aliases))
        known = list(self.conn.execute(
            f"""SELECT alias, canonical, first_seen FROM identities
                WHERE alias IN ({placeholders})
                ORDER BY first_seen""", aliases))

        canonicals = []
        for row in known:
            if row["canonical"] not in canonicals:
                canonicals.append(row["canonical"])

        if canonicals:
            canonical = canonicals[0]
            # Two aliases that were tracked apart have now been seen on the
            # same issue, so fold the newer identities into the oldest.
            for stale in canonicals[1:]:
                self._merge_identity(stale, canonical)
        else:
            # Prefer the exchange symbol as the readable canonical id.
            canonical = aliases[0]

        now = now_ist().isoformat(timespec="seconds")
        self.conn.executemany(
            """INSERT INTO identities (alias, canonical, first_seen) VALUES (?,?,?)
               ON CONFLICT(alias) DO UPDATE SET canonical = excluded.canonical""",
            [(alias, canonical, now) for alias in aliases])
        self.conn.commit()

        ipo.canonical_key = canonical
        return canonical

    def _merge_identity(self, stale: str, canonical: str) -> None:
        """Re-point every row written under a duplicate id."""
        for table in ("snapshots", "alerts", "outcomes"):
            try:
                self.conn.execute(
                    f"UPDATE OR IGNORE {table} SET ipo_key = ? WHERE ipo_key = ?"
                    if table != "outcomes" else
                    "UPDATE OR IGNORE outcomes SET ipo_key = ? WHERE ipo_key = ?",
                    (canonical, stale))
            except sqlite3.IntegrityError:          # pragma: no cover
                pass
        self.conn.execute("UPDATE identities SET canonical = ? WHERE canonical = ?",
                          (canonical, stale))
        log.info("merged duplicate IPO identity %r into %r", stale, canonical)

    def repair_identities(self) -> int:
        """Back-fill the alias table from history and fold existing splits.

        Rows written before identity resolution existed are keyed by whatever
        was available that day. Company name is the one field common to both,
        so it is used to reunite them.
        """
        from .util import norm_name

        by_name: dict[str, list[str]] = {}
        for row in self.conn.execute(
                "SELECT DISTINCT ipo_key, name FROM snapshots"):
            by_name.setdefault(norm_name(row["name"]), []).append(row["ipo_key"])

        merged = 0
        now = now_ist().isoformat(timespec="seconds")
        for name_key, keys in by_name.items():
            # Prefer an exchange symbol (upper-case, no spaces) as canonical.
            symbols = [k for k in keys if k.isupper()]
            canonical = symbols[0] if symbols else sorted(keys)[0]
            for stale in keys:
                if stale != canonical:
                    self._merge_identity(stale, canonical)
                    merged += 1
            for alias in set(keys) | {name_key}:
                self.conn.execute(
                    """INSERT INTO identities (alias, canonical, first_seen)
                       VALUES (?,?,?)
                       ON CONFLICT(alias) DO UPDATE SET canonical = excluded.canonical""",
                    (alias, canonical, now))
        self.conn.commit()
        return merged

    # ---------------- snapshots ----------------

    def save_snapshot(self, ipo: IPO) -> None:
        retail = ipo.bids.get("RETAIL")
        self.conn.execute(
            """INSERT INTO snapshots (captured_at, ipo_key, name, segment, close_date,
                   total_sub, qib_sub, nii_sub, retail_sub, retail_apps, gmp_pct,
                   expected_gain, retail_odds, confidence, verdict, payload)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now_ist().isoformat(timespec="seconds"), ipo.key, ipo.name, ipo.segment,
             ipo.close_date.isoformat() if ipo.close_date else None,
             ipo.total_subscription, ipo.sub("QIB"), ipo.sub("NII"), ipo.sub("RETAIL"),
             retail.applications if retail else None,
             ipo.gmp_pct, ipo.expected_gain_pct,
             ipo.allotment_prob.get("RETAIL"), ipo.confidence, ipo.verdict,
             json.dumps(ipo.to_dict(), default=str)))
        self.conn.commit()

    def history(self, ipo_key: str, limit: int = 50) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            """SELECT captured_at, total_sub, gmp_pct, expected_gain, retail_odds
               FROM snapshots WHERE ipo_key = ?
               ORDER BY captured_at DESC LIMIT ?""", (ipo_key, limit)))

    def previous_subscription(self, ipo_key: str) -> float | None:
        row = self.conn.execute(
            """SELECT total_sub FROM snapshots
               WHERE ipo_key = ? AND total_sub IS NOT NULL
               ORDER BY captured_at DESC LIMIT 1""", (ipo_key,)).fetchone()
        return row["total_sub"] if row else None

    # ---------------- alerts ----------------

    def already_alerted(self, ipo_key: str, kind: str, level: float | None) -> bool:
        row = self.conn.execute(
            """SELECT 1 FROM alerts WHERE ipo_key = ? AND kind = ?
               AND level IS ?""", (ipo_key, kind, level)).fetchone()
        return row is not None

    def record_alert(self, ipo_key: str, kind: str, level: float | None,
                     title: str, body: str) -> bool:
        """Returns False if this exact alert has already gone out."""
        try:
            self.conn.execute(
                """INSERT INTO alerts (fired_at, ipo_key, kind, level, title, body)
                   VALUES (?,?,?,?,?,?)""",
                (now_ist().isoformat(timespec="seconds"), ipo_key, kind, level,
                 title, body))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def recent_alerts(self, limit: int = 30) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            """SELECT fired_at, ipo_key, kind, level, title, body
               FROM alerts ORDER BY fired_at DESC LIMIT ?""", (limit,)))

    # ---------------- outcomes (for calibration) ----------------

    def record_outcome(self, ipo_key: str, name: str, issue_price: float | None,
                       listing_open: float | None, *,
                       listing_close: float | None = None,
                       segment: str | None = None,
                       listing_date=None, source: str | None = None) -> None:
        def gain(price):
            if issue_price and price and issue_price > 0:
                return (price - issue_price) / issue_price * 100.0
            return None

        self.conn.execute(
            """INSERT INTO outcomes (ipo_key, name, segment, listing_date,
                   issue_price, listing_open, listing_close, gain_open,
                   gain_close, source, recorded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(ipo_key) DO UPDATE SET
                   listing_open  = excluded.listing_open,
                   listing_close = excluded.listing_close,
                   gain_open     = excluded.gain_open,
                   gain_close    = excluded.gain_close,
                   source        = excluded.source,
                   recorded_at   = excluded.recorded_at""",
            (ipo_key, name, segment,
             listing_date.isoformat() if hasattr(listing_date, "isoformat") else listing_date,
             issue_price, listing_open, listing_close,
             gain(listing_open), gain(listing_close), source,
             now_ist().isoformat(timespec="seconds")))
        self.conn.commit()

    def record_final_book(self, ipo_key: str, subs: dict[str, float | None]) -> None:
        """Attach the post-listing subscription book to an outcome row."""
        self.conn.execute(
            """UPDATE outcomes SET
                   final_total_sub = ?, final_qib_sub = ?, final_nii_sub = ?,
                   final_retail_sub = ?, final_bnii_sub = ?, final_snii_sub = ?,
                   book_fetched_at = ?
               WHERE ipo_key = ?""",
            (subs.get("TOTAL"), subs.get("QIB"), subs.get("NII"),
             subs.get("RETAIL"), subs.get("BNII"), subs.get("SNII"),
             now_ist().isoformat(timespec="seconds"), ipo_key))
        self.conn.commit()

    def has_final_book(self, ipo_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM outcomes WHERE ipo_key = ? AND book_fetched_at IS NOT NULL",
            (ipo_key,)).fetchone()
        return row is not None

    def historical_pairs(self) -> list[sqlite3.Row]:
        """Final book paired with realised listing gain - the calibration set."""
        return list(self.conn.execute(
            """SELECT ipo_key, name, segment, listing_date, issue_price,
                      gain_open, gain_close, final_total_sub, final_qib_sub,
                      final_nii_sub, final_retail_sub, final_bnii_sub,
                      final_snii_sub
               FROM outcomes
               WHERE gain_open IS NOT NULL AND final_total_sub IS NOT NULL
               ORDER BY listing_date"""))

    def has_outcome(self, ipo_key: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM outcomes WHERE ipo_key = ? AND gain_open IS NOT NULL",
            (ipo_key,)).fetchone() is not None

    def outcomes(self, limit: int = 500, segment: str | None = None) -> list[sqlite3.Row]:
        """Most recent realised listings first."""
        sql = ("SELECT * FROM outcomes WHERE gain_open IS NOT NULL"
               + (" AND segment = ?" if segment else "")
               + " ORDER BY listing_date DESC LIMIT ?")
        args = ([segment, limit] if segment else [limit])
        return list(self.conn.execute(sql, args))

    def calibration_rows(self) -> list[sqlite3.Row]:
        """Closing-day snapshots paired with realised listing gains.

        Only snapshots taken while the engine could actually see the book are
        useful here, so the gain is matched to the last snapshot of each issue.
        """
        return list(self.conn.execute(
            """SELECT s.ipo_key, s.name, s.segment, s.gmp_pct, s.total_sub,
                      s.qib_sub, s.retail_sub, s.expected_gain,
                      o.gain_open AS listing_gain, o.gain_close
               FROM snapshots s
               JOIN outcomes o ON o.ipo_key = s.ipo_key
               WHERE s.id IN (SELECT MAX(id) FROM snapshots GROUP BY ipo_key)
                 AND o.gain_open IS NOT NULL"""))
