#!/usr/bin/env bash
# One-command refresh: pull new Garmin data, top up per-activity detail,
# re-match this week's runs, rebuild the dashboard. Safe to re-run.
set -uo pipefail
cd "$(dirname "$0")"
PY=./.venv/bin/python

if [ ! -x "$PY" ]; then
  echo "error: $PY missing. Recreate it with:" >&2
  echo "  uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python garminconnect curl_cffi pytest" >&2
  exit 1
fi

SKIP_SYNC=0
[ "${1:-}" = "--no-sync" ] && SKIP_SYNC=1

if [ "$SKIP_SYNC" -eq 1 ]; then
  echo "==> Skipping sync (--no-sync)"
else
  echo "==> Syncing Garmin"
  if ! $PY garmin_sync.py; then
    code=$?
    if [ $code -eq 2 ]; then
      echo "   Garmin rate-limited this IP. Rebuilding from existing data instead." >&2
    else
      echo "   Sync failed (exit $code). If it is an auth error your tokens expired:" >&2
      echo "     $PY garmin_sync.py --login" >&2
      echo "   Rebuilding from existing data." >&2
    fi
  fi

  echo
  echo "==> Activity detail (splits, zones, traces)"
  $PY sync_details.py || echo "   detail incomplete; the dashboard still builds" >&2
fi

echo
echo "==> This week"
$PY week_plan.py || exit 1
$PY week_plan.py --json >/dev/null

echo "==> Rebuilding dashboard"
$PY build_dashboard.py || exit 1

echo
echo "==> Checks"
$PY -m pytest test_analyze.py -q 2>&1 | tail -3

echo
echo "Dashboard rebuilt: $(pwd)/dashboard.html"
if [ -f insights/ARTIFACT.txt ]; then
  echo "Publish it to:     $(cat insights/ARTIFACT.txt)"
  echo "Ask Claude: \"republish my garmin dashboard\" (it must reuse that URL,"
  echo "otherwise you get a second artifact and lose your saved ticks)."
fi
