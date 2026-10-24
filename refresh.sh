#!/usr/bin/env bash
# One-command refresh: pull new Garmin data, top up per-activity detail,
# re-match this week's runs, rebuild the dashboard. Safe to re-run.
#
# Sources: the Personal API key in .env (official; activities, sleep, daily,
# HRV, weight) plus the legacy ~/.garminconnect session for what the key
# cannot reach (readiness, training status, VO2max, laps). Either alone still
# syncs what it can; see garmin_sync.py.
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
  # With a key configured, close stdin so an expired legacy session is skipped
  # (and reported) instead of stopping the refresh at a password prompt.
  # Without one, keep the old behaviour and let it ask.
  if grep -q '^GARMIN_PERSONAL_API_KEY=.' .env 2>/dev/null; then
    $PY garmin_sync.py </dev/null
  else
    $PY garmin_sync.py
  fi
  code=$?
  if [ $code -ne 0 ]; then
    if [ $code -eq 2 ]; then
      echo "   Garmin rate-limited this IP. Rebuilding from existing data instead." >&2
    else
      echo "   Sync failed (exit $code): neither source worked." >&2
      echo "     Personal API key: renew at connect.garmin.com/app/settings/personalApiToken," >&2
      echo "       then set GARMIN_PERSONAL_API_KEY in $(pwd)/.env" >&2
      echo "     Legacy session:   $PY garmin_sync.py --login" >&2
      echo "   Rebuilding from existing data." >&2
    fi
  fi

  echo
  echo "==> Activity detail (splits, zones, traces)"
  $PY sync_details.py </dev/null || echo "   detail incomplete; the dashboard still builds" >&2
fi

echo
echo "==> This week"
$PY week_plan.py || exit 1
$PY week_plan.py --json >/dev/null

echo "==> Rebuilding dashboard"
$PY build_dashboard.py || exit 1

# docs/index.html is what GitHub Pages serves; keep it in step with the build.
cp dashboard.html docs/index.html 2>/dev/null && echo "   docs/index.html updated"

# Publishing is part of the refresh, not a separate step: fresh data that never
# reaches geoffreybian.github.io/train is the same as no refresh at all.
# Pass --no-web to stop before this.
if [ "${1:-}" = "--no-web" ] || [ "${2:-}" = "--no-web" ]; then
  echo "   --no-web: not pushing to GitHub"
elif [ -n "$(git status --porcelain -- docs/index.html data insights)" ]; then
  git add -A docs/index.html data insights
  git commit -q -m "Refresh $(date +%Y-%m-%d)" && \
    git push -q && echo "   pushed — live at https://geoffreybian.github.io/train/"
else
  echo "   nothing changed — no push"
fi

echo
echo "==> Checks"
$PY -m pytest test_analyze.py test_personal_api.py -q 2>&1 | tail -3

echo
echo "Dashboard rebuilt: $(pwd)/dashboard.html"
if [ -f insights/ARTIFACT.txt ]; then
  echo "Publish it to:     $(cat insights/ARTIFACT.txt)"
  echo "Ask Claude: \"republish my garmin dashboard\" (it must reuse that URL,"
  echo "otherwise you get a second artifact and lose your saved ticks)."
fi
