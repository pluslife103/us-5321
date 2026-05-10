"""PostgreSQL adapter — used on Vercel + Supabase."""


TIERS = [
    ("mega",    200e9,  None),
    ("large",   100e9, 200e9),
    ("mlarge",   10e9, 100e9),
    ("mid",       2e9,  10e9),
    ("small",   300e6,   2e9),
]
TIER_MIN = 300e6

def _tier(cap):
    for name, lo, hi in TIERS:
        if cap >= lo and (hi is None or cap < hi):
            return name
    return None


def _compute_crossovers(rows, target_dates):
    """Detect crossovers from raw DB rows across consecutive dates."""
    by_date = {}
    for r in rows:
        d, t, name, cap = r[0], r[1], r[2], float(r[3])
        if d not in by_date:
            by_date[d] = {}
        by_date[d][t] = {"company_name": name, "market_cap": cap, "tier": _tier(cap)}

    all_dates  = sorted(by_date.keys())
    target_set = set(target_dates)
    events     = []

    for i in range(1, len(all_dates)):
        today_str, prev_str = all_dates[i], all_dates[i - 1]
        if today_str not in target_set:
            continue
        today, prev = by_date[today_str], by_date[prev_str]
        common = [t for t in today if t in prev]

        for j in range(len(common)):
            for k in range(j + 1, len(common)):
                a, b = common[j], common[k]
                if today[a]["tier"] != today[b]["tier"]:
                    continue
                at, bt = today[a]["market_cap"], today[b]["market_cap"]
                ap, bp = prev[a]["market_cap"],  prev[b]["market_cap"]
                winner = loser = None
                if at > bt and ap <= bp: winner, loser = a, b
                elif bt > at and bp <= ap: winner, loser = b, a
                if winner:
                    events.append({
                        "date":            today_str,
                        "tier":            today[winner]["tier"],
                        "winner":          winner,
                        "winner_name":     today[winner]["company_name"],
                        "winner_cap":      today[winner]["market_cap"],
                        "winner_prev_cap": prev[winner]["market_cap"],
                        "loser":           loser,
                        "loser_name":      today[loser]["company_name"],
                        "loser_cap":       today[loser]["market_cap"],
                        "loser_prev_cap":  prev[loser]["market_cap"],
                    })
    return events
import os
from contextlib import contextmanager
from datetime import date

import psycopg2
import psycopg2.extras

_DATABASE_URL = os.environ["DATABASE_URL"]
if "sslmode" not in _DATABASE_URL:
    sep = "&" if "?" in _DATABASE_URL else "?"
    _DATABASE_URL += f"{sep}sslmode=require"


