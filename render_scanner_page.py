"""
render_scanner_page.py
======================
Reads scanner_results.json (written by watchlist_scanner.py) and injects into
scanner.html:
  • the signals, between <!-- RESULTS:START --> / <!-- RESULTS:END -->
  • the tracked universe, between <!-- UNIVERSE:START --> / <!-- UNIVERSE:END -->
  • the "Updated ..." date stamp, between <!-- ASOF:START --> / <!-- ASOF:END -->

PUBLIC PAGE = SIGNALS ONLY. No prices, no dollar MA values, no raw volumes, no
news headline text. Derived readings only: cross freshness, MA gap %, RSI zone,
volume-surge multiple, days-on-watchlist, and a positive/negative/mixed news tag.

Paths (override with env vars if needed):
  SCANNER_INPUT  → scanner_results.json   (default: next to this file)
  PAGE_PATH      → scanner.html           (default: next to this file)
"""

import os
import re
import json
from datetime import datetime

_here = os.path.dirname(os.path.abspath(__file__))
INPUT_PATH = os.environ.get('SCANNER_INPUT', os.path.join(_here, 'scanner_results.json'))
PAGE_PATH  = os.environ.get('PAGE_PATH',     os.path.join(_here, 'scanner.html'))

# Freshness → (CSS suffix, badge label, section heading, section note)
FRESH = {
    "TODAY":      ("today", "Today",      "Crossed Today",       "A fresh moving-average crossover on the latest close."),
    "1 DAY AGO":  ("d1",    "1 Day",      "Crossed 1 Day Ago",   "Crossed on the prior session — still recent."),
    "2 DAYS AGO": ("d2",    "2 Days",     "Crossed 2 Days Ago",  "Crossed two sessions back."),
    "NEAR CROSS": ("near",  "Near Cross", "Near Cross",          "Fast average still below the slow one, but narrowing from below — about to cross."),
}
ORDER = ["TODAY", "1 DAY AGO", "2 DAYS AGO", "NEAR CROSS"]

RSI_CLASS = {
    "strong": "rsi-strong", "overbought": "rsi-overbought",
    "oversold": "rsi-oversold", "weak": "rsi-weak", "neutral": "rsi-weak",
}

NEWS_CHIP = {
    "positive": ('news-pos',   '📈 positive news'),
    "negative": ('news-neg',   '📉 negative news'),
    "mixed":    ('news-mixed', '📰 mixed news'),
}


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def card_html(r):
    fresh  = r.get("freshness", "")
    suffix = FRESH.get(fresh, ("", ""))[0]
    label  = FRESH.get(fresh, ("", ""))[1]

    chips = []

    # MA gap — only meaningful (positive) once crossed; skip for near-cross.
    # Shown as a plain % so the MA periods stay private.
    gap = r.get("gap_pct")
    if gap is not None and gap > 0:
        chips.append(f'<span class="chip gap">MA gap +{gap:.1f}%</span>')

    # RSI zone — label only (strong / weak / neutral / overbought / oversold)
    rlabel = r.get("rsi_label") or ""
    if rlabel:
        cls = RSI_CLASS.get(rlabel, "rsi-weak")
        chips.append(f'<span class="chip {cls}">RSI {esc(rlabel)}</span>')

    # Volume surge multiple
    vs = r.get("vsurge")
    if vs is not None:
        tail = " (surge)" if vs >= 2.0 else ""
        chips.append(f'<span class="chip vol">Vol {vs:.1f}× avg{tail}</span>')

    # News sentiment (positive / negative / mixed) — no headline text
    sent = r.get("news_sentiment")
    if sent in NEWS_CHIP:
        cls, text = NEWS_CHIP[sent]
        chips.append(f'<span class="chip {cls}">{text}</span>')

    # Days on watchlist
    days = r.get("days_on_watchlist")
    if days is not None:
        if days == 0:
            chips.append('<span class="chip age">📌 added today</span>')
        else:
            chips.append(f'<span class="chip age">📌 {days}d on watch</span>')

    chips_html = "\n        ".join(chips) if chips else ""
    return (
        f'    <div class="scard {suffix}">\n'
        f'      <div class="scard-top"><span class="scard-ticker">{esc(r.get("ticker",""))}</span>'
        f'<span class="badge {suffix}">{label}</span></div>\n'
        f'      <div class="chips">\n        {chips_html}\n      </div>\n'
        f'    </div>'
    )


def build_results_html(data):
    results = data.get("results", [])
    total = data.get("universe_size")
    if not results:
        return (
            '  <div class="empty">None of the tracked stocks triggered a signal today. '
            'Markets were quiet for this setup — check back after the next close. '
            'The full watchlist is further down.</div>'
        )

    of_txt = f" of ~{total}" if total else ""
    intro = (
        f'  <p class="results-intro"><strong>{len(results)}</strong>{of_txt} tracked '
        f'stocks triggered a signal today. Only triggered stocks show up here — '
        f'the full watchlist is in &ldquo;What we&rsquo;re tracking&rdquo; below.</p>'
    )

    groups = {k: [] for k in ORDER}
    for r in results:
        f = r.get("freshness")
        if f in groups:
            groups[f].append(r)

    blocks = []
    for key in ORDER:
        rows = groups[key]
        if not rows:
            continue
        _, _, heading, note = FRESH[key]
        cards = "\n".join(card_html(r) for r in rows)
        blocks.append(
            f'  <h2>{heading}</h2>\n'
            f'  <p class="group-note">{note}</p>\n'
            f'  <div class="grid">\n{cards}\n  </div>'
        )
    return intro + "\n\n" + "\n\n".join(blocks)


def build_universe_html(data):
    groups = data.get("universe_groups") or {}
    if not groups:
        return "    <!-- no universe data -->"
    out = []
    for cat, tickers in groups.items():
        tick_str = " · ".join(esc(t) for t in tickers)
        out.append(
            f'    <div class="uni-group"><span class="uni-cat">{esc(cat)}</span>'
            f'<span class="uni-tickers">{tick_str}</span></div>'
        )
    return "\n".join(out)


def _replace_block(html, start, end, inner):
    return re.sub(
        re.escape(start) + r'.*?' + re.escape(end),
        lambda m: start + "\n" + inner + "\n  " + end,
        html, flags=re.DOTALL,
    )


def main():
    with open(INPUT_PATH, "r") as f:
        data = json.load(f)
    with open(PAGE_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    # Results + universe blocks
    html = _replace_block(html, "<!-- RESULTS:START -->", "<!-- RESULTS:END -->", build_results_html(data))
    html = _replace_block(html, "<!-- UNIVERSE:START -->", "<!-- UNIVERSE:END -->", build_universe_html(data))

    # Date stamp
    gdate = data.get("generated_date", "")
    try:
        asof = datetime.strptime(gdate, "%Y-%m-%d").strftime("%b %d, %Y")
    except Exception:
        asof = gdate or datetime.now().strftime("%b %d, %Y")
    html = re.sub(
        r'(<!-- ASOF:START -->).*?(<!-- ASOF:END -->)',
        lambda m: m.group(1) + asof + " " + m.group(2),
        html, flags=re.DOTALL,
    )

    with open(PAGE_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    n = len(data.get("results", []))
    print(f"  → scanner.html updated: {n} signal(s), as of {asof}")


if __name__ == "__main__":
    main()
