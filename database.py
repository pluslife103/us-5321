import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "us_market_cap.db"


class Database:
    def __init__(self, path=DB_PATH):
        self.path = path

    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS market_cap_daily (
                    date        TEXT,
                    ticker      TEXT,
                    company_name TEXT,
                    sector      TEXT,
                    price       REAL,
                    market_cap  REAL,
                    shares      REAL,
                    change_pct  REAL,
                    PRIMARY KEY (date, ticker)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mcd_date ON market_cap_daily(date)"
            )

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_dates(self):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT date FROM market_cap_daily ORDER BY date DESC LIMIT 30"
            ).fetchall()
            return [r[0] for r in rows]

    def has_data_for_date(self, date_str):
        with self._conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM market_cap_daily WHERE date = ?", (date_str,)
            ).fetchone()[0]
            return count > 0

    def get_sectors(self, date_str):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT sector FROM market_cap_daily WHERE date = ? AND sector != '' ORDER BY sector",
                (date_str,),
            ).fetchall()
            return [r[0] for r in rows]

    def get_summary(self, date_str):
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(market_cap) AS total_cap,
                    SUM(CASE WHEN market_cap >= 200e9                    THEN 1 ELSE 0 END) AS mega,
                    SUM(CASE WHEN market_cap >= 10e9 AND market_cap < 200e9 THEN 1 ELSE 0 END) AS large,
                    SUM(CASE WHEN market_cap >= 2e9  AND market_cap < 10e9  THEN 1 ELSE 0 END) AS mid,
                    SUM(CASE WHEN market_cap >= 300e6 AND market_cap < 2e9  THEN 1 ELSE 0 END) AS small,
                    SUM(CASE WHEN market_cap > 0 AND market_cap < 300e6    THEN 1 ELSE 0 END) AS micro
                FROM market_cap_daily
                WHERE date = ? AND market_cap > 0
                """,
                (date_str,),
            ).fetchone()
            return dict(row)

    def get_data_for_date(self, date_str):
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT
                    RANK() OVER (ORDER BY market_cap DESC) AS rank,
                    ticker, company_name, sector,
                    price, market_cap, shares, change_pct
                FROM market_cap_daily
                WHERE date = ? AND market_cap > 0
                ORDER BY market_cap DESC
                """,
                (date_str,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Writes ────────────────────────────────────────────────────────────────

    def insert_batch(self, records):
        """records: list of (date, ticker, company_name, sector, price, market_cap, shares, change_pct)"""
        with self._conn() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO market_cap_daily
                    (date, ticker, company_name, sector, price, market_cap, shares, change_pct)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                records,
            )
