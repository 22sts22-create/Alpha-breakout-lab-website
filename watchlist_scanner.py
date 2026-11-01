"""
AlphaDesk Personal Watchlist Scanner  (website-ready)
=====================================================
Finds stocks where MA15 crosses above MA65
Filters: Price $3-400, Volume >= 500K
Includes recent news headlines from Alpaca
You check the options chain manually

Uses free Alpaca IEX feed — no subscription needed

WEBSITE CHANGES vs. the personal version:
  1. Keys load from ENVIRONMENT VARIABLES (GitHub Secrets) first, then fall back
     to config.txt for local use. config.txt is NEVER required and must NOT be
     committed to the repo.
  2. Non-interactive in CI: when run by GitHub Actions (CI=true), it skips every
     "Press Enter" prompt so the job can't hang.
  3. Writes scanner_results.json (machine-readable) so the website can render it.
     Set SCANNER_OUTPUT to control where the JSON is written.
  Your local console output is unchanged.
"""

import requests
import numpy as np
import os
import re
import json
from datetime import datetime, timedelta, timezone

# ── CI / interactivity ────────────────────────────────────────
# GitHub Actions sets CI=true. When running there we must never call input().
IS_CI = os.environ.get('CI', '').lower() == 'true' or os.environ.get('SCANNER_CI', '') == '1'

def _pause(msg="\n  Press Enter to exit..."):
    """input() locally, no-op in CI so the job doesn't hang."""
    if IS_CI:
        return
    try:
        input(msg)
    except EOFError:
        pass

