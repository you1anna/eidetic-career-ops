#!/usr/bin/env bash
#
# Daily ingest on the VPS: the same `wo ingest --keyless` the Mac runs (tools/ingest.py). The owner approved
# the free sources (decision #18), Companies House (#29) and the Adzuna and Reed feeds (#39). The alerts
# group runs separately under wo-email.timer, with its own delivery and processing receipt.
# It runs each adapter in isolation, imports through wo import-scans and records one
# receipt. Keys arrive as systemd credentials (wo-scan.service), never in the environment; Companies
# House and the feeds each run once their keys are installed. wo ingest takes the shared writer lock itself.
#
set -euo pipefail
keyed=(--keyless)
creds=${CREDENTIALS_DIRECTORY:-/nonexistent}
if [[ -s "$creds/COMPANIES_HOUSE_API_KEY" ]]; then keyed+=(--keyed companies-house); fi
if [[ -s "$creds/ADZUNA_APP_ID" && -s "$creds/ADZUNA_APP_KEY" && -s "$creds/REED_API_KEY" ]]; then keyed+=(--keyed feeds); fi
exec /srv/work-ops/venv/bin/python /srv/work-ops/work-ops/tools/wo.py --db /srv/work-ops/data/work-ops.db \
  ingest "${keyed[@]}" --data /srv/work-ops/data
