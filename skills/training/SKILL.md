---
name: training
description: Tracks and plans the user's concurrent Hyrox and marathon training. Use when the user logs a workout, asks about their training plan, mentions Hyrox, running mileage, lifting, race prep, or wants their week/plan adjusted.
---

# Training (Hyrox + Marathon)

A real periodized plan exists in `plan.md` (Vancouver Hyrox, Dec 20 2026, Open division; marathon after). Concurrent Hyrox + marathon training is demanding — running volume for the marathon competes with the strength/station work for Hyrox — so treat `plan.md` as the source of truth and adjust it rather than regenerating it.

**Objective data lives in the `garmin` skill** (`~/dev/garmin/data/`): every workout, sleep, HRV, recovery, training load, straight off the watch. This skill holds the plan and the subjective log. Answer "how is training going" from both — the plan says what was supposed to happen, `log.md` says how it felt, and the Garmin CSVs say what actually happened.

## Data layout

```
training/
  plan.md   # current weekly plan / periodization
  log.md    # dated workout log, most recent entry on top
```

Garmin already captures distance, pace, HR, and duration automatically — don't
re-type those into `log.md`. Log what the watch can't see: RPE, how it felt,
station technique notes, niggles, why a session was cut short.

## Still open

- Marathon date is unconfirmed. Phase 4 of `plan.md` assumes spring; pin it down and rewrite that phase once the user registers.

## Working with the log and plan

- `log.md`: append each workout with date, type (run/lift/Hyrox stations/rest), duration or distance, and how it felt (RPE or notes). Newest entry at the top.
- `plan.md`: the current week's plan plus the overall periodization (base/build/peak/taper), rewritten as blocks complete — not a permanent artifact, keep it current.
- When the user logs a workout, write it to `log.md` immediately; don't just acknowledge it in conversation.
- Flag conflicts explicitly when marathon and Hyrox demands collide in the same week (e.g. long run the day before a heavy sled day) rather than silently picking one.
- Before adjusting the plan, sync and read the Garmin data (`garmin` skill). Adjust off resting HR / HRV / readiness trends and actual completed volume, not off how the week was supposed to look.
