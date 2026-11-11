"""
PDUFA Pro — EDGAR Enrichment
============================
For each ticker on the PDUFA calendar, pull company financials from SEC EDGAR's
free JSON APIs and compute the Pro data layer: cash, quarterly burn, runway,
dilution, revenue, recent filings, and insider (Form 4) activity.

DISCIPLINE (do not weaken):
  - Every number is source-stamped: value + the filing date it came from + the form.
  - Missing or stale data (filing older than ~9 months) is FLAGGED, never guessed.
  - A missing number renders as "not available", never 0 or an estimate.
  - Backend only. EDGAR has no CORS — this must run server-side, never in the browser.
  - One ticker failing logs a warning and continues; it never crashes the run.

Reads tickers from pdufa_calendar.html (the PDUFA_DATA array) by default.
Override with the PRO_TICKERS env var (comma list) for testing, e.g.
  PRO_TICKERS=UNCY,VERA python3 pdufa_pro_enrich.py
Writes pdufa_pro_data.json next to this file (or $PRO_OUTPUT).

Needs: requests  (pip install requests)
"""

import os
import re
import json
import time
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pdufa_pro")

_here = os.path.dirname(os.path.abspath(__file__))

# SEC requires a descriptive User-Agent with contact info or it blocks you.
UA = os.environ.get("SEC_USER_AGENT", "AlphaBreakoutLab PDUFA-Pro contact@alphabreakoutlab.com")
HEADERS = {"User-Agent": UA, "Accept": "application/json"}

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL   = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
SUBS_URL    = "https://data.sec.gov/submissions/CIK{cik10}.json"
ARCH_BASE   = "https://www.sec.gov/Archives/edgar/data"

CIK_CACHE   = os.path.join(_here, "ticker_cik_map.json")
OUTPUT_PATH = os.environ.get("PRO_OUTPUT", os.path.join(_here, "pdufa_pro_data.json"))
CALENDAR    = os.path.join(_here, "pdufa_calendar.html")

STALE_DAYS = 270   # ~9 months; a source older than this is flagged stale
RATE_SLEEP = 0.15  # stay well under SEC's ~10 req/s courtesy limit


# ── HTTP with polite retry/backoff ────────────────────────────────────────────
def get_json(url, tries=4):
    delay = 1.0
    for attempt in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 200:
                time.sleep(RATE_SLEEP)
                return r.json()
            if r.status_code in (429, 503):
                log.warning(f"{r.status_code} on {url} — backing off {delay:.0f}s")
                time.sleep(delay); delay *= 2; continue
            # 404 etc. — not retryable
            log.warning(f"{r.status_code} on {url}")
            return None
        except Exception as e:
            log.warning(f"request error on {url}: {e}")
            time.sleep(delay); delay *= 2
    return None


def get_raw(url, tries=3):
    """Fetch raw text (for Form 4 ownership XML, which is not JSON)."""
    delay = 1.0
    for _ in range(tries):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
            if r.status_code == 200:
                time.sleep(RATE_SLEEP)
                return r.text
            if r.status_code in (429, 503):
                time.sleep(delay); delay *= 2; continue
            return None
        except Exception as e:
            log.warning(f"raw request error on {url}: {e}")
            time.sleep(delay); delay *= 2
    return None


# ── Form 4 transaction-code classification ────────────────────────────────────
# A Form 4 line's transactionCode is what separates a real signal from comp:
#   P = open-market/private PURCHASE  -> the bullish "insider is buying" signal
#   S = open-market/private SALE
#   A = grant / award / other acquisition from the issuer -> COMPENSATION, not a buy
#   M = option exercise, F = shares withheld for tax, G = gift, C/X/J = other/mechanical
# We tally P and S (with dollar value = shares x price) and count A separately so
# RSU grants never masquerade as conviction buying.
BUY_CODES   = {"P"}
SELL_CODES  = {"S"}
GRANT_CODES = {"A"}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _money(v):
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v/1_000:.0f}K"
    return f"${v:.0f}"