# ── CONFIG ────────────────────────────────────────────────────
def read_config():
    """Load config from config.txt if present, then let environment variables
    override/supply any keys. Env vars win so GitHub Secrets take precedence in CI.
    config.txt is optional — missing is fine (CI supplies keys via env)."""
    config = {}
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.txt')
    try:
        with open(config_path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'): continue
                if '=' not in line: continue
                key, val = line.split('=', 1)
                config[key.strip()] = val.strip()
    except FileNotFoundError:
        pass  # fine — env vars (GitHub Secrets) will supply the keys

    # Environment variables override config.txt
    for k in ('ALPACA_API_KEY', 'ALPACA_SECRET_KEY', 'NEWS_KEYWORDS'):
        if os.environ.get(k):
            config[k] = os.environ[k]
    return config

cfg        = read_config()
API_KEY    = cfg.get('ALPACA_API_KEY', '')
SECRET_KEY = cfg.get('ALPACA_SECRET_KEY', '')

if not API_KEY or not SECRET_KEY:
    print("ERROR: Alpaca keys not found. Set them in config.txt (local) or as "
          "ALPACA_API_KEY / ALPACA_SECRET_KEY environment variables / GitHub Secrets (CI).")
    _pause()
    raise SystemExit(1)

# ── NEWS KEYWORDS ─────────────────────────────────────────────
# Comma-separated words/phrases in config.txt or the NEWS_KEYWORDS env var, e.g.:
#   NEWS_KEYWORDS = earnings, FDA, upgrade, acquisition, stock split, guidance
# Case-insensitive, whole-word/phrase match. Leave blank to flag nothing.
KEYWORDS = [
    k.strip().lower()
    for k in cfg.get('NEWS_KEYWORDS', '').split(',')
    if k.strip()
]

# Positive / negative keyword sets for a crude news-sentiment read on the PUBLIC
# page. We publish only "positive / negative / mixed" — never the headline text.
# Override in config.txt with NEWS_POSITIVE= / NEWS_NEGATIVE= comma lists.
_DEF_POS = ("upgrade,upgraded,beats,beat,record,approval,approved,partnership,acquisition,"
            "acquire,buyback,raises,raised,outperform,breakthrough,wins,awarded,contract,"
            "surges,soars,rally,jumps,tops,expands,launches")
_DEF_NEG = ("downgrade,downgraded,miss,misses,lawsuit,sued,investigation,probe,recall,cuts,"
            "lowered,warning,bankruptcy,delay,delayed,halt,halted,crl,rejection,rejected,"
            "fraud,plunges,falls,slashes,layoffs,resigns,charges,warns")
NEWS_POSITIVE = [k.strip().lower() for k in cfg.get('NEWS_POSITIVE', _DEF_POS).split(',') if k.strip()]
NEWS_NEGATIVE = [k.strip().lower() for k in cfg.get('NEWS_NEGATIVE', _DEF_NEG).split(',') if k.strip()]

def classify_news_sentiment(headlines):
    """Crude positive/negative read from the configured keyword sets.
    Returns 'positive', 'negative', 'mixed', or None. Never exposes the headline."""
    pos = neg = 0
    for h in headlines:
        text = (h or "").lower()
        for kw in NEWS_POSITIVE:
            if re.search(r'\b' + re.escape(kw) + r'\b', text): pos += 1
        for kw in NEWS_NEGATIVE:
            if re.search(r'\b' + re.escape(kw) + r'\b', text): neg += 1
    if pos and neg: return "mixed"
    if pos:         return "positive"
    if neg:         return "negative"
    return None

HEADERS = {
    "APCA-API-KEY-ID":     API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
}
DATA_URL = "https://data.alpaca.markets"

# Where to write the machine-readable results for the website.
OUTPUT_PATH = os.environ.get(
    'SCANNER_OUTPUT',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scanner_results.json')
)

# ── SCAN UNIVERSE ─────────────────────────────────────────────
# Grouped so the website can show which stocks are tracked. To add a ticker,
# drop it into the right category (or make a new one) — the scan and the public
# "what we track" list both read from this.
WATCHLIST_GROUPS = {
    "AI / Tech":        ["NVDA","AMD","PLTR","SMCI","ARM","AVGO","MRVL","INTC","QCOM","TSM"],
    "Space / Defense":  ["ASTS","LUNR","RKLB","RDW","LHX","RTX","NOC","GD","SPCX"],
    "Fintech":          ["SOFI","UPST","AFRM","LC","PYPL","SQ","NU","HOOD"],
    "Semiconductors":   ["MU","WOLF","ON","SLAB","MTSI","SWKS","QRVO","MCHP","QBTS","RGTI","QUBT"],
    "Biotech":          ["RXRX","BEAM","EDIT","NTLA","SANA","ARKG"],
    "Energy / Nuclear": ["SMR","OKLO","UUUU","CCJ","NNE","BWXT","VST","CEG"],
    "EV / Auto":        ["TSLA","RIVN","LCID"],
    "Consumer":         ["AMZN","META","GOOGL","NFLX","SPOT","SNAP","PINS"],
    "Finance":          ["JPM","BAC","GS","MS","C","WFC"],
    "Healthcare":       ["UNH","CVS","HCA","THC"],
    "Industrial":       ["CAT","DE","HON","MMM","GE","ETN"],
    "Momentum":         ["CADL","TSHA","PAWM","APP","COIN","MSTR","HUDI","IIPR","BULL","GTEX"],
}
# Flatten to a de-duplicated scan list (preserves order, drops any repeats).
WATCHLIST = list(dict.fromkeys(t for group in WATCHLIST_GROUPS.values() for t in group))

# ── WATCHLIST DATE TRACKING ───────────────────────────────────
# Remembers when each ticker first appeared so we can show "added" date
# and "days on watchlist". Stored in watchlist_dates.json next to this file.
_dates_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'watchlist_dates.json')

def load_watchlist_dates():
    try:
        with open(_dates_path, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}

def update_watchlist_dates():
    """Stamp any ticker not seen before with today's date. Returns the full map."""
    dates = load_watchlist_dates()
    today = datetime.now().strftime('%Y-%m-%d')
    changed = False
    for t in WATCHLIST:
        if t not in dates:
            dates[t] = today
            changed = True
    if changed:
        try:
            with open(_dates_path, 'w') as f:
                json.dump(dates, f, indent=2)
        except Exception as e:
            print(f"  (could not save watchlist dates: {e})")
    return dates

def days_on_watchlist(added_date):
    try:
        d = datetime.strptime(added_date, '%Y-%m-%d')
        return (datetime.now() - d).days
    except Exception:
        return 0

# ── FILTERS ───────────────────────────────────────────────────
MIN_PRICE  = 3
MAX_PRICE  = 400
MIN_VOLUME = 500_000

# ── HELPERS ───────────────────────────────────────────────────
def get_daily_bars(ticker, days=300):
    try:
        end   = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        params = {
            "timeframe": "1Day",
            "start":     start.strftime("%Y-%m-%dT00:00:00Z"),
            "end":       end.strftime("%Y-%m-%dT00:00:00Z"),
            "limit":     300,
            "feed":      "iex",
        }
        bars = []
        page_token = None
        while True:
            page_params = dict(params)
            if page_token:
                page_params["page_token"] = page_token
            r = requests.get(
                f"{DATA_URL}/v2/stocks/{ticker}/bars",
                headers=HEADERS, params=page_params, timeout=10
            )
            if r.status_code != 200: return None
            data = r.json()
            bars.extend(data.get('bars') or [])
            page_token = data.get('next_page_token')
            if not page_token:
                break
        if len(bars) < 70: return None
        return bars
    except: return None

