import logging
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")
logger = logging.getLogger(__name__)

MAX_WORKERS = 20
BATCH_SIZE  = 200
EXCLUDE_TICKERS = {"GOOG", "GOOGL", "BRK-B"}


# ── Stock list ────────────────────────────────────────────────────────────────

def get_sp500_list():
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        tables = pd.read_html(url, storage_options={"User-Agent": "Mozilla/5.0"})
        df = tables[0]
        result = []
        for _, row in df.iterrows():
            ticker  = str(row.get("Symbol", "")).strip().replace(".", "-")
            company = str(row.get("Security", row.get("Company", ""))).strip()
            sector  = str(row.get("GICS Sector", "")).strip()
            if ticker and ticker not in EXCLUDE_TICKERS:
                result.append({"ticker": ticker, "company_name": company, "sector": sector})
        logger.info(f"S&P 500 list: {len(result)} tickers")
        return result
    except Exception as e:
        logger.error(f"Failed to fetch S&P 500 list: {e}")
        return []


# ── Shares outstanding (parallel, cached) ────────────────────────────────────

def _fetch_one_shares(item):
    ticker = item["ticker"]
    try:
        info = yf.Ticker(ticker).info
        market_cap    = float(info.get("marketCap")      or 0)
        current_price = float(info.get("currentPrice")   or info.get("regularMarketPrice") or 0)
        # Use marketCap/price as effective shares — correctly handles multi-class shares (GOOG, BRK, etc.)
        if market_cap > 0 and current_price > 0:
            effective_shares = market_cap / current_price
        else:
            effective_shares = float(info.get("sharesOutstanding") or 0)
        return {
            "ticker":       ticker,
            "company_name": info.get("longName") or info.get("shortName") or item["company_name"],
            "sector":       info.get("sector") or item.get("sector") or "",
            "shares":       effective_shares,
            "ok":           True,
        }
    except Exception as e:
        logger.debug(f"{ticker}: {e}")
        return {"ticker": ticker, "ok": False}


def fetch_shares_parallel(items):
    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_one_shares, it): it for it in items}
        done = 0
        for fut in as_completed(futures):
            r = fut.result()
            done += 1
            if r["ok"] and r["shares"] > 0:
                results[r["ticker"]] = {
                    "company_name": r["company_name"],
                    "sector":       r["sector"],
                    "shares":       r["shares"],
                }
            if done % 100 == 0:
                logger.info(f"  shares fetched: {done}/{len(items)}")
    return results


# ── Historical price download ─────────────────────────────────────────────────

def download_prices(tickers, period="65d"):
    """Batch download closing prices. Returns {date: {ticker: price}}."""
    all_data = {}
    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i : i + BATCH_SIZE]
        try:
            df = yf.download(batch, period=period, auto_adjust=True, progress=False)
            if df.empty:
                continue
            close = df["Close"]
            # MultiIndex columns when batch > 1; Series when batch == 1
            if isinstance(close, pd.Series):
                close = close.to_frame(name=batch[0])
            for ts, row in close.iterrows():
                day = ts.strftime("%Y-%m-%d")
                if day not in all_data:
                    all_data[day] = {}
                for col, price in row.items():
                    if pd.notna(price) and price > 0:
                        all_data[day][str(col)] = float(price)
        except Exception as e:
            logger.error(f"Batch download error (i={i}): {e}")
    logger.info(f"Price data: {len(all_data)} trading days")
    return all_data


# ── Main entry point ──────────────────────────────────────────────────────────

def fetch_and_store(db, period="65d"):
    """Fetch historical prices + shares, compute market cap, store in DB."""
    stocks     = get_sp500_list()
    if not stocks:
        logger.error("No stock list"); return 0

    all_tickers = [s["ticker"] for s in stocks]
    info_map    = {s["ticker"]: s for s in stocks}

    # 1. Ensure shares are cached
    missing = db.get_missing_shares(all_tickers)
    if missing:
        logger.info(f"Fetching shares for {len(missing)} tickers…")
        missing_items = [info_map[t] for t in missing if t in info_map]
        shares_data   = fetch_shares_parallel(missing_items)
        db.upsert_ticker_info(shares_data)
        logger.info(f"Cached {len(shares_data)} tickers' shares")

    shares_map = db.get_all_shares()   # {ticker: shares}

    # 2. Download historical prices
    price_data = download_prices(all_tickers, period=period)

    # 3. Calculate change_pct from consecutive days
    sorted_dates = sorted(price_data.keys())
    total = 0

    for idx, day in enumerate(sorted_dates):
        if db.has_data_for_date(day):
            continue

        prices_today = price_data[day]
        prices_prev  = price_data[sorted_dates[idx - 1]] if idx > 0 else {}

        records = []
        for ticker, close in prices_today.items():
            if close <= 0:
                continue
            shares     = shares_map.get(ticker, 0)
            market_cap = close * shares if shares > 0 else 0
            prev_close = prices_prev.get(ticker)
            change_pct = ((close - prev_close) / prev_close * 100) if prev_close else 0.0
            info       = info_map.get(ticker, {})
            records.append((
                day, ticker,
                info.get("company_name", ticker),
                info.get("sector", ""),
                close, market_cap, shares, round(change_pct, 4),
            ))

        if records:
            db.insert_batch(records)
            total += len(records)
            logger.info(f"  {day}: {len(records)} records")

    return total
