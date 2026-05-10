"""PostgreSQL adapter — used on Vercel + Supabase."""
from database import _compute_crossovers   # shared helper
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
        """Batch crossover detection — 2 DB queries for any period size."""
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
                    "SELECT date, ticker, company_name, market_cap FROM market_cap_daily "
                    "WHERE date = ANY(%s) AND market_cap >= 100e9 ORDER BY date",
                    (all_needed,),
                )
                rows = [(r["date"], r["ticker"], r["company_name"], r["market_cap"])
                        for r in cur.fetchall()]

        return _compute_crossovers(rows, sorted_dates)

    def get_crossovers(self, date_str):
        """Detect crossovers within ≥200B (mega) and 100B–200B tiers."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MAX(date) FROM market_cap_daily WHERE date < %s", (date_str,)
                )
                row = cur.fetchone()
                prev_date = row["max"] if row else None
        if not prev_date:
            return []

        def _get(d, lo, hi=None):
            cond = f"market_cap >= {lo}" if hi is None else f"market_cap >= {lo} AND market_cap < {hi}"
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT ticker, company_name, market_cap FROM market_cap_daily "
                        f"WHERE date = %s AND {cond} ORDER BY market_cap DESC", (d,)
                    )
                    return {r["ticker"]: {k: float(v) if k == "market_cap" else v
                                          for k, v in r.items()}
                            for r in cur.fetchall()}

        def _detect(today, prev, tier):
            common = [t for t in today if t in prev]
            out = []
            for i in range(len(common)):
                for j in range(i + 1, len(common)):
                    a, b = common[i], common[j]
                    at, bt = today[a]["market_cap"], today[b]["market_cap"]
                    ap, bp = prev[a]["market_cap"],  prev[b]["market_cap"]
                    winner = loser = None
                    if at > bt and ap <= bp: winner, loser = a, b
                    elif bt > at and bp <= ap: winner, loser = b, a
                    if winner:
                        out.append({
                            "tier": tier,
                            "winner": winner, "winner_name": today[winner]["company_name"],
                            "winner_cap": today[winner]["market_cap"],
                            "winner_prev_cap": prev[winner]["market_cap"],
                            "loser": loser,   "loser_name":  today[loser]["company_name"],
                            "loser_cap":  today[loser]["market_cap"],
                            "loser_prev_cap": prev[loser]["market_cap"],
                        })
            return out

        results = (
            _detect(_get(date_str, 200e9),        _get(prev_date, 200e9),        "mega") +
            _detect(_get(date_str, 100e9, 200e9),  _get(prev_date, 100e9, 200e9), "large")
        )
        return sorted(results, key=lambda x: x["winner_cap"], reverse=True)

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
