import sqlite3
from pathlib import Path


TIERS = [
    ("mega",     200e9,   None),   # ≥$200B
    ("large",    100e9,  200e9),   # $100B–$200B
    ("mlarge",    10e9,  100e9),   # $10B–$100B
    ("mid",        2e9,   10e9),   # $2B–$10B
    ("small",    300e6,    2e9),   # $300M–$2B
]
TIER_MIN = 300e6   # lowest boundary we care about

def _tier(cap):
    for name, lo, hi in TIERS:
        if cap >= lo and (hi is None or cap < hi):
            return name
    return None


def _compute_crossovers(rows, target_dates):
    """Detect crossovers from raw DB rows across consecutive dates."""
    # Build {date: {ticker: {company_name, market_cap, tier}}}
    by_date = {}
    for r in rows:
        d, t, name, cap = r[0], r[1], r[2], r[3]
        if d not in by_date:
            by_date[d] = {}
        by_date[d][t] = {"company_name": name, "market_cap": float(cap), "tier": _tier(float(cap))}

    all_dates = sorted(by_date.keys())
    events = []

    for i in range(1, len(all_dates)):
        today_str, prev_str = all_dates[i], all_dates[i - 1]
        if today_str not in set(target_dates):
            continue
        today, prev = by_date[today_str], by_date[prev_str]
        common = [t for t in today if t in prev]

        for j in range(len(common)):
            for k in range(j + 1, len(common)):
                a, b = common[j], common[k]
                # Only compare within same tier
                if today[a]["tier"] != today[b]["tier"]:
                    continue
                at, bt = today[a]["market_cap"], today[b]["market_cap"]
                ap, bp = prev[a]["market_cap"],  prev[b]["market_cap"]
                winner = loser = None
                if at > bt and ap <= bp: winner, loser = a, b
                elif bt > at and bp <= ap: winner, loser = b, a
                if winner:
                    events.append({
                        "date":             today_str,
                        "tier":             today[winner]["tier"],
                        "winner":           winner,
                        "winner_name":      today[winner]["company_name"],
                        "winner_cap":       today[winner]["market_cap"],
                        "winner_prev_cap":  prev[winner]["market_cap"],
                        "loser":            loser,
                        "loser_name":       today[loser]["company_name"],
                        "loser_cap":        today[loser]["market_cap"],
                        "loser_prev_cap":   prev[loser]["market_cap"],
                    })
    return events

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

    def get_crossovers_batch(self, dates):
        """Fetch all mega/large cap rows for given dates + their prev day in ONE query,
        then detect crossovers in Python. Much faster than calling get_crossovers() N times."""
        if not dates:
            return []

        sorted_dates = sorted(set(dates))

        # Also need the trading day before the first date to detect crossovers on day 0
        with self._conn() as conn:
            prev_row = conn.execute(
                "SELECT MAX(date) FROM market_cap_daily WHERE date < ?", (sorted_dates[0],)
            ).fetchone()
            first_prev = prev_row[0] if prev_row and prev_row[0] else None

        all_needed = ([first_prev] if first_prev else []) + sorted_dates
        placeholders = ",".join("?" * len(all_needed))

        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT date, ticker, company_name, market_cap FROM market_cap_daily "
                f"WHERE date IN ({placeholders}) AND market_cap >= {TIER_MIN} ORDER BY date",
                all_needed,
            ).fetchall()

        return _compute_crossovers(rows, sorted_dates)

    def get_crossovers(self, date_str):
        """Detect crossovers across all tiers for a single date."""
        with self._conn() as conn:
            prev_row = conn.execute(
                "SELECT MAX(date) FROM market_cap_daily WHERE date < ?", (date_str,)
            ).fetchone()
            prev_date = prev_row[0] if prev_row and prev_row[0] else None
        if not prev_date:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT date, ticker, company_name, market_cap FROM market_cap_daily "
                f"WHERE date IN (?, ?) AND market_cap >= {TIER_MIN} ORDER BY date",
                (prev_date, date_str),
            ).fetchall()
        events = _compute_crossovers(rows, [date_str])
        return sorted(events, key=lambda x: x["winner_cap"], reverse=True)

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

    def clear_prices(self):
        """Clear only price/market-cap data; keep shares cache (ticker_info)."""
        with self._conn() as conn:
            conn.execute("DELETE FROM market_cap_daily")

    def clear_all(self):
        """Full reset — clears both prices and shares cache."""
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
