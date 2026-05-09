"""Entry point for GitHub Actions daily data fetch."""
import logging
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

if not os.environ.get("DATABASE_URL"):
    raise SystemExit("DATABASE_URL is not set")

from database_pg import Database
import fetcher

db = Database()
db.init_db()

if os.environ.get("FORCE_REFETCH", "").lower() == "true":
    logging.info("FORCE_REFETCH=true — clearing all cached data")
    db.clear_all()

count = fetcher.fetch_and_store(db, period="65d")
logging.info(f"Done — {count} new records written")
