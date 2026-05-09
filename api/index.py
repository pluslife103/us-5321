import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database_pg import Database
_db = Database()
_db.init_db()

from app import app  # noqa: E402
