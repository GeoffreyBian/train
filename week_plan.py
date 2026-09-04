#!/usr/bin/env python3
"""This week's prescribed sessions, and what the Garmin data says you did.

    ./.venv/bin/python week_plan.py           # show the week
    ./.venv/bin/python week_plan.py --json    # emit data/week_plan.json

Sessions are generated from where the training ACTUALLY is, not from the
aspirational template in training/plan.md. The long run grows from the real
recent ceiling, capped at +2 km/week, so a missed week doesn't let the target
run away from him.

Completion is derived from activities.csv, not asserted. Anything Garmin cannot
see (untracked lifting) stays open here and is ticked by hand on the dashboard.
"""

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze as A

ROOT = Path(__file__).resolve().parent
LONG_STEP = 2.0        # km added per week
LONG_CAP = 26.0        # Phase 2b ceiling; marathon block raises this later
WEEK_FLOOR = 20.0      # minimum weekly km before chasing bigger weeks
REP_PACE = 5.0         # 1 km repeats: target min/km
# A tempo effort is slower than repeats but clearly harder than easy running.
# Anything in (REP_PACE + 0.4, TEMPO_PACE] over a sustained distance is tempo.
TEMPO_PACE = REP_PACE + 1.0
TEMPO_MIN_KM = 4.0


def monday(d):
    return d - timedelta(days=d.weekday())


def prescribe(D, wk_start):
    """Build this week's runs from recent actual training.

    Runs only. Strength and station work belong in training/plan.md — Garmin has
    no record of any, so putting them on this board would only ever show an
    unticked box that the data can never resolve.
    """
    runs = D.runs()
    prior = [r for r in runs if wk_start - timedelta(days=28) <= r["_d"] < wk_start]
    ceiling = max((r["_km"] for r in prior), default=8.0)
    long_target = round(min(ceiling + LONG_STEP, LONG_CAP) * 2) / 2

    recent_fast = [r for r in runs if r["_pace"] and r["_pace"] < 5.3
                   and r["_d"] >= wk_start - timedelta(days=60)]
    reps = 5 if not recent_fast else 6

    # Whatever the floor still needs after the long run and the repeats,
    # clamped to a sane single-session range.
    easy_km = min(8.0, max(6.0, WEEK_FLOOR - long_target - reps * 1.6))

    return [
        {"id": "reps", "day": "Tue", "kind": "run_intervals",
         "title": f"{reps} \u00d7 1 km @ {A.fmt_pace(REP_PACE)}/km",
         "detail": "90 s jog recovery. This is the race: eight 1 km efforts off "
                   "fatigue. Start conservative \u2014 the first sessions rebuild a "
                   "capacity you had in April.",
         "target": {"max_pace": 5.4, "min_km": 5.0}},
        {"id": "easy", "day": "Thu", "kind": "run_easy",
         "title": f"Easy run \u2014 {easy_km:.0f} km",
         "detail": "Genuinely easy, HR under 150. Puts a floor under the week; "
                   "your week-to-week swing hurts more than low mileage does.",
         "target": {"min_km": round(easy_km * 0.7, 1)}},
        {"id": "long", "day": "Sat", "kind": "run_long",
         "title": f"Long run \u2014 {long_target:.0f} km easy",
         "detail": f"Conversational, HR under 155. Up {LONG_STEP:.0f} km from your "
                   f"{ceiling:.1f} km ceiling \u2014 the only sustainable way back to "
                   f"the 22 km Phase 2b wants.",
         "target": {"min_km": round(long_target * 0.85, 1)}},
        {"id": "tempo", "day": "Sun", "kind": "run_tempo",
         "title": f"Optional: tempo 5\u20138 km @ {A.fmt_pace(TEMPO_PACE)}/km or quicker",
         "detail": "Only if the three above are done. Comfortably hard, HR in the "
                   "155\u2013170 band \u2014 the pace you could hold for an hour. This is "
                   "what most of your running already drifts into by accident; "
                   "doing it deliberately once a week is worth more than three "
                   "runs that all land there.",
         "target": {"max_pace": TEMPO_PACE, "min_km": TEMPO_MIN_KM},
         "optional": True},
    ]


