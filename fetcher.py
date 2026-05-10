import logging
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
import yfinance as yf

warnings.filterwarnings("ignore")
logger = logging.getLogger(__name__)

MAX_WORKERS = 30
BATCH_SIZE  = 200

EXCLUDE_TICKERS = {"GOOG", "GOOGL", "BRK-B"}

# Suffixes that indicate non-common-stock instruments
_BAD_ENDS = ("W", "WS", "WI", "R", "RI", "U", "Q", "P", "A", "B")


# ── Stock list ────────────────────────────────────────────────────────────────

def _is_common_stock(ticker: str) -> bool:
    """Rough filter: skip warrants, rights, units, preferred, bankrupt stubs."""
    if ticker in EXCLUDE_TICKERS:
        return False
    if len(ticker) > 5:          # tickers longer than 5 chars are usually derivatives
        return False
    if any(c in ticker for c in ("^", "~", "/")):
        return False
    # Skip e.g. "AAAPLW" (warrant) or "XYZR" (right) by checking trailing letter patterns
    for suf in _BAD_ENDS:
        if len(ticker) > len(suf) and ticker.endswith(suf) and ticker[-len(suf)-1:][0].isdigit() is False:
            # heuristic: only flag if last non-suffix part looks like a normal ticker
            stripped = ticker[: -len(suf)]
            if stripped.isalpha() and len(stripped) >= 2:
                return False
    return True


def _fetch_exchange(exchange: str) -> list:
    url = (
        "https://api.nasdaq.com/api/screener/stocks"
        f"?tableonly=true&exchange={exchange}&download=true"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nasdaq.com/",
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    rows = r.json().get("data", {}).get("rows", []) or []
    logger.info(f"{exchange}: {len(rows)} raw rows")
    return rows


def get_us_stock_list() -> list:
    """Return all common stocks from NYSE + NASDAQ + AMEX, falling back to S&P 500."""
    result, seen = [], set()

    for exchange in ("NASDAQ", "NYSE", "AMEX"):
        try:
            rows = _fetch_exchange(exchange)
            for row in rows:
                ticker = str(row.get("symbol", "")).strip().upper().replace(".", "-")
                if not ticker or ticker in seen:
                    continue
                if not _is_common_stock(ticker):
                    continue
                name   = str(row.get("name",   "")).strip()
                sector = str(row.get("sector", "")).strip()
                # Skip obvious ETFs / funds by name keyword
                nl = name.lower()
                if any(kw in nl for kw in (" etf", " fund", " trust", "ishares", "spdr", "invesco ", "vanguard ")):
                    continue
                seen.add(ticker)
                result.append({"ticker": ticker, "company_name": name, "sector": sector})
        except Exception as e:
            logger.error(f"Failed to fetch {exchange}: {e}")

    if result:
        logger.info(f"Total US stocks: {len(result)}")
        return result

    # Fallback to S&P 500 from Wikipedia
    logger.warning("NASDAQ API failed — falling back to S&P 500 list")
    return _get_sp500_fallback()


def _get_sp500_fallback() -> list:
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        tables = pd.read_html(url, storage_options={"User-Agent": "Mozilla/5.0"})
        df = tables[0]
        result = []
        for _, row in df.iterrows():
            ticker  = str(row.get("Symbol", "")).strip().replace(".", "-")
            company = str(row.get("Security", "")).strip()
            sector  = str(row.get("GICS Sector", "")).strip()
            if ticker and ticker not in EXCLUDE_TICKERS:
                result.append({"ticker": ticker, "company_name": company, "sector": sector})
        logger.info(f"S&P 500 fallback: {len(result)} tickers")
        return result
    except Exception as e:
        logger.error(f"S&P 500 fallback failed: {e}")
        return []


# ── Shares outstanding (parallel, cached) ────────────────────────────────────

def _fetch_one_shares(item):
    ticker = item["ticker"]
    try:
        info          = yf.Ticker(ticker).info
        market_cap    = float(info.get("marketCap")    or 0)
        current_price = float(info.get("currentPrice") or info.get("regularMarketPrice") or 0)
        effective_shares = (
            market_cap / current_price
            if market_cap > 0 and current_price > 0
            else float(info.get("sharesOutstanding") or 0)
        )
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


def fetch_shares_parallel(items, max_workers=MAX_WORKERS):
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
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
            if done % 200 == 0:
                logger.info(f"  shares fetched: {done}/{len(items)}")
    return results


# ── Historical price download ─────────────────────────────────────────────────

def download_prices(tickers, period="65d"):
    """Batch download closing prices. Returns {date: {ticker: price}}."""
    all_data = {}
    total_batches = (len(tickers) + BATCH_SIZE - 1) // BATCH_SIZE
    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i: i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        try:
            df = yf.download(batch, period=period, auto_adjust=True, progress=False)
            if df.empty:
                continue
            close = df["Close"]
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
            logger.error(f"Batch {batch_num}/{total_batches} error: {e}")
        if batch_num % 10 == 0:
            logger.info(f"  price batches done: {batch_num}/{total_batches}")
    logger.info(f"Price data: {len(all_data)} trading days, {len(tickers)} tickers")
    return all_data


# ── Main entry point ──────────────────────────────────────────────────────────

def fetch_and_store(db, period="65d"):
    stocks = get_us_stock_list()
    if not stocks:
        logger.error("No stock list"); return 0

    all_tickers = [s["ticker"] for s in stocks]
    info_map    = {s["ticker"]: s for s in stocks}
    logger.info(f"Stock universe: {len(all_tickers)} tickers")

    # 1. Fetch shares for new/uncached tickers only
    missing = db.get_missing_shares(all_tickers)
    if missing:
        logger.info(f"Fetching shares for {len(missing)} new tickers…")
        missing_items = [info_map[t] for t in missing if t in info_map]
        shares_data   = fetch_shares_parallel(missing_items)
        db.upsert_ticker_info(shares_data)
        logger.info(f"Cached {len(shares_data)} tickers' shares")

    shares_map = db.get_all_shares()

    # 2. Batch download historical prices
    price_data = download_prices(all_tickers, period=period)

    # 3. Build records; skip if market_cap = 0
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
            if market_cap <= 0:
                continue          # skip tickers without shares data
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
