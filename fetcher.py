import logging
import warnings

import pandas as pd
import requests
import yfinance as yf

warnings.filterwarnings("ignore")
logger = logging.getLogger(__name__)

BATCH_SIZE = 200

EXCLUDE_TICKERS = {"GOOG", "GOOGL", "BRK-B"}

# ── Stock list ────────────────────────────────────────────────────────────────

def _is_common_stock(ticker: str) -> bool:
    """Light filter — NASDAQ Trader files already have explicit ETF flags.
    Only reject tickers with special chars or obviously non-stock formats."""
    if ticker in EXCLUDE_TICKERS:
        return False
    if not ticker or len(ticker) > 6:
        return False
    # Special chars indicate options, indices, futures
    if any(c in ticker for c in ("^", "~", "/", "+", "$", ".", "*")):
        return False
    return True


# Exchanges we accept from otherlisted.txt
# A=AMEX/NYSE American, N=NYSE, P=NYSE Arca, Q=NASDAQ, Z=CBOE BZX, V=IEX
_VALID_EXCHANGES = {"A", "N", "P", "Q", "Z", "V", "C"}


def _parse_nasdaqlisted(text: str, seen: set, result: list):
    """Parse nasdaqlisted.txt — NASDAQ-listed stocks with explicit ETF flag."""
    # Header: Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
    for line in text.splitlines()[1:]:
        parts = line.split("|")
        if len(parts) < 7:
            continue
        ticker   = parts[0].strip()
        name     = parts[1].strip()
        is_etf   = parts[6].strip() == "Y"
        test     = parts[3].strip() == "Y"
        if not ticker or ticker == "File Creation Time" or is_etf or test:
            continue
        if ticker in EXCLUDE_TICKERS or not _is_common_stock(ticker) or ticker in seen:
            continue
        seen.add(ticker)
        result.append({"ticker": ticker, "company_name": name, "sector": ""})


def _parse_otherlisted(text: str, seen: set, result: list):
    """Parse otherlisted.txt — NYSE/AMEX/Arca stocks with explicit ETF flag."""
    # Header: ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
    for line in text.splitlines()[1:]:
        parts = line.split("|")
        if len(parts) < 7:
            continue
        ticker   = parts[0].strip()
        name     = parts[1].strip()
        exchange = parts[2].strip()
        is_etf   = parts[4].strip() == "Y"
        test     = parts[6].strip() == "Y"
        if not ticker or ticker == "File Creation Time" or is_etf or test:
            continue
        if exchange not in _VALID_EXCHANGES:
            continue
        if ticker in EXCLUDE_TICKERS or not _is_common_stock(ticker) or ticker in seen:
            continue
        seen.add(ticker)
        result.append({"ticker": ticker, "company_name": name, "sector": ""})


def get_us_stock_list() -> list:
    """Return all US-listed common stocks (excl. ETFs/options).

    Primary:  NASDAQ Trader bulk text files — official, daily-updated, ETF-flagged.
    Fallback: Wikipedia S&P 500/400/600 + NASDAQ 100.
    """
    result: list = []
    seen:   set  = set()

    # ── Primary: NASDAQ Trader text files (no auth, GitHub Actions friendly) ─
    _TRADER_HDR = {"User-Agent": "Mozilla/5.0 research-bot/1.0"}
    success = 0
    for url, parser in [
        ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", _parse_nasdaqlisted),
        ("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",  _parse_otherlisted),
    ]:
        try:
            r = requests.get(url, headers=_TRADER_HDR, timeout=30)
            r.raise_for_status()
            before = len(result)
            parser(r.text, seen, result)
            logger.info(f"{url.split('/')[-1]}: +{len(result) - before} stocks")
            success += 1
        except Exception as e:
            logger.error(f"NASDAQ Trader fetch failed ({url}): {e}")

    if success > 0:
        logger.info(f"Total US stocks via NASDAQ Trader: {len(result)}")
        return result

    # ── Fallback: Wikipedia indices ──────────────────────────────────────────
    logger.warning("NASDAQ Trader failed — falling back to Wikipedia index lists")
    return _get_wikipedia_fallback()