def classify_form4(cik_int, accession):
    """Open one Form 4's ownership XML and tally its transactions by code.
    Returns a tally dict, or None if the filing's XML couldn't be read."""
    acc_nodash = accession.replace("-", "")
    base = f"{ARCH_BASE}/{cik_int}/{acc_nodash}"
    idx = get_json(f"{base}/index.json")
    if not idx:
        return None
    xml_name = None
    for it in idx.get("directory", {}).get("item", []):
        n = it.get("name", "")
        # the ownership doc is a .xml that isn't the XSLT-rendered wrapper (xslF345...)
        if n.lower().endswith(".xml") and not n.lower().startswith("xsl"):
            xml_name = n
            break
    if not xml_name:
        return None
    raw = get_raw(f"{base}/{xml_name}")
    if not raw:
        return None
    try:
        root = ET.fromstring(raw)
    except Exception:
        return None
    tally = {"buy_n": 0, "buy_val": 0.0, "sell_n": 0, "sell_val": 0.0,
             "grant_n": 0, "other_n": 0}
    for tag in ("nonDerivativeTransaction", "derivativeTransaction"):
        for tx in root.iter(tag):
            code_el = tx.find("./transactionCoding/transactionCode")
            if code_el is None or not (code_el.text or "").strip():
                continue
            code = code_el.text.strip().upper()
            shares = _f(tx.findtext("./transactionAmounts/transactionShares/value"))
            price = _f(tx.findtext("./transactionAmounts/transactionPricePerShare/value"))
            val = shares * price
            if code in BUY_CODES:
                tally["buy_n"] += 1; tally["buy_val"] += val
            elif code in SELL_CODES:
                tally["sell_n"] += 1; tally["sell_val"] += val
            elif code in GRANT_CODES:
                tally["grant_n"] += 1
            else:
                tally["other_n"] += 1
    return tally


def summarize_insider(agg, total_filings):
    """Human-readable insider line that leads with open-market BUYS (the signal)."""
    if total_filings == 0:
        return "no Form 4 filings in last 90 days"
    parts = []
    if agg["buy_n"]:
        parts.append(f"🟢 {agg['buy_n']} open-market buy(s) ~{_money(agg['buy_val'])}")
    if agg["sell_n"]:
        parts.append(f"🔴 {agg['sell_n']} sell(s) ~{_money(agg['sell_val'])}")
    if agg["grant_n"]:
        parts.append(f"{agg['grant_n']} grant/comp")
    if not parts:
        if agg["parsed"] == 0:
            return f"{total_filings} Form 4(s) in 90d (detail unavailable)"
        return f"{total_filings} Form 4(s) in 90d — no open-market buys (grants/exercises only)"
    return " · ".join(parts) + f"  ({total_filings} Form 4s, 90d)"


# ── ticker -> CIK (cached daily) ──────────────────────────────────────────────
def load_cik_map():
    # Start from the COMMITTED map (ticker_cik_map.json in the repo). This is the
    # reliable fallback: SEC blocks www.sec.gov/files/company_tickers.json from
    # cloud IPs (GitHub Actions) with a 403, so we never depend on the live file.
    base = {}
    try:
        with open(CIK_CACHE) as f:
            base = {k.upper(): int(v) for k, v in json.load(f).items()}
    except Exception:
        pass
    # Best-effort refresh from SEC to pick up any newly-added tickers. If it
    # fails (403 / offline), we keep the committed map and carry on.
    data = get_json(TICKERS_URL)
    if data:
        try:
            live = {row["ticker"].upper(): int(row["cik_str"]) for row in data.values()}
            base.update(live)  # live values win; adds tickers not in the committed map
            with open(CIK_CACHE, "w") as f:
                json.dump(base, f)
            log.info(f"CIK map refreshed from SEC ({len(live)} tickers)")
        except Exception as e:
            log.warning(f"could not merge live CIK map: {e}")
    else:
        log.warning("live SEC ticker file unavailable — using committed ticker_cik_map.json")
    if not base:
        log.error("no CIK map available (live fetch failed AND no committed ticker_cik_map.json)")
    return base


# ── helpers for reading companyfacts ──────────────────────────────────────────
def _days(d1, d2):
    return abs((datetime.strptime(d1, "%Y-%m-%d") - datetime.strptime(d2, "%Y-%m-%d")).days)

def _is_stale(filed):
    try:
        return (datetime.now() - datetime.strptime(filed, "%Y-%m-%d")).days > STALE_DAYS
    except Exception:
        return False

def concept(facts, tag, taxonomies=("us-gaap", "dei")):
    """Return the list of unit entries for the first taxonomy that has `tag`."""
    for tax in taxonomies:
        node = facts.get(tax, {}).get(tag)
        if node and node.get("units"):
            # entries live under a unit key (USD, shares, ...). Take the first unit.
            units = node["units"]
            key = next(iter(units))
            return units[key]
    return None

