#!/usr/bin/env bash
# PDUFA Pro — Stripe → MailerLite sync runner (cron every ~15 min).
set -euo pipefail
cd /opt/pdufa-pro/repo
git pull --quiet || true          # pick up any pushed sync changes; never fatal
set -a                            # export everything sourced from config.env
. /opt/pdufa-pro/config.env
set +a
python3 pdufa_pro_sync.py