def _wiki_table(url, symbol_col, name_col, sector_col=None) -> list:
    try:
        tables = pd.read_html(url, storage_options={"User-Agent": "Mozilla/5.0"})
        df = tables[0]
        result = []
        for _, row in df.iterrows():
            ticker  = str(row.get(symbol_col, "")).strip().replace(".", "-")
            company = str(row.get(name_col, "")).strip()
            sector  = str(row.get(sector_col, "")).strip() if sector_col else ""
            if ticker and ticker not in EXCLUDE_TICKERS and _is_common_stock(ticker):
                result.append({"ticker": ticker, "company_name": company, "sector": sector})
        return result
    except Exception as e:
        logger.error(f"Wikipedia fetch failed ({url}): {e}")
        return []


def _get_wikipedia_fallback() -> list:
    seen, result = set(), []
    sources = [
        ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
         "Symbol", "Security", "GICS Sector"),
        ("https://en.wikipedia.org/wiki/Nasdaq-100",
         "Ticker", "Company", "GICS Sector"),
        ("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
         "Ticker symbol", "Company", "GICS Sector"),
        ("https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
         "Ticker symbol", "Company", "GICS Sector"),
    ]
    for url, sym, name, sec in sources:
        for item in _wiki_table(url, sym, name, sec):
            if item["ticker"] not in seen:
                seen.add(item["ticker"])
                result.append(item)
    logger.info(f"Wikipedia fallback: {len(result)} tickers")
    return result


# ── Market cap bulk fetch (Yahoo Finance quote API) ───────────────────────────

_YF_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
_YF_FIELDS    = "symbol,marketCap,regularMarketPrice,sharesOutstanding,longName,shortName,sector,quoteType"
_YF_HEADERS   = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_CHUNK        = 100   # tickers per request


def fetch_shares_bulk(items: list, info_map: dict) -> dict:
    """Fetch market caps for all items using Yahoo Finance bulk quote API.
    Returns {ticker: {company_name, sector, shares}}.
    70 requests for 7000 stocks vs 7000 individual calls.
    """
    tickers = [it["ticker"] for it in items]
    results: dict = {}

    for i in range(0, len(tickers), _CHUNK):
        batch = tickers[i: i + _CHUNK]
        try:
            r = requests.get(
                _YF_QUOTE_URL,
                params={"symbols": ",".join(batch), "fields": _YF_FIELDS},
                headers=_YF_HEADERS,
                timeout=20,
            )
            r.raise_for_status()
            quotes = r.json().get("quoteResponse", {}).get("result", []) or []

            for q in quotes:
                t = q.get("symbol", "")
                if not t:
                    continue
                qtype = q.get("quoteType", "")
                if qtype not in ("EQUITY", ""):   # skip ETF, CRYPTOCURRENCY, etc.
                    continue

                market_cap = float(q.get("marketCap")          or 0)
                price      = float(q.get("regularMarketPrice") or 0)
                shares_out = float(q.get("sharesOutstanding")  or 0)

                if market_cap > 0 and price > 0:
                    effective_shares = market_cap / price
                elif shares_out > 0:
                    effective_shares = shares_out
                else:
                    continue    # no usable data for this ticker

                src = info_map.get(t, {})
                company_name = (q.get("longName") or q.get("shortName")
                                or src.get("company_name", t))
                sector = q.get("sector") or src.get("sector", "")

                results[t] = {
                    "company_name": company_name,
                    "sector":       sector,
                    "shares":       effective_shares,
                }
        except Exception as e:
            logger.error(f"Bulk quote fetch failed (batch {i // _CHUNK + 1}): {e}")

        if (i // _CHUNK + 1) % 10 == 0:
            logger.info(f"  bulk quote batches done: {i // _CHUNK + 1}/{(len(tickers) + _CHUNK - 1) // _CHUNK}")

    logger.info(f"Bulk fetch: got data for {len(results)}/{len(tickers)} tickers")
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

    # 1. Fetch market caps for new/uncached tickers (bulk API, ~1 min for 7000 stocks)
    missing = db.get_missing_shares(all_tickers)
    if missing:
        logger.info(f"Bulk-fetching market cap for {len(missing)} new tickers…")
        missing_items = [info_map[t] for t in missing if t in info_map]
        shares_data   = fetch_shares_bulk(missing_items, info_map)
        db.upsert_ticker_info(shares_data)
        logger.info(f"Cached {len(shares_data)}/{len(missing)} tickers")

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
