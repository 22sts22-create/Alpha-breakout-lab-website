"""
PDUFA Pro — Catalyst Alert Job
==============================
The paid tier's headline feature: tell paying subscribers when an FDA decision
date is coming up, with the Pro financials attached, so they never miss a catalyst.

HOW IT FITS WHAT YOU ALREADY HAVE
  • PDUFA dates           → parsed from pdufa_calendar.html (the free calendar's data)
  • Pro financials        → read from pdufa_pro_data.json (pdufa_pro_enrich.py output)
  • Email delivery        → MailerLite DRAFT (same as the free bot — never auto-sends)
  • Daily schedule        → GitHub Actions cron (pdufa_pro_alerts.yml)
  • No duplicate spam     → state committed to sent_alerts.json

SAFE BY DESIGN
  • Creates a DRAFT in MailerLite and stops — you review and send it yourself.
  • Targets the PAID "PDUFA Pro" group ONLY, never the free list.
  • If MailerLite isn't configured yet (no API key / group id), it writes a preview
    HTML file instead of failing — so you can build and see it before Stripe exists.
  • One bad event logs a warning and is skipped; it never crashes the run.
  • Same "educational only, not financial advice" disclaimer as the rest of the site.

MODES (ALERT_MODE env)
  • "digest"  (default) — once a week (Monday), one email listing every catalyst in
                           the next DIGEST_DAYS days, sorted by date, each with Pro data.
  • "windows"           — daily; emails when any catalyst is exactly N days out
                           (ALERT_WINDOWS, default 7 and 1). Group-wide, no accounts needed.

Needs: requests  (pip install requests)
"""

import os
import re
import json
import time
import logging
from datetime import datetime, timezone

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pdufa_pro_alerts")

_here = os.path.dirname(os.path.abspath(__file__))

# ── Paths ─────────────────────────────────────────────────────────────────────
CALENDAR   = os.environ.get("CALENDAR_PATH", os.path.join(_here, "pdufa_calendar.html"))
PRO_DATA   = os.environ.get("PRO_DATA_PATH", os.path.join(_here, "pdufa_pro_data.json"))
STATE_PATH = os.environ.get("ALERT_STATE_PATH", os.path.join(_here, "sent_alerts.json"))
PREVIEW    = os.environ.get("ALERT_PREVIEW_PATH", os.path.join(_here, "pdufa_pro_digest_preview.html"))

# ── Config (safe to tweak / set as GitHub Secrets or workflow env) ────────────
ALERT_MODE    = os.environ.get("ALERT_MODE", "digest")          # "digest" | "windows"
DIGEST_DAYS   = int(os.environ.get("DIGEST_DAYS", "7"))          # digest look-ahead window
DIGEST_WEEKDAY= int(os.environ.get("DIGEST_WEEKDAY", "0"))       # 0 = Monday
ALERT_WINDOWS = [int(x) for x in os.environ.get("ALERT_WINDOWS", "7,1").split(",") if x.strip()]
FORCE_RUN     = os.environ.get("FORCE_RUN", "") == "1"           # build digest even if not the weekday
DRY_RUN       = os.environ.get("DRY_RUN", "") == "1"             # force preview file instead of a draft

# ── MailerLite (PAID group — separate from the free alerts list) ──────────────
MAILERLITE_API_KEY  = os.environ.get("MAILERLITE_API_KEY", "")
# IMPORTANT: this is the PAID "PDUFA Pro" group, NOT the free list. Leave the
# placeholder until you create the group, then drop its id in (or set the secret).
MAILERLITE_PRO_GROUP_ID = os.environ.get("MAILERLITE_PRO_GROUP_ID", "REPLACE_WITH_PRO_GROUP_ID")
MAILERLITE_FROM      = os.environ.get("MAILERLITE_FROM", "alerts@alphabreakoutlab.com")
MAILERLITE_FROM_NAME = os.environ.get("MAILERLITE_FROM_NAME", "Alpha Breakout Lab")

