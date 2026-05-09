import logging
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")
logger = logging.getLogger(__name__)

MAX_WORKERS = 30


# ── Stock list ────────────────────────────────────────────────────────────────

def get_sp500_list():
    """Fetch S&P 500 tickers from Wikipedia."""
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        tables = pd.read_html(url, storage_options={"User-Agent": "Mozilla/5.0"})
        df = tables[0]
        result = []
        for _, row in df.iterrows():
            ticker = str(row.get("Symbol", "")).strip().replace(".", "-")
            company = str(row.get("Security", row.get("Company", ""))).strip()
            sector = str(row.get("GICS Sector", "")).strip()
            industry = str(row.get("GICS Sub-Industry", "")).strip()
            if ticker:
                result.append(
                    {"ticker": ticker, "company_name": company, "sector": sector, "industry": industry}
                )
        logger.info(f"S&P 500 list: {len(result)} stocks")
        return result
    except Exception as e:
        logger.error(f"Failed to fetch S&P 500 list: {e}")
        return []


# ── Data fetch ────────────────────────────────────────────────────────────────

def _fetch_one(item):
    """Fetch market cap info for a single ticker dict."""
    ticker = item["ticker"]
    try:
        t = yf.Ticker(ticker)
        info = t.info
        market_cap = info.get("marketCap") or 0
        price = info.get("currentPrice") or info.get("regularMarketPrice") or 0
        shares = info.get("sharesOutstanding") or 0
        change_pct = info.get("regularMarketChangePercent") or 0
        company_name = (
            info.get("longName") or info.get("shortName") or item["company_name"] or ticker
        )
        sector = info.get("sector") or item.get("sector") or ""
        return {
            "ticker": ticker,
            "company_name": company_name,
            "sector": sector,
            "market_cap": float(market_cap),
            "price": float(price),
            "shares": float(shares),
            "change_pct": float(change_pct),
            "ok": True,
        }
    except Exception as e:
        logger.debug(f"{ticker} error: {e}")
        return {"ticker": ticker, "ok": False}


def fetch_all(items):
    """Parallel fetch for all stock items."""
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_one, item): item for item in items}
        done = 0
        for fut in as_completed(futures):
            r = fut.result()
            done += 1
            if r["ok"]:
                results.append(r)
            if done % 100 == 0:
                logger.info(f"  fetched {done}/{len(items)}")
    return results


# ── Entry point ───────────────────────────────────────────────────────────────

def fetch_and_store(db):
    today = date.today().isoformat()

    if db.has_data_for_date(today):
        logger.info(f"Data for {today} already up-to-date")
        return 0

    stocks = get_sp500_list()
    if not stocks:
        logger.error("Could not get stock list")
        return 0

    logger.info(f"Fetching market cap for {len(stocks)} stocks…")
    results = fetch_all(stocks)
    logger.info(f"Received data for {len(results)}/{len(stocks)} stocks")

    records = [
        (
            today,
            r["ticker"],
            r["company_name"],
            r["sector"],
            r["price"],
            r["market_cap"],
            r["shares"],
            r["change_pct"],
        )
        for r in results
        if r["market_cap"] > 0
    ]

    if records:
        db.insert_batch(records)
        logger.info(f"Stored {len(records)} records for {today}")

    return len(records)