def calc_sma(arr, period):
    if len(arr) < period: return None
    return float(np.mean(arr[-period:]))

def calc_rsi(closes, period=14):
    """Standard Wilder RSI on the close series. Returns 0-100, or None if not enough data."""
    if len(closes) < period + 1:
        return None
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    # seed with simple average of first `period`
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    # Wilder smoothing over the rest
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def rsi_label(rsi):
    """Short tag describing the RSI zone, for the scan output."""
    if rsi is None:
        return ""
    if rsi >= 70:   return "overbought"
    if rsi >= 60:   return "strong"
    if rsi <= 30:   return "oversold"
    if rsi <= 40:   return "weak"
    return "neutral"

def avg_volume(bars, days=20):
    vols = [b['v'] for b in bars[-days:]]
    return np.mean(vols) if vols else 0

def volume_surge(bars, lookback=20):
    """Compare the most recent bar's volume to the trailing average (excluding today).
    Returns a multiple, e.g. 2.5 means today is 2.5x the recent average. None if n/a."""
    if len(bars) < lookback + 1:
        return None
    recent = bars[-1]['v']
    prior = [b['v'] for b in bars[-lookback-1:-1]]  # the `lookback` bars before today
    base = np.mean(prior) if prior else 0
    if base <= 0:
        return None
    return recent / base

# ── KEYWORD MATCHING ──────────────────────────────────────────
def match_keywords(headline):
    """Return the list of configured keywords/phrases found in a headline.
    Whole-word / whole-phrase, case-insensitive. 'AI' won't match 'said'."""
    if not KEYWORDS:
        return []
    text = headline.lower()
    hits = []
    for kw in KEYWORDS:
        # \b word boundaries so 'AI' matches 'AI' but not 'again'
        if re.search(r'\b' + re.escape(kw) + r'\b', text):
            hits.append(kw)
    return hits

# ── GET NEWS ──────────────────────────────────────────────────
def get_news(ticker, days=5):
    try:
        end   = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        params = {
            "symbols": ticker,
            "start":   start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit":   3,
            "sort":    "desc",
        }
        r = requests.get(
            f"{DATA_URL}/v1beta1/news",
            headers=HEADERS, params=params, timeout=10
        )
        if r.status_code != 200: return []
        articles = r.json().get('news', [])
        return [a.get('headline', '')[:75] for a in articles]
    except: return []

# ── WRITE RESULTS FOR THE WEBSITE ─────────────────────────────
def write_results_json(results, news_hits):
    """Write a machine-readable snapshot the website can render.
    NOTE: 'news' headlines are third-party content — see the publishing note in
    the build plan before showing them on a public page."""
    payload = {
        "generated_at":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_date": datetime.now().strftime("%Y-%m-%d"),
        "universe_size": len(WATCHLIST),
        "universe_groups": WATCHLIST_GROUPS,
        "data_source":   "Alpaca IEX (end-of-day)",
        "filters": {
            "min_price":  MIN_PRICE,
            "max_price":  MAX_PRICE,
            "min_volume": MIN_VOLUME,
            "strategy":   "MA15 x MA65 crossover",
        },
        "results": [
            {
                "ticker":     r["ticker"],
                "price":      float(r["price"]),
                "ma15":       float(r["ma15"]),
                "ma65":       float(r["ma65"]),
                "gap_pct":    float(r["gap_pct"]),
                "avg_vol":    int(r["avg_vol"]),
                "rsi":        (round(float(r["rsi"]), 1) if r["rsi"] is not None else None),
                "rsi_label":  rsi_label(r["rsi"]),
                "vsurge":     (round(float(r["vsurge"]), 2) if r["vsurge"] is not None else None),
                "freshness":  r["freshness"],
                "added_date": r.get("added_date"),
                "days_on_watchlist": r.get("days_on_watchlist"),
                "keyword_hit": bool(r.get("keyword_hit")),
                "news_sentiment": r.get("news_sentiment"),
                "news":       r.get("news", []),
            }
            for r in results
        ],
        "news_hits": [
            {
                "ticker":  t,
                "tags":    sorted({kw for _, hits in matched for kw in hits}),
                "headlines": [h for h, _ in matched],
            }
            for t, matched in news_hits
        ],
    }
    try:
        with open(OUTPUT_PATH, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"\n  → wrote {len(results)} result(s) to {OUTPUT_PATH}")
    except Exception as e:
        print(f"\n  (could not write results JSON: {e})")

