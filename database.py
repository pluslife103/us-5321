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
                CREATE TABLE IF NOT EXISTS ticker_info (
                    ticker       TEXT PRIMARY KEY,
                    company_name TEXT DEFAULT '',
                    sector       TEXT DEFAULT '',
                    shares       REAL DEFAULT 0,
                    updated      TEXT DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS market_cap_daily (
                    date         TEXT,
                    ticker       TEXT,
                    company_name TEXT,
                    sector       TEXT,
                    price        REAL,
                    market_cap   REAL,
                    shares       REAL,
                    change_pct   REAL,
                    PRIMARY KEY (date, ticker)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mcd_date ON market_cap_daily(date)"
            )

    # ── Shares cache ──────────────────────────────────────────────────────────

    def get_missing_shares(self, tickers):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ticker FROM ticker_info WHERE shares > 0"
            ).fetchall()
            cached = {r[0] for r in rows}
        return [t for t in tickers if t not in cached]

    def upsert_ticker_info(self, data):
        """data: {ticker: {company_name, sector, shares}}"""
        from datetime import date
        today = date.today().isoformat()
        with self._conn() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO ticker_info (ticker, company_name, sector, shares, updated) "
                "VALUES (?, ?, ?, ?, ?)",
                [(t, d.get("company_name", t), d.get("sector", ""), d.get("shares", 0), today)
                 for t, d in data.items()],
            )

    def get_all_shares(self):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ticker, shares FROM ticker_info WHERE shares > 0"
            ).fetchall()
            return {r[0]: r[1] for r in rows}

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_dates(self):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT date FROM market_cap_daily ORDER BY date DESC LIMIT 90"
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
                    SUM(CASE WHEN market_cap >= 200e9                     THEN 1 ELSE 0 END) AS mega,
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

    def get_crossovers(self, date_str):
        """Detect crossovers within ≥200B (mega) and 100B–200B tiers."""
        with self._conn() as conn:
            prev_row = conn.execute(
                "SELECT MAX(date) FROM market_cap_daily WHERE date < ?", (date_str,)
            ).fetchone()
            prev_date = prev_row[0] if prev_row else None
        if not prev_date:
            return []

        def _get(d, lo, hi=None):
            cond = f"market_cap >= {lo}" if hi is None else f"market_cap >= {lo} AND market_cap < {hi}"
            with self._conn() as conn:
                rows = conn.execute(
                    f"SELECT ticker, company_name, market_cap FROM market_cap_daily "
                    f"WHERE date = ? AND {cond} ORDER BY market_cap DESC", (d,)
                ).fetchall()
            return {r[0]: {"company_name": r[1], "market_cap": r[2]} for r in rows}

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
            _detect(_get(date_str, 200e9),      _get(prev_date, 200e9),      "mega") +
            _detect(_get(date_str, 100e9, 200e9), _get(prev_date, 100e9, 200e9), "large")
        )
        return sorted(results, key=lambda x: x["winner_cap"], reverse=True)

    def get_ticker_history(self, tickers):
        placeholders = ",".join("?" * len(tickers))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT date, ticker, company_name, market_cap FROM market_cap_daily "
                f"WHERE ticker IN ({placeholders}) AND market_cap > 0 ORDER BY date ASC",
                tickers,
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Writes ────────────────────────────────────────────────────────────────

    def clear_all(self):
        with self._conn() as conn:
            conn.execute("DELETE FROM ticker_info")
            conn.execute("DELETE FROM market_cap_daily")

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