def first_concept(facts, tags, taxonomies=("us-gaap", "dei")):
    """Try several tag names in order; return (entries, tag_used) for the first hit."""
    for t in tags:
        e = concept(facts, t, taxonomies)
        if e:
            return e, t
    return None, None

def latest_instant(entries):
    """Most recent point-in-time value (cash, shares). Returns dict or None."""
    if not entries:
        return None
    # instant facts have 'end' but no meaningful 'start' span; take max end that has a form
    cand = [e for e in entries if e.get("end") and e.get("filed")]
    if not cand:
        return None
    best = max(cand, key=lambda e: e["end"])
    return {"val": best["val"], "asof": best["end"], "form": best.get("form", ""),
            "filed": best.get("filed", ""), "stale": _is_stale(best.get("filed", ""))}

def latest_quarterly_flow(entries):
    """Most recent ~single-quarter value for a flow metric (OCF, net income, revenue).
    10-Qs report YTD cumulative, so we prefer entries whose period is ~one quarter
    (end-start ≈ 80–100 days) to isolate a real quarter. Falls back to the most
    recent duration entry (annualized /4 if it looks annual), with a note."""
    if not entries:
        return None
    dur = [e for e in entries if e.get("start") and e.get("end") and e.get("filed")]
    if not dur:
        return None
    def span(e): return _days(e["end"], e["start"])
    quarterly = [e for e in dur if 80 <= span(e) <= 100]
    if quarterly:
        e = max(quarterly, key=lambda x: x["end"])
        return {"val": e["val"], "asof": e["end"], "form": e.get("form", ""),
                "filed": e.get("filed", ""), "stale": _is_stale(e.get("filed", "")),
                "basis": "quarter"}
    # no clean quarter — take most recent duration entry
    e = max(dur, key=lambda x: x["end"])
    s = span(e)
    if 330 <= s <= 380:   # looks annual → approximate a quarter
        return {"val": e["val"] / 4.0, "asof": e["end"], "form": e.get("form", ""),
                "filed": e.get("filed", ""), "stale": _is_stale(e.get("filed", "")),
                "basis": "annual/4 (approx)"}
    return {"val": e["val"], "asof": e["end"], "form": e.get("form", ""),
            "filed": e.get("filed", ""), "stale": _is_stale(e.get("filed", "")),
            "basis": f"{s}-day period"}

def shares_now_and_year_ago(entries):
    """Latest shares outstanding and the value closest to ~1 year before it."""
    if not entries:
        return None, None
    cand = [e for e in entries if e.get("end") and e.get("val")]
    if not cand:
        return None, None
    latest = max(cand, key=lambda e: e["end"])
    target_days = 365
    prior_pool = [e for e in cand if e["end"] < latest["end"]]
    prior = None
    if prior_pool:
        prior = min(prior_pool, key=lambda e: abs(_days(e["end"], latest["end"]) - target_days))
        # only accept if it's genuinely ~6-18 months back
        if not (180 <= _days(prior["end"], latest["end"]) <= 550):
            prior = None
    now = {"val": latest["val"], "asof": latest["end"]}
    ago = {"val": prior["val"], "asof": prior["end"]} if prior else None
    return now, ago


