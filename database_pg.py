"""PostgreSQL adapter — used on Vercel + Supabase."""
import os
from contextlib import contextmanager

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
                        COUNT(*)                                                       AS total,
                        SUM(market_cap)                                                AS total_cap,
                        SUM(CASE WHEN market_cap >= 200e9                    THEN 1 ELSE 0 END) AS mega,
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

    # ── Writes ────────────────────────────────────────────────────────────────

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