def match(D, sessions, wk_start):
    """Tick sessions off against what actually got recorded."""
    wk_end = wk_start + timedelta(days=6)
    acts = [r for r in D.acts if wk_start <= r["_d"] <= wk_end]
    used = set()

    def take(pred):
        for r in acts:
            if id(r) in used or not pred(r):
                continue
            used.add(id(r))
            return r
        return None

    # Most specific first, so a long run isn't consumed by the "easy" slot.
    order = sorted(sessions, key=lambda s: {"run_long": 0, "run_intervals": 1,
                                            "run_tempo": 2, "run_easy": 3}[s["kind"]])
    for s in order:
        t = s["target"]
        hit = None
        if s["kind"] == "run_long":
            hit = take(lambda r: r["_type"] in A.RUN_TYPES and (r["_km"] or 0) >= t["min_km"])
        elif s["kind"] == "run_intervals":
            hit = take(lambda r: r["_type"] in A.RUN_TYPES and (r["_km"] or 0) >= t["min_km"]
                       and ((r["_pace"] or 99) <= t["max_pace"]
                            or (A.f(r.get("anaerobic_te")) or 0) >= 1.0))
        elif s["kind"] == "run_tempo":
            hit = take(lambda r: r["_type"] in A.RUN_TYPES
                       and (r["_km"] or 0) >= t["min_km"]
                       and (r["_pace"] or 99) <= t["max_pace"])
        elif s["kind"] == "run_easy":
            hit = take(lambda r: r["_type"] in A.RUN_TYPES and (r["_km"] or 0) >= t["min_km"])
        if hit:
            s["done"] = {
                "date": hit["_d"].isoformat(),
                # The board shows when it actually happened; the prescribed day
                # is only a suggestion and a Tuesday run must not read "Thu".
                "day": hit["_d"].strftime("%a"),
                "name": hit["name"] or hit["_type"],
                "km": round(hit["_km"], 1) if hit["_km"] else None,
                "pace": round(hit["_pace"], 2) if hit["_pace"] else None,
                "hr": int(hit["_hr"]) if hit["_hr"] else None,
            }
        else:
            s["done"] = None

    # Anything recorded that no session claimed still counts toward the week.
    extra = [{"date": r["_d"].isoformat(), "name": r["name"] or r["_type"],
              "type": r["_type"],
              "km": round(r["_km"], 1) if r["_km"] else None}
             for r in acts if id(r) not in used]
    return sessions, extra


DAY_ORDER = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


def build(D=None):
    D = D or A.Data()
    wk_start = monday(D.today)
    sessions = prescribe(D, wk_start)
    sessions, extra = match(D, sessions, wk_start)
    # Matching runs most-specific-first; the board reads Mon -> Sun.
    sessions.sort(key=lambda s: DAY_ORDER[s["day"]])
    wk_end = wk_start + timedelta(days=6)
    done_km = sum(r["_km"] or 0 for r in D.acts
                  if wk_start <= r["_d"] <= wk_end and r["_type"] in A.RUN_TYPES)
    planned_km = sum(s["target"].get("min_km", 0) for s in sessions
                     if not s.get("optional"))
    return {
        "week_start": wk_start.isoformat(),
        "week_end": wk_end.isoformat(),
        "week_label": f"{wk_start.isocalendar()[0]}-W{wk_start.isocalendar()[1]:02d}",
        "data_through": D.today.isoformat(),
        "sessions": sessions,
        "unclaimed": extra,
        "km_done": round(done_km, 1),
        "km_floor": WEEK_FLOOR,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    w = build()
    if args.json:
        p = ROOT / "data" / "week_plan.json"
        p.write_text(json.dumps(w, indent=1))
        print(f"wrote {p}")
        return
    print(f"\n\033[1mWEEK OF {w['week_start']} ({w['week_label']})\033[0m")
    print(f"data through {w['data_through']}   "
          f"{w['km_done']:.1f} / {w['km_floor']:.0f} km floor\n")
    for s in w["sessions"]:
        d = s["done"]
        mark = "[x]" if d else ("[ ]" if not s.get("optional") else "[-]")
        day = d["day"] if d else s["day"]      # actual day once it is done
        print(f"  {mark} {day:4s} {s['title']}")
        if d:
            bits = [d["date"]]
            if d["km"]:
                bits.append(f"{d['km']} km")
            if d["pace"]:
                bits.append(f"{A.fmt_pace(d['pace'])}/km")
            if d["hr"]:
                bits.append(f"HR {d['hr']}")
            print(f"          done: {'  '.join(bits)}")
    if w["unclaimed"]:
        print("\n  also recorded this week:")
        for e in w["unclaimed"]:
            print(f"    {e['date']}  {e['name']} ({e['type']})"
                  + (f"  {e['km']} km" if e["km"] else ""))
    print()


if __name__ == "__main__":
    main()