# ── formatting ────────────────────────────────────────────────────────────────
def humanize_usd(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return None
    sign = "-" if n < 0 else ""
    n = abs(n)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            v = n / div
            return f"{sign}${v:.1f}{suf}" if v < 10 else f"{sign}${v:.0f}{suf}"
    return f"{sign}${n:.0f}"

def stamp(disp, asof, form, stale):
    s = disp
    bits = []
    if asof: bits.append(f"as of {asof}")
    if form: bits.append(form)
    if bits: s += f" ({', '.join(bits)})"
    if stale: s += " ⚠ stale"
    return s


# ── per-ticker enrichment ─────────────────────────────────────────────────────
def enrich(ticker, cik):
    rec = {"ticker": ticker, "cik": cik}
    cik10 = str(cik).zfill(10)

    facts_doc = get_json(FACTS_URL.format(cik10=cik10))
    facts = (facts_doc or {}).get("facts", {})

    # ---- Cash ----
    cash_entries, cash_tag = first_concept(facts, [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "Cash",
    ])
    cash = latest_instant(cash_entries) if cash_entries else None
    if cash:
        rec.update(cash_value=cash["val"], cash_asof=cash["asof"], cash_form=cash["form"],
                   cash_stale=cash["stale"], cash_missing=False,
                   cash_display=stamp(humanize_usd(cash["val"]), cash["asof"], cash["form"], cash["stale"]))
    else:
        rec.update(cash_value=None, cash_missing=True, cash_display="not available")

    # ---- Operating cash flow / net income → quarterly burn ----
    ocf_entries, _ = first_concept(facts, ["NetCashProvidedByUsedInOperatingActivities"])
    ni_entries, _  = first_concept(facts, ["NetIncomeLoss"])
    ocf = latest_quarterly_flow(ocf_entries) if ocf_entries else None
    ni  = latest_quarterly_flow(ni_entries) if ni_entries else None

    burn = None; burn_src = None
    if ocf and ocf["val"] is not None:
        burn = abs(ocf["val"]); burn_src = f"operating cash flow ({ocf['basis']}, {ocf['asof']})"
    elif ni and ni["val"] is not None:
        burn = abs(ni["val"]); burn_src = f"net loss ({ni['basis']}, {ni['asof']})"
    rec["quarterly_burn"] = burn
    rec["burn_source"] = burn_src
    rec["burn_display"] = (f"{humanize_usd(burn)}/qtr — {burn_src}" if burn else "not available")

    # positive operating cash flow? (OCF > 0 means they're generating, not burning)
    cash_flow_positive = bool(ocf and ocf["val"] is not None and ocf["val"] > 0)

    # ---- Runway ----
    if cash and burn and burn > 0 and not cash_flow_positive:
        q = cash["val"] / burn
        # approximate the calendar quarter the cash runs into
        future = datetime.now()
        add_days = int(q * 91)
        fy = future.year + (future.month - 1 + add_days // 30) // 12
        # simpler: project forward by add_days
        proj = datetime.fromtimestamp(future.timestamp() + add_days * 86400)
        qtr = (proj.month - 1) // 3 + 1
        rec.update(runway_quarters=round(q, 1),
                   runway_label=f"~{q:.1f} quarters — cash into Q{qtr} {proj.year}")
    elif cash_flow_positive:
        rec.update(runway_quarters=None, runway_label="positive operating cash flow — n/a")
    else:
        rec.update(runway_quarters=None, runway_label="not available")

    # ---- Shares outstanding / dilution ----
    sh_entries, _ = first_concept(facts, [
        "CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding"
    ], taxonomies=("dei", "us-gaap"))
    now_sh, ago_sh = shares_now_and_year_ago(sh_entries) if sh_entries else (None, None)
    if now_sh:
        rec["shares_now"] = now_sh["val"]; rec["shares_now_asof"] = now_sh["asof"]
    if now_sh and ago_sh and ago_sh["val"]:
        dil = (now_sh["val"] - ago_sh["val"]) / ago_sh["val"] * 100.0
        if dil > 400 or dil < -80:
            # implausible — stock split / reverse merger / SPAC, not real dilution
            rec.update(shares_year_ago=None, dilution_pct=None,
                       dilution_note="n/a (corporate action)")
        else:
            rec.update(shares_year_ago=ago_sh["val"], dilution_pct=round(dil, 1),
                       dilution_note=(f"+{dil:.1f}% shares vs {ago_sh['asof']}" if dil >= 0
                                      else f"{dil:.1f}% shares vs {ago_sh['asof']}"))
    else:
        rec.update(shares_year_ago=None, dilution_pct=None,
                   dilution_note="insufficient history")

    # ---- Revenue ----
    rev_entries, _ = first_concept(facts, [
        "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"
    ])
    rev = latest_quarterly_flow(rev_entries) if rev_entries else None
    if rev and rev["val"] is not None:
        rec.update(revenue_value=rev["val"], revenue_asof=rev["asof"],
                   revenue_display=stamp(humanize_usd(rev["val"]), rev["asof"], rev["form"], rev["stale"]))
    else:
        rec.update(revenue_value=None, revenue_display="not available")

    # ---- Total debt (optional) ----
    debt_entries, _ = first_concept(facts, [
        "LongTermDebt", "LongTermDebtNoncurrent", "DebtLongtermAndShorttermCombinedAmount"
    ])
    debt = latest_instant(debt_entries) if debt_entries else None
    if debt:
        rec.update(debt_value=debt["val"], debt_asof=debt["asof"],
                   debt_display=stamp(humanize_usd(debt["val"]), debt["asof"], debt["form"], debt["stale"]))
    else:
        rec.update(debt_value=None, debt_display="not available")

    # ---- Recent filings + insider Form 4 (from submissions feed) ----
    rec["recent_filings"] = []
    rec["insider_filings_90d"] = []
    subs = get_json(SUBS_URL.format(cik10=cik10))
    if subs:
        recent = subs.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accns = recent.get("accessionNumber", [])
        docs  = recent.get("primaryDocument", [])
        cik_int = str(int(cik))
        wanted = {"8-K", "10-Q", "10-K"}
        for i in range(len(forms)):
            f = forms[i]
            if f in wanted and len(rec["recent_filings"]) < 5:
                acc_nodash = accns[i].replace("-", "")
                url = f"{ARCH_BASE}/{cik_int}/{acc_nodash}/{docs[i]}" if docs[i] else \
                      f"{ARCH_BASE}/{cik_int}/{acc_nodash}/"
                rec["recent_filings"].append(
                    {"form": f, "filingDate": dates[i], "accession": accns[i], "url": url})
            if f == "4":
                try:
                    age = (datetime.now() - datetime.strptime(dates[i], "%Y-%m-%d")).days
                except Exception:
                    age = 999
                if age <= 90:
                    acc_nodash = accns[i].replace("-", "")
                    rec["insider_filings_90d"].append(
                        {"filingDate": dates[i], "accession": accns[i],
                         "url": f"{ARCH_BASE}/{cik_int}/{acc_nodash}/"})
        # Open each Form 4 (cap at 12 most-recent to bound requests) and classify
        # its lines by transaction code, so buys are never confused with comp.
        agg = {"buy_n": 0, "buy_val": 0.0, "sell_n": 0, "sell_val": 0.0,
               "grant_n": 0, "other_n": 0, "parsed": 0}
        for filing in rec["insider_filings_90d"][:12]:
            t = classify_form4(cik_int, filing["accession"])
            if t:
                agg["parsed"] += 1
                for k in ("buy_n", "buy_val", "sell_n", "sell_val", "grant_n", "other_n"):
                    agg[k] += t[k]
        rec["insider_detail"] = agg
        rec["insider_activity"] = summarize_insider(agg, len(rec["insider_filings_90d"]))
    else:
        rec["insider_activity"] = "not available"

    return rec


# ── ticker source ─────────────────────────────────────────────────────────────
def tickers_from_calendar():
    try:
        html = open(CALENDAR, encoding="utf-8").read()
    except FileNotFoundError:
        return []
    found = re.findall(r'ticker:\s*"([A-Z]{1,6})"', html)
    seen = []
    for t in found:
        if t not in seen:
            seen.append(t)
    return seen


def main():
    env = os.environ.get("PRO_TICKERS", "").strip()
    if env:
        tickers = [t.strip().upper() for t in env.split(",") if t.strip()]
    else:
        tickers = tickers_from_calendar()
    if not tickers:
        log.error("no tickers found (set PRO_TICKERS or place pdufa_calendar.html next to this script)")
        return

    log.info(f"enriching {len(tickers)} ticker(s): {', '.join(tickers)}")
    cik_map = load_cik_map()

    records = []
    for t in tickers:
        cik = cik_map.get(t)
        if not cik:
            log.warning(f"{t}: no CIK found, skipping")
            records.append({"ticker": t, "cik": None, "error": "no CIK found"})
            continue
        try:
            rec = enrich(t, cik)
            records.append(rec)
            log.info(f"{t}: cash={rec.get('cash_display')} | burn={rec.get('burn_display')} "
                     f"| runway={rec.get('runway_label')} | dilution={rec.get('dilution_note')}")
        except Exception as e:
            log.warning(f"{t}: enrichment failed ({e}) — continuing")
            records.append({"ticker": t, "cik": cik, "error": str(e)})

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_date": datetime.now().strftime("%Y-%m-%d"),
        "source": "SEC EDGAR (companyfacts + submissions)",
        "count": len(records),
        "events": records,
    }
    with open(OUTPUT_PATH, "w") as f:
        json.dump(payload, f, indent=2)
    log.info(f"wrote {len(records)} enriched record(s) to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