# ── Optional Pushover ping (reuses the free bot's keys if present) ────────────
PUSHOVER_USER_KEY  = os.environ.get("PUSHOVER_USER_KEY", "")
PUSHOVER_API_TOKEN = os.environ.get("PUSHOVER_API_TOKEN", "")

DISCLAIMER = ("Educational and informational only — not financial advice. PDUFA dates are "
              "sourced from public SEC filings and company press releases and can change "
              "without notice. The FDA may approve, reject (CRL), or extend on, before, or "
              "after any date. Always do your own research.")


# ── Date helpers ──────────────────────────────────────────────────────────────
def days_until(iso):
    try:
        target = datetime.strptime(iso, "%Y-%m-%d").date()
        return (target - datetime.now().date()).days
    except Exception:
        return None

def is_vague(date_str):
    d = (date_str or "").strip()
    return bool(re.search(r"TBD|pivotal", d, re.I) or re.match(r"^Q[1-4]\s*20\d\d$", d, re.I))

def fmt_date(iso):
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%b %d, %Y")
    except Exception:
        return iso


# ── Read events from the calendar (the free tier's own data) ──────────────────
def load_events():
    try:
        html = open(CALENDAR, encoding="utf-8").read()
    except FileNotFoundError:
        log.error(f"calendar not found at {CALENDAR}")
        return []
    m = re.search(r"const PDUFA_DATA = \[(.*?)\];", html, re.S)
    arr = m.group(1) if m else html

    def grab(key):
        return re.findall(rf'(?<![A-Za-z]){key}:\s*"([^"]*)"', arr)

    tickers   = grab("ticker")
    companies = grab("company")
    drugs     = grab("drug")
    dates     = grab("date")
    isodates  = grab("isoDate")
    types     = grab("type")

    n = len(tickers)
    if not (len(companies) == len(drugs) == len(dates) == len(isodates) == len(types) == n):
        log.warning("calendar field counts don't line up; parsing what aligns")
        n = min(len(tickers), len(companies), len(drugs), len(dates), len(isodates), len(types))

    events = []
    for i in range(n):
        events.append({
            "ticker": tickers[i], "company": companies[i], "drug": drugs[i],
            "date": dates[i], "isoDate": isodates[i], "type": types[i],
            "days": days_until(isodates[i]),
        })
    log.info(f"parsed {len(events)} events from calendar")
    return events


def load_pro_data():
    try:
        doc = json.load(open(PRO_DATA))
    except Exception as e:
        log.warning(f"no pro data loaded ({e}) — alerts will show 'Pro data pending'")
        return {}
    rows = doc.get("events", doc if isinstance(doc, list) else [])
    return {r.get("ticker"): r for r in rows if r.get("ticker")}


# ── State (so reruns never re-send the same alert) ────────────────────────────
def load_state():
    try:
        return set(json.load(open(STATE_PATH)).get("sent", []))
    except Exception:
        return set()

