import logging
import os
import threading
from datetime import date

from flask import Flask, jsonify, render_template, request

if os.environ.get("DATABASE_URL"):
    from database_pg import Database
else:
    from database import Database

import fetcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
db = Database()

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/dates")
def api_dates():
    return jsonify(db.get_dates())


@app.route("/api/sectors")
def api_sectors():
    dates = db.get_dates()
    if not dates:
        return jsonify([])
    return jsonify(db.get_sectors(dates[0]))


@app.route("/api/stocks")
def api_stocks():
    date_str = request.args.get("date")
    if not date_str:
        dates = db.get_dates()
        if not dates:
            return jsonify({"error": "no data"}), 404
        date_str = dates[0]

    rows = db.get_data_for_date(date_str)
    summary = db.get_summary(date_str)
    return jsonify({"date": date_str, "summary": summary, "data": rows})


@app.route("/api/crossovers")
def api_crossovers():
    date_str = request.args.get("date")
    if not date_str:
        dates = db.get_dates()
        if not dates:
            return jsonify([])
        date_str = dates[0]
    return jsonify(db.get_crossovers(date_str))


@app.route("/api/history")
def api_history():
    raw = request.args.get("tickers", "")
    tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
    if not tickers:
        return jsonify([])
    return jsonify(db.get_ticker_history(tickers))


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    if os.environ.get("DATABASE_URL"):
        return jsonify({
            "message": "資料由 GitHub Actions 每個交易日美股收盤後自動更新。"
                       "如需立即更新，請至 GitHub → Actions → Run workflow。"
        })

    def _run():
        count = fetcher.fetch_and_store(db)
        logger.info(f"Refresh done: {count} records")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"message": "Fetching data in background (may take 1–2 minutes)…"})


# ── Startup ───────────────────────────────────────────────────────────────────

def bootstrap():
    db.init_db()
    dates = db.get_dates()
    today = date.today().isoformat()
    if not dates:
        logger.info("Empty DB — starting initial fetch…")
        fetcher.fetch_and_store(db)
    elif today not in dates:
        logger.info("Fetching today's data…")
        fetcher.fetch_and_store(db)
    else:
        logger.info(f"DB ready ({len(dates)} dates)")


if __name__ == "__main__":
    bootstrap()
    app.run(debug=False, port=5001, use_reloader=False)
