#!/usr/bin/env python3
"""
PDUFA Pro — Stripe → MailerLite sync.

Polls Stripe for everyone CURRENTLY entitled to Pro (active / trialing / past_due
subscriptions) and reconciles the MailerLite "PDUFA Pro" group to match:
  • adds new payers to the Pro group
  • removes people who cancelled (so a lapsed member stops getting paid alerts)

No webhook / always-on server needed — runs on cron every ~15 min. A ~15-minute
delay before a new payer gets their first alert is invisible in practice.

Config (env, normally sourced from /opt/pdufa-pro/config.env):
  STRIPE_API_KEY            restricted, READ-only key (Subscriptions + Customers)
  MAILERLITE_API_KEY        same key the alert job already uses
  MAILERLITE_PRO_GROUP_ID   the PDUFA Pro group id (NUMBER, not a URL)

Optional:
  SYNC_DRY_RUN=1            log what it WOULD do, change nothing
  SYNC_REMOVE_CHURNED=0     only add; never remove (default: remove is ON)

Safety: if the Stripe fetch fails, it aborts WITHOUT touching MailerLite, so a
transient API error can never wipe the group. Removals are also skipped when the
active set is empty (the zero-subscriber state) to avoid nuking a group that only
holds manual/test entries.
"""
import os
import sys
import logging
import requests

log = logging.getLogger("pdufa_sync")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

STRIPE_KEY = os.environ.get("STRIPE_API_KEY", "").strip()
ML_KEY     = os.environ.get("MAILERLITE_API_KEY", "").strip()
ML_GROUP   = os.environ.get("MAILERLITE_PRO_GROUP_ID", "").strip()

def _flag(name, default):
    v = os.environ.get(name, default).strip().lower()
    return v not in ("0", "false", "no", "")

DRY_RUN         = _flag("SYNC_DRY_RUN", "0")
REMOVE_CHURNED  = _flag("SYNC_REMOVE_CHURNED", "1")

ENTITLED     = {"active", "trialing", "past_due"}   # statuses that keep Pro access
STRIPE_BASE  = "https://api.stripe.com/v1"
ML_BASE      = "https://connect.mailerlite.com/api"


def die(msg, code=1):
    log.error(msg)
    sys.exit(code)


# ── Stripe ────────────────────────────────────────────────────────────────────
def active_subscriber_emails():
    """Set of lowercased emails currently entitled to Pro (active/trialing/past_due)."""
    emails = set()
    starting_after = None
    while True:
        params = {"status": "all", "limit": 100, "expand[]": "data.customer"}
        if starting_after:
            params["starting_after"] = starting_after
        r = requests.get(f"{STRIPE_BASE}/subscriptions", params=params,
                         auth=(STRIPE_KEY, ""), timeout=30)
        if r.status_code == 401:
            die("Stripe 401 — STRIPE_API_KEY missing/invalid or lacks read access "
                "to Subscriptions + Customers.")
        r.raise_for_status()
        data = r.json()
        items = data.get("data", [])
        for sub in items:
            if sub.get("status") not in ENTITLED:
                continue
            cust = sub.get("customer")
            email = (cust.get("email") if isinstance(cust, dict) else None) or ""
            email = email.strip().lower()
            if email:
                emails.add(email)
        if data.get("has_more") and items:
            starting_after = items[-1]["id"]
        else:
            break
    return emails


# ── MailerLite ────────────────────────────────────────────────────────────────
def ml_headers():
    return {"Authorization": f"Bearer {ML_KEY}",
            "Content-Type": "application/json", "Accept": "application/json"}

def ml_get_or_create(email):
    """Upsert the subscriber by email WITHOUT groups (so existing group
    memberships, e.g. the free list, are left untouched). Returns subscriber id."""
    r = requests.post(f"{ML_BASE}/subscribers", json={"email": email},
                     headers=ml_headers(), timeout=30)
    if r.status_code in (200, 201):
        return (r.json().get("data") or {}).get("id")
    log.warning(f"ML upsert {email} -> {r.status_code}: {r.text[:200]}")
    return None

def ml_assign_group(subscriber_id):
    """Add the subscriber to the Pro group (additive; does not remove other groups)."""
    r = requests.post(f"{ML_BASE}/subscribers/{subscriber_id}/groups/{ML_GROUP}",
                     headers=ml_headers(), timeout=30)
    return r.status_code in (200, 201)

def ml_group_members():
    """email -> subscriber_id for everyone currently in the Pro group (cursor paged)."""
    out = {}
    cursor = None
    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(f"{ML_BASE}/groups/{ML_GROUP}/subscribers",
                        params=params, headers=ml_headers(), timeout=30)
        r.raise_for_status()
        j = r.json()
        for s in j.get("data", []):
            em = (s.get("email") or "").strip().lower()
            if em:
                out[em] = s.get("id")
        cursor = (j.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
    return out

def ml_remove_from_group(subscriber_id):
    """Remove the subscriber from the Pro group only (keeps them on the free list)."""
    r = requests.delete(f"{ML_BASE}/subscribers/{subscriber_id}/groups/{ML_GROUP}",
                       headers=ml_headers(), timeout=30)
    return r.status_code in (200, 204)


# ── reconcile ─────────────────────────────────────────────────────────────────
def main():
    missing = [n for n, v in (("STRIPE_API_KEY", STRIPE_KEY),
                              ("MAILERLITE_API_KEY", ML_KEY),
                              ("MAILERLITE_PRO_GROUP_ID", ML_GROUP)) if not v]
    if missing:
        die("missing config: " + ", ".join(missing))
    if not ML_GROUP.isdigit():
        die(f"MAILERLITE_PRO_GROUP_ID must be a number, got '{ML_GROUP}' "
            "(paste the id, not the dashboard URL).")

    try:
        active = active_subscriber_emails()
    except requests.RequestException as e:
        die(f"Stripe fetch failed ({e}); aborting without touching MailerLite.")
    log.info(f"{len(active)} entitled Pro subscriber(s) in Stripe"
             + (f": {', '.join(sorted(active))}" if active else ""))

    # 1) ensure every active payer is in the Pro group
    ensured = 0
    for em in sorted(active):
        if DRY_RUN:
            log.info(f"[dry-run] would ensure {em} in Pro group")
            ensured += 1
            continue
        sid = ml_get_or_create(em)
        if sid and ml_assign_group(sid):
            ensured += 1
        else:
            log.warning(f"could not add {em} to Pro group")
    log.info(f"ensured {ensured}/{len(active)} active member(s) in Pro group")

    # 2) remove churned members (in the group but no longer entitled)
    if not REMOVE_CHURNED:
        log.info("removal disabled (SYNC_REMOVE_CHURNED=0) — add-only mode")
    elif not active:
        log.info("active set is empty — skipping removals as a safety guard "
                 "(won't clear the group when there are no subscribers yet)")
    else:
        try:
            members = ml_group_members()
        except requests.RequestException as e:
            log.warning(f"couldn't list Pro group members ({e}); skipping removals")
            members = None
        if members is not None:
            churned = [(em, sid) for em, sid in members.items() if em not in active]
            for em, sid in churned:
                if DRY_RUN:
                    log.info(f"[dry-run] would remove churned {em}")
                    continue
                if sid and ml_remove_from_group(sid):
                    log.info(f"removed churned {em} from Pro group")
                else:
                    log.warning(f"failed to remove {em}")
            if not churned:
                log.info("no churned members to remove")

    log.info("sync complete" + (" (dry-run, nothing changed)" if DRY_RUN else ""))


if __name__ == "__main__":
    main()
