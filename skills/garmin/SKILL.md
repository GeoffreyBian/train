---
name: garmin
description: Pulls the user's Garmin watch data (runs, workouts, sleep, HRV, recovery, training load) into local CSVs and analyzes it for training insights. Use when the user asks how their training is going, mentions sleep/recovery/HRV/readiness, wants their Garmin data synced or refreshed, asks whether they're overtraining or ready for a hard session, or wants a weekly review. Pairs with the `training` skill, which holds the Hyrox/marathon plan this data is judged against.
---

# Garmin

Objective body data. The `training` skill holds the *plan* (`~/dev/training/plan.md`)
and the subjective log (`log.md`); this skill holds what actually happened.
Answer "how is training going" by reading both.

## Layout

```
~/dev/garmin/
  garmin_sync.py       # Garmin -> CSV
  analyze.py           # CSV -> training report
  sync_details.py      # per-activity splits, HR zones, weather, traces
  refresh.sh           # sync + re-match + rebuild, one command
  week_plan.py         # this week's prescribed runs + auto-matching
  build_dashboard.py   # CSV -> dashboard.html
  dashboard.template.html
  test_analyze.py      # guards on the analysis logic
  data/
    activities.csv     # one row per workout
    sleep.csv          # one row per night
    daily.csv          # one row per day (steps, RHR, HRV, stress, body battery)
    readiness.csv      # one row per morning
    zones.json         # Garmin's own max HR / threshold / zone floors
    details/<id>.json  # per-activity laps, zones, weather, ~160-point trace
    .last_sync
  insights/weekly/     # YYYY-Www.md reviews
  data/week_plan.json
```

## The weekly refresh loop

One command does everything — sync, re-match this week's runs, rebuild the dashboard, run the tests:

```bash
~/dev/garmin/refresh.sh              # add --no-sync to rebuild without hitting Garmin
```

Run it whenever he asks how the week is going, logs new activity, or wants the
dashboard refreshed. It is safe to re-run and degrades gracefully: if Garmin
rate-limits or the tokens have expired it says so and rebuilds from existing
data rather than failing.

Then **republish `dashboard.html` to the URL in `insights/ARTIFACT.txt`**, passing
that URL as `url`. Publishing without it creates a second artifact and orphans
the ticks stored against the first.

If `refresh.sh` reports an auth error, tokens expired (~6 months). He has to run
`./.venv/bin/python garmin_sync.py --login` himself — it prompts for a password
and MFA code.

Never hand-edit `dashboard.html`; it is regenerated every refresh. Edit
`dashboard.template.html`. The build fails loudly if a `{{token}}` goes unfilled.

## The week board

`week_plan.py` prescribes **three runs and an optional fourth**, from his real
recent training rather than the template in `training/plan.md`. The long run
steps +2 km from his actual 4-week ceiling and is capped, so a missed week can't
let the target run away.

Runs tick themselves off against `activities.csv`. A completed row shows the day
he *actually* ran, not the day it was slotted for.

Strength and station work are deliberately not on this board — Garmin has never
recorded any, so a box for them could never resolve from data. Keep that
prescription in `training/plan.md` and judge it in conversation, not here.

Manual ticks (for a run the watch missed) live in the artifact's `db` under
`ticks/<week>__<session id>`. Read them back with the Artifact tool's `read_db`
when judging adherence.

## Drill-down views

The dashboard is hash-routed: `#/activities`, `#/activity/<id>`, `#/nights`,
`#/night/<date>`, `#/days`, `#/day/<date>`. When he asks about a specific run,
night or day, point him at the URL rather than retyping the numbers.

`sync_details.py` fetches lap splits, time in each HR zone, weather and a
downsampled HR/pace/elevation/cadence trace, one JSON per activity, fetched once
and kept. `refresh.sh` tops up anything missing.

**Heart-rate zones come from `data/zones.json`**, which is Garmin's own model for
him (max 198, threshold 177, floors 99/119/139/158/178). Do not infer a max HR
from observed peaks — that understated it as 194 and moved the intensity
distribution from 71% to 38% in Z3, which is a completely different read on his
training.

Two unit traps already handled: Garmin's *series* cadence is per-leg while its
*lap* cadence is steps/min, and a single bad GPS sample can put a pace value
outside any runnable range. Both are corrected at render time.

## Live queries

## Drill-down views

The dashboard is hash-routed: `#/activities`, `#/activity/<id>`, `#/nights`,
`#/night/<date>`, `#/days`, `#/day/<date>`. When he asks about a specific run,
night or day, point him at the URL rather than retyping the numbers.

