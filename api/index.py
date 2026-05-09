import sys
import os
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402 — imports db = Database() from database_pg

# Create tables on cold start; log but don't crash if DB is unavailable
try:
    import app as _m
    _m.db.init_db()
except Exception as e:
    logging.error(f"DB init error (check DATABASE_URL): {e}")