# ── SCAN ──────────────────────────────────────────────────────
def scan():
    print("=" * 70)
    print("  ALPHADESK PERSONAL WATCHLIST SCANNER")
    print(f"  Price ${MIN_PRICE}-${MAX_PRICE} | Vol >={MIN_VOLUME/1e6:.1f}M | MA15 x MA65 | + News")
    print(f"  Universe: {len(WATCHLIST)} stocks")
    if KEYWORDS:
        print(f"  Keywords: {', '.join(KEYWORDS)}  (🔥 = match)")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 70)
    print(f"\n  Scanning...\n")

    # Stamp any new tickers with today's date; load the full date map
    _wl_dates = update_watchlist_dates()

    results = []

    for ticker in WATCHLIST:
        bars = get_daily_bars(ticker)
        if not bars: continue

        closes  = [b['c'] for b in bars]
        volumes = [b['v'] for b in bars]
        price   = closes[-1]
        avg_vol = avg_volume(bars)

        if price < MIN_PRICE or price > MAX_PRICE: continue
        if avg_vol < MIN_VOLUME: continue

        ma15      = calc_sma(closes, 15)
        ma65      = calc_sma(closes, 65)
        ma15_prev = calc_sma(closes[:-1], 15)
        ma65_prev = calc_sma(closes[:-1], 65)

        if not all([ma15, ma65, ma15_prev, ma65_prev]): continue

        crossed_today     = ma15_prev <= ma65_prev and ma15 > ma65
        crossed_yesterday = False
        crossed_2days     = False

        if len(closes) > 2:
            ma15_2 = calc_sma(closes[:-2], 15)
            ma65_2 = calc_sma(closes[:-2], 65)
            if ma15_2 and ma65_2:
                crossed_yesterday = ma15_2 <= ma65_2 and ma15_prev > ma65_prev

        if len(closes) > 3:
            ma15_3  = calc_sma(closes[:-3], 15)
            ma65_3  = calc_sma(closes[:-3], 65)
            ma15_2d = calc_sma(closes[:-2], 15)
            ma65_2d = calc_sma(closes[:-2], 65)
            if all([ma15_3, ma65_3, ma15_2d, ma65_2d]):
                crossed_2days = ma15_3 <= ma65_3 and ma15_2d > ma65_2d

        # NEAR A CROSS: MA15 still below MA65 but closing in from below
        # (within NEAR_THRESH %), i.e. about to cross up — early heads-up.
        NEAR_THRESH = 0.5  # percent
        near_cross = False
        if not (crossed_today or crossed_yesterday or crossed_2days):
            below_gap = (ma65 - ma15) / ma65 * 100  # positive when MA15 below MA65
            # must be below but close, AND narrowing (MA15 rising toward MA65)
            narrowing = (ma15 - ma15_prev) > (ma65 - ma65_prev)
            if 0 < below_gap <= NEAR_THRESH and narrowing:
                near_cross = True

        if not (crossed_today or crossed_yesterday or crossed_2days or near_cross): continue

        if crossed_today:       freshness = "TODAY"
        elif crossed_yesterday: freshness = "1 DAY AGO"
        elif crossed_2days:     freshness = "2 DAYS AGO"
        else:                   freshness = "NEAR CROSS"

        gap_pct = (ma15 - ma65) / ma65 * 100

        # RSI reading + volume surge (new)
        rsi = calc_rsi(closes, 14)
        vsurge = volume_surge(bars, 20)

        # Get news
        news = get_news(ticker)
        has_kw = any(match_keywords(h) for h in news)
        news_sentiment = classify_news_sentiment(news)

        # watchlist age
        _added = _wl_dates.get(ticker)
        _days  = days_on_watchlist(_added) if _added else None

        results.append({
            'ticker':    ticker,
            'price':     round(price, 2),
            'ma15':      round(ma15, 2),
            'ma65':      round(ma65, 2),
            'gap_pct':   round(gap_pct, 2),
            'avg_vol':   avg_vol,
            'rsi':       rsi,
            'vsurge':    vsurge,
            'freshness': freshness,
            'news':      news,
            'keyword_hit': has_kw,
            'news_sentiment': news_sentiment,
            'added_date': _added,
            'days_on_watchlist': _days,
        })

    order = {'TODAY': 0, '1 DAY AGO': 1, '2 DAYS AGO': 2, 'NEAR CROSS': 3}
    results.sort(key=lambda x: (order[x['freshness']], x['gap_pct']))

    if not results:
        print("  No stocks matched criteria today.")
        print("  Try again tomorrow or expand the watchlist.")
    else:
        print(f"  MA CROSS RESULTS — stocks that crossed MA15 x MA65")
        print(f"  {'-'*62}")
        print(f"  {'TICKER':<7} {'PRICE':>7} {'MA15':>8} {'MA65':>8} {'GAP%':>6} {'AVG VOL':>8} {'CROSS':>10}")
        print(f"  {'-'*62}")

        for r in results:
            vol_str = f"{r['avg_vol']/1e6:.1f}M"
            star   = " ⭐" if r.get('keyword_hit') else ""
            print(
                f"  {r['ticker']:<7} ${r['price']:>6.2f} "
                f"${r['ma15']:>7.2f} ${r['ma65']:>7.2f} "
                f"{r['gap_pct']:>+5.1f}% {vol_str:>8} {r['freshness']:>10}{star}"
            )
            # Date added / days on watchlist
            _added = r.get('added_date')
            if _added:
                _days = r.get('days_on_watchlist') or 0
                _dtxt = "today" if _days == 0 else (f"{_days} day{'s' if _days != 1 else ''} ago")
                print(f"           📌 on watchlist since {_added} ({_dtxt})")
            # RSI + volume surge line
            _rsi = r.get('rsi')
            _vs  = r.get('vsurge')
            if _rsi is not None or _vs is not None:
                parts = []
                if _rsi is not None:
                    lbl = rsi_label(_rsi)
                    parts.append(f"RSI {_rsi:.0f} ({lbl})")
                if _vs is not None:
                    if _vs >= 2.0:
                        parts.append(f"🔊 VOL {_vs:.1f}x avg (surge!)")
                    elif _vs >= 1.5:
                        parts.append(f"🔊 VOL {_vs:.1f}x avg")
                    else:
                        parts.append(f"VOL {_vs:.1f}x avg")
                print(f"           📊 {'  |  '.join(parts)}")
            # Print news headlines indented below
            if r['news']:
                for headline in r['news']:
                    hits = match_keywords(headline)
                    if hits:
                        tags = ', '.join(sorted(set(hits)))
                        print(f"           🔥 {headline}")
                        print(f"              └─ matched: {tags}")
                    else:
                        print(f"           📰 {headline}")
            else:
                print(f"           📰 No recent news")
            print()

        print(f"  {len(results)} stock(s) found")
        print(f"  Check options chain manually before trading")

    # ── SECTION 2: STANDALONE NEWS HITS (all tickers, cross or not) ───
    news_hits = []
    if KEYWORDS:
        crossed_tickers = {r['ticker'] for r in results}
        print(f"\n{'='*70}")
        print(f"  NEWS HITS — keyword matches across ALL watchlist tickers")
        print(f"  (includes tickers already shown above; tagged [CROSS] if so)")
        print(f"  Keywords: {', '.join(KEYWORDS)}")
        print(f"{'='*70}\n")
        print(f"  Scanning news for {len(WATCHLIST)} stocks...\n")

        for ticker in WATCHLIST:
            headlines = get_news(ticker)
            matched = [(h, match_keywords(h)) for h in headlines]
            matched = [(h, hits) for h, hits in matched if hits]
            if matched:
                news_hits.append((ticker, matched))

        if not news_hits:
            print("  No keyword matches in recent news.")
        else:
            for ticker, matched in news_hits:
                all_tags = sorted({kw for _, hits in matched for kw in hits})
                cat = "[CROSS + NEWS]" if ticker in crossed_tickers else "[NEWS ONLY]"
                print(f"  {ticker:<7} ⭐  {cat}  [{', '.join(all_tags)}]")
                for headline, hits in matched:
                    print(f"           🔥 {headline}")
                print()
            print(f"  {len(news_hits)} stock(s) with news matches")

    # Write the machine-readable snapshot for the website
    write_results_json(results, news_hits)

    print(f"\n{'='*70}")
    _pause()

if __name__ == '__main__':
    scan()