`sync_details.py` fetches lap splits, time in each HR zone, weather and a
downsampled HR/pace/elevation/cadence trace, one JSON per activity, fetched once
and kept. `refresh.sh` tops up anything missing.

**Heart-rate zones come from `data/zones.json`**, which is Garmin's own model for
him (max 198, threshold 177, floors 99/119/139/158/178). Do not infer a max HR
from observed peaks — that understated it as 194 and moved the intensity
distribution from 71% to 38% in Z3, which is a completely different read on his
training.

Two unit traps already handled: Garmin's *series* cadence is per-leg while its
*lap* cadence is steps/min, and a single bad GPS sample can put a pace value
outside any runnable range. Both are corrected at render time.

## Live queries

The `garmin` MCP server exposes 110+ live tools for anything not in the CSVs:
per-activity splits, HR zone distribution, gear mileage, race predictions,
and writing workouts back to the watch. Prefer the CSVs for anything
historical or trend-shaped — they're already local and cheaper to scan.

## Analysis

```bash
cd ~/dev/garmin && ./.venv/bin/python analyze.py           # full report
                   ./.venv/bin/python analyze.py --write   # also insights/latest.md
```

Run this before answering anything about how training is going, rather than
recomputing from the CSVs by hand. It covers race clock, volume, plan adherence,
terrain-corrected aerobic efficiency, VO2max/heat, intensity distribution,
acute:chronic load, cross-training, Hyrox-specific gaps, seasonal load, recovery,
sleep, and a verdict.

`test_analyze.py` (pytest) guards the two corrections that are easy to
reintroduce — terrain control and the resting-HR source. Run it after editing
`analyze.py`.

## Two data traps, already handled — do not undo them

**Resting HR.** Garmin reports `restingHeartRate` every day, including the many
nights the watch wasn't worn, where it derives one from daytime readings that
runs materially high. On paired nights the two sources agree exactly; on unworn
nights the value is fiction. `analyze.py` reads RHR only from `sleep.csv`. Read
it from `daily.csv` and the trend measures watch-wearing habits, not fitness.

**Terrain.** Pace at a fixed heart rate is the best aerobic-fitness read, but
only on comparable ground. A move to hillier routes has already accounted for
more than half of one apparent decline. Always quote the flat-run figure and say
so; `analyze.py` prints both columns with the climbing rate beside them. Heat
acclimation (in `daily.csv`) is the second confounder — check it before calling
a summer slowdown a loss of fitness.

## Reading the data

- **Rest** is a date in `daily.csv` with no row in `activities.csv` for it. A date
  missing from `daily.csv` entirely means the watch didn't sync, not a rest day.
  Never count those as rest.
- **Compare to baseline, not to absolutes.** An HRV number means nothing alone;
  it means something against his own trailing 28-day average. Same for resting HR.
  Compute the baseline from the CSV before making any claim about it.
- **Weekly running volume** is the single number that matters most for the
  marathon build. Sum `distance_km` where `type` contains `running`, by ISO week.
- **Acute:chronic load ratio** — 7-day training load sum over the 28-day daily
  average, from `activities.csv`. Above ~1.5 is a spike worth flagging.
- Garmin's own `training_status` and readiness `feedback` are useful but blunt.
  Cite them, don't defer to them.

## Insights: what to actually look for

Lead with what changed and what to do about it. Specifics from his data, not
general training advice he could get anywhere.

- Easy runs drifting too fast (aerobic base gets built at easy pace, and the
  plan's Z2 days are where this leaks)
- Sleep debt stacking up ahead of a key session
- Resting HR trending up or HRV trending down over 7+ days while load climbs —
  the classic overreaching signature
- Compromised-running progress: pace on runs immediately following station work
  vs. fresh runs. This is the Hyrox limiter named in `plan.md`.
- Week-over-week mileage jumps over ~10%
- Sessions in `plan.md` that never appeared in the data

Say when the data doesn't support a conclusion. Three nights of poor sleep is
not a trend, and a single missed workout is not a pattern.

## Weekly review

On request, or when a week has closed and hasn't been reviewed, write
`insights/weekly/YYYY-Www.md`:

- Planned vs. actual, per `~/dev/training/plan.md`
- Running volume and how it moved week over week
- Sleep: average, worst night, whether it tracks with hard days
- Recovery trend: HRV, resting HR, readiness — direction, against baseline
- Two or three concrete changes for next week

Keep it short and specific. No motivational wrap-up.