class Database:
    @contextmanager
    def _conn(self):
        conn = psycopg2.connect(_DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_db(self):
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS ticker_info (
                        ticker       TEXT PRIMARY KEY,
                        company_name TEXT DEFAULT '',
                        sector       TEXT DEFAULT '',
                        shares       REAL DEFAULT 0,
                        updated      TEXT DEFAULT ''
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS market_cap_daily (
                        date         TEXT,
                        ticker       TEXT,
                        company_name TEXT,
                        sector       TEXT,
                        price        REAL DEFAULT 0,
                        market_cap   REAL DEFAULT 0,
                        shares       REAL DEFAULT 0,
                        change_pct   REAL DEFAULT 0,
                        PRIMARY KEY (date, ticker)
                    )
                """)
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_mcd_date ON market_cap_daily(date)"
                )

    # ── Shares cache ──────────────────────────────────────────────────────────

    def get_missing_shares(self, tickers):
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT ticker FROM ticker_info WHERE shares > 0")
                cached = {r["ticker"] for r in cur.fetchall()}
        return [t for t in tickers if t not in cached]

    def upsert_ticker_info(self, data):
        """data: {ticker: {company_name, sector, shares}}"""
        today = date.today().isoformat()
        rows = [(t, d.get("company_name", t), d.get("sector", ""), d.get("shares", 0), today)
                for t, d in data.items()]
        with self._conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, """
                    INSERT INTO ticker_info (ticker, company_name, sector, shares, updated)
                    VALUES %s
                    ON CONFLICT (ticker) DO UPDATE SET
                        company_name = EXCLUDED.company_name,
                        sector       = EXCLUDED.sector,
                        shares       = EXCLUDED.shares,
                        updated      = EXCLUDED.updated
                """, rows)

    def get_all_shares(self):
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT ticker, shares FROM ticker_info WHERE shares > 0")
                return {r["ticker"]: r["shares"] for r in cur.fetchall()}

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_dates(self):
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT date FROM market_cap_daily ORDER BY date DESC LIMIT 90"
                )
                return [r["date"] for r in cur.fetchall()]

    def has_data_for_date(self, date_str):
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM market_cap_daily WHERE date = %s", (date_str,)
                )
                return cur.fetchone()["count"] > 0

    def get_sectors(self, date_str):
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT sector FROM market_cap_daily "
                    "WHERE date = %s AND sector != '' ORDER BY sector",
                    (date_str,),
                )
                return [r["sector"] for r in cur.fetchall()]

    def get_summary(self, date_str):
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        COUNT(*)                                                        AS total,
                        SUM(market_cap)                                                 AS total_cap,
                        SUM(CASE WHEN market_cap >= 200e9                     THEN 1 ELSE 0 END) AS mega,
                        SUM(CASE WHEN market_cap >= 10e9 AND market_cap < 200e9 THEN 1 ELSE 0 END) AS large,
                        SUM(CASE WHEN market_cap >= 2e9  AND market_cap < 10e9  THEN 1 ELSE 0 END) AS mid,
                        SUM(CASE WHEN market_cap >= 300e6 AND market_cap < 2e9  THEN 1 ELSE 0 END) AS small,
                        SUM(CASE WHEN market_cap > 0 AND market_cap < 300e6    THEN 1 ELSE 0 END) AS micro
                    FROM market_cap_daily
                    WHERE date = %s AND market_cap > 0
                    """,
                    (date_str,),
                )
                row = cur.fetchone()
                return dict(row) if row else {}

    def get_data_for_date(self, date_str):
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        RANK() OVER (ORDER BY market_cap DESC) AS rank,
                        ticker, company_name, sector,
                        price, market_cap, shares, change_pct
                    FROM market_cap_daily
                    WHERE date = %s AND market_cap > 0
                    ORDER BY market_cap DESC
                    """,
                    (date_str,),
                )
                return [dict(r) for r in cur.fetchall()]

    def get_crossovers_batch(self, dates):
        """Batch crossover detection across all tiers — 2 DB queries."""
        if not dates:
            return []
        sorted_dates = sorted(set(dates))

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MAX(date) FROM market_cap_daily WHERE date < %s", (sorted_dates[0],)
                )
                row = cur.fetchone()
                first_prev = row["max"] if row and row["max"] else None

        all_needed = ([first_prev] if first_prev else []) + sorted_dates

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT date, ticker, company_name, market_cap FROM market_cap_daily "
                    f"WHERE date = ANY(%s) AND market_cap >= {TIER_MIN} ORDER BY date",
                    (all_needed,),
                )
                rows = [(r["date"], r["ticker"], r["company_name"], r["market_cap"])
                        for r in cur.fetchall()]

        return _compute_crossovers(rows, sorted_dates)

    def get_crossovers(self, date_str):
        """Detect crossovers across all tiers for a single date."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MAX(date) FROM market_cap_daily WHERE date < %s", (date_str,)
                )
                row = cur.fetchone()
                prev_date = row["max"] if row and row["max"] else None
        if not prev_date:
            return []

        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT date, ticker, company_name, market_cap FROM market_cap_daily "
                    f"WHERE date = ANY(%s) AND market_cap >= {TIER_MIN} ORDER BY date",
                    ([prev_date, date_str],),
                )
                rows = [(r["date"], r["ticker"], r["company_name"], r["market_cap"])
                        for r in cur.fetchall()]

        events = _compute_crossovers(rows, [date_str])
        return sorted(events, key=lambda x: x["winner_cap"], reverse=True)

    def get_ticker_history(self, tickers):
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT date, ticker, company_name, market_cap FROM market_cap_daily "
                    "WHERE ticker = ANY(%s) AND market_cap > 0 ORDER BY date ASC",
                    (list(tickers),),
                )
                return [dict(r) for r in cur.fetchall()]

    # ── Writes ────────────────────────────────────────────────────────────────

    def clear_prices(self):
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM market_cap_daily")

    def clear_all(self):
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM ticker_info")
                cur.execute("DELETE FROM market_cap_daily")

    def insert_batch(self, records):
        """records: list of (date, ticker, company_name, sector, price, market_cap, shares, change_pct)"""
        with self._conn() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO market_cap_daily
                        (date, ticker, company_name, sector, price, market_cap, shares, change_pct)
                    VALUES %s
                    ON CONFLICT (date, ticker) DO UPDATE SET
                        company_name = EXCLUDED.company_name,
                        sector       = EXCLUDED.sector,
                        price        = EXCLUDED.price,
                        market_cap   = EXCLUDED.market_cap,
                        shares       = EXCLUDED.shares,
                        change_pct   = EXCLUDED.change_pct
                    """,
                    records,
                )