def save_state(sent):
    try:
        json.dump({"sent": sorted(sent),
                   "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                  open(STATE_PATH, "w"), indent=2)
    except Exception as e:
        log.warning(f"could not write state: {e}")


# ── Build the email ───────────────────────────────────────────────────────────
def pro_bits(ticker, pro):
    """Return (runway, dilution, insider) display strings for an event, gracefully."""
    r = pro.get(ticker)
    if not r:
        return ("Pro data pending", "Pro data pending", "Pro data pending")
    runway   = r.get("runway_label") or "not available"
    dilution = r.get("dilution_note") or "not available"
    insider  = r.get("insider_activity") or "not available"
    return (runway, dilution, insider)


def build_email(events, pro, title, lead):
    rows = ""
    for e in events:
        runway, dilution, insider = pro_bits(e["ticker"], pro)
        dleft = e["days"]
        dleft_txt = "today" if dleft == 0 else (f"in {dleft} day" + ("" if dleft == 1 else "s"))
        rows += (
            "<tr>"
            f"<td style='padding:12px;border-bottom:1px solid #E2E8F0;'>"
            f"<strong style='color:#0891B2;font-size:16px;'>{e['ticker']}</strong><br>"
            f"<span style='color:#64748B;font-size:12px;'>{e['company']}</span></td>"
            f"<td style='padding:12px;border-bottom:1px solid #E2E8F0;color:#334155;'>{e['drug']}</td>"
            f"<td style='padding:12px;border-bottom:1px solid #E2E8F0;white-space:nowrap;color:#1E293B;'>"
            f"{e['date']}<br><span style='color:#D97706;font-size:12px;'>{dleft_txt}</span></td>"
            f"<td style='padding:12px;border-bottom:1px solid #E2E8F0;color:#334155;font-size:13px;line-height:1.7;'>"
            f"⏳ {runway}<br>📉 {dilution}<br>👀 {insider}</td>"
            "</tr>"
        )
    html = (
        f"<div style='font-family:Arial,sans-serif;max-width:640px;margin:0 auto;'>"
        f"<h2 style='color:#7C3AED;margin-bottom:4px;'>{title}</h2>"
        f"<p style='color:#475569;font-size:14px;line-height:1.6;'>{lead}</p>"
        f"<table style='width:100%;border-collapse:collapse;margin:16px 0;'>"
        f"<tr style='text-align:left;'>"
        f"<th style='padding:12px;font-size:11px;letter-spacing:1px;color:#64748B;'>TICKER</th>"
        f"<th style='padding:12px;font-size:11px;letter-spacing:1px;color:#64748B;'>DRUG</th>"
        f"<th style='padding:12px;font-size:11px;letter-spacing:1px;color:#64748B;'>PDUFA DATE</th>"
        f"<th style='padding:12px;font-size:11px;letter-spacing:1px;color:#64748B;'>PRO SNAPSHOT</th>"
        f"</tr>{rows}</table>"
        f"<p style='font-size:12px;color:#64748B;line-height:1.6;'>{DISCLAIMER}</p>"
        f"<p style='font-size:12px;color:#64748B;'><a href='{{$unsubscribe}}'>Unsubscribe</a> · "
        f"<a href='https://alphabreakoutlab.com/pdufa_calendar.html'>View the full calendar</a></p>"
        f"</div>"
    )
    return html


# ── Delivery ──────────────────────────────────────────────────────────────────
def mailerlite_draft(subject, html):
    """Create a DRAFT campaign to the PAID Pro group. Never sends. Returns (ok, msg)."""
    if not MAILERLITE_API_KEY or MAILERLITE_PRO_GROUP_ID == "REPLACE_WITH_PRO_GROUP_ID":
        return (False, "MailerLite Pro group not configured")
    base = "https://connect.mailerlite.com/api"
    headers = {"Authorization": f"Bearer {MAILERLITE_API_KEY}",
               "Content-Type": "application/json", "Accept": "application/json"}
    body = {
        # Campaign name must be plain ASCII — MailerLite 500s on emoji here.
        "name": "PDUFA Pro Alert " + datetime.now().strftime("%b %d %Y"),
        "type": "regular",
        "emails": [{"subject": subject, "from_name": MAILERLITE_FROM_NAME,
                    "from": MAILERLITE_FROM, "content": html}],
        "groups": [str(MAILERLITE_PRO_GROUP_ID)],
    }
    try:
        r = requests.post(f"{base}/campaigns", headers=headers, json=body, timeout=20)
        if r.status_code not in (200, 201):
            log.error(f"MailerLite draft failed: {r.status_code} — {r.text[:300]}")
            return (False, f"create failed {r.status_code}")
        cid = r.json().get("data", {}).get("id")
        log.info(f"MailerLite Pro DRAFT created (id={cid}). Review & send in the dashboard.")
        return (True, f"draft {cid}")
    except Exception as e:
        log.error(f"MailerLite error: {e}")
        return (False, str(e))


def deliver(subject, html):
    """Draft into MailerLite, or write a preview file if not configured / DRY_RUN."""
    if DRY_RUN or not MAILERLITE_API_KEY or MAILERLITE_PRO_GROUP_ID == "REPLACE_WITH_PRO_GROUP_ID":
        try:
            open(PREVIEW, "w", encoding="utf-8").write(
                f"<!doctype html><meta charset='utf-8'>"
                f"<title>{subject}</title><body style='background:#080B14;padding:24px;'>{html}</body>")
            log.info(f"preview written to {PREVIEW} (subject: {subject})")
        except Exception as e:
            log.warning(f"could not write preview: {e}")
        return (True, "preview")
    return mailerlite_draft(subject, html)


def pushover(msg):
    if not (PUSHOVER_USER_KEY and PUSHOVER_API_TOKEN):
        return
    try:
        requests.post("https://api.pushover.net/1/messages.json",
                      data={"token": PUSHOVER_API_TOKEN, "user": PUSHOVER_USER_KEY,
                            "title": "PDUFA Pro alert", "message": msg}, timeout=10)
    except Exception as e:
        log.warning(f"pushover failed: {e}")


# ── The two modes ───────────────────────────────────────────────────────────--
def run_digest(events, pro, sent):
    today = datetime.now()
    if today.weekday() != DIGEST_WEEKDAY and not FORCE_RUN:
        log.info(f"not digest day (weekday={today.weekday()}, want {DIGEST_WEEKDAY}); set FORCE_RUN=1 to override")
        return sent, None

    key = f"digest-{today.isocalendar().year}-W{today.isocalendar().week:02d}"
    if key in sent and not FORCE_RUN:
        log.info(f"digest for {key} already drafted; skipping")
        return sent, None

    upcoming = sorted(
        [e for e in events if not is_vague(e["date"]) and e["days"] is not None and 0 <= e["days"] <= DIGEST_DAYS],
        key=lambda e: e["days"])
    if not upcoming:
        log.info(f"no catalysts in the next {DIGEST_DAYS} days — no digest this week")
        return sent, None

    n = len(upcoming)
    subject = f"{n} FDA catalyst{'s' if n != 1 else ''} in the next {DIGEST_DAYS} days — {today.strftime('%b %d')}"
    lead = (f"{n} tracked FDA decision{'s are' if n != 1 else ' is'} coming up in the next "
            f"{DIGEST_DAYS} days. Here's the countdown with the Pro financials on each name.")
    html = build_email(upcoming, pro, "This Week's FDA Catalysts", lead)
    ok, msg = deliver(subject, html)
    if ok:
        sent.add(key)
        pushover(f"{subject} ({msg})")
    return sent, (subject, [e["ticker"] for e in upcoming], msg)


def run_windows(events, pro, sent):
    today = datetime.now()
    triggered = [e for e in events
                 if not is_vague(e["date"]) and e["days"] in ALERT_WINDOWS]
    fresh = [e for e in triggered if f"{e['ticker']}-{e['isoDate']}-{e['days']}d" not in sent]
    if not fresh:
        log.info("no new catalysts hit an alert window today")
        return sent, None

    fresh.sort(key=lambda e: e["days"])
    n = len(fresh)
    subject = f"{n} FDA catalyst{'s' if n != 1 else ''} approaching — {today.strftime('%b %d')}"
    lead = "A tracked FDA decision date is almost here. Countdown and Pro financials below."
    html = build_email(fresh, pro, "Catalyst Approaching", lead)
    ok, msg = deliver(subject, html)
    if ok:
        for e in fresh:
            sent.add(f"{e['ticker']}-{e['isoDate']}-{e['days']}d")
        pushover(f"{subject} ({msg})")
    return sent, (subject, [e["ticker"] for e in fresh], msg)


def main():
    log.info("=" * 60)
    log.info(f"PDUFA Pro Alert Job — {datetime.now().strftime('%Y-%m-%d %H:%M')} — mode={ALERT_MODE}")
    log.info("=" * 60)

    events = load_events()
    if not events:
        log.error("no events — aborting")
        return
    pro = load_pro_data()
    sent = load_state()

    if ALERT_MODE == "windows":
        sent, result = run_windows(events, pro, sent)
    else:
        sent, result = run_digest(events, pro, sent)

    save_state(sent)
    if result:
        subject, tickers, msg = result
        log.info(f"ALERT: {subject} → {', '.join(tickers)} [{msg}]")
    log.info("done.")


if __name__ == "__main__":
    main()
