#!/usr/bin/env python3
"""Generate a synthetic year of Garmin data for the demo dashboard.

    ./.venv/bin/python demo_data.py            # writes demo/data/*.csv

The README screenshots are built from this, not from anyone's real training, so
the public repo carries no health record. The athlete below is invented: a
consistent club runner with a decent aerobic base, deliberately different from
any real user so the two can never be confused.
"""

import csv
import math
import random
from datetime import date, timedelta
from pathlib import Path

import garmin_sync as G

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "demo" / "data"
SEED = 20261220
END = date(2026, 9, 2)
DAYS = 366

# Invented athlete: 190 max HR, VO2max drifting 49 -> 53 over the year.
MAX_HR = 190


def gen():
    rng = random.Random(SEED)
    start = END - timedelta(days=DAYS - 1)
    acts, sleeps, dailies, readies = [], [], [], []
    aid = 18000000000

    for i in range(DAYS):
        d = start + timedelta(days=i)
        wk = i / 7.0
        # fitness improves through the year, dips over the winter block
        fit = 49 + 4 * (i / DAYS) - 1.2 * math.exp(-((i - 110) ** 2) / 2200)
        base_pace = 5.9 - 0.5 * (i / DAYS)          # easy pace improves
        heat = 20 if 270 <= i <= 340 else 0          # a hot summer block

        dow = d.weekday()
        did_run = dow in (1, 3, 5) or (dow == 6 and rng.random() < 0.35)
        if rng.random() < 0.08:                      # the occasional missed day
            did_run = False

        if did_run:
            if dow == 5:                             # Saturday long run
                km = round(min(30, 14 + wk * 0.28) + rng.uniform(-1.5, 1.5), 2)
                pace = base_pace + rng.uniform(0.15, 0.4)
                hr = rng.randint(148, 158)
                kind = "Long Run"
            elif dow == 1:                           # Tuesday quality
                km = round(rng.uniform(8, 12), 2)
                pace = base_pace - rng.uniform(0.9, 1.4)
                hr = rng.randint(163, 176)
                kind = "Intervals"
            else:                                    # easy days
                km = round(rng.uniform(6, 10), 2)
                pace = base_pace + rng.uniform(0, 0.3)
                hr = rng.randint(136, 150)
                kind = "Easy Run"
            pace = round(pace + (0.25 if heat else 0), 2)
            dur = round(km * pace, 1)
            elev = round(km * rng.uniform(3, 14), 0)
            acts.append({
                "date": d.isoformat(), "activity_id": aid, "name": kind,
                "type": "running", "distance_km": km, "duration_min": dur,
                "pace_min_km": pace, "avg_hr": hr,
                "max_hr": hr + rng.randint(8, 18),
                "calories": round(km * 68), "elev_gain_m": elev,
                "avg_cadence": rng.randint(168, 180),
                "aerobic_te": round(rng.uniform(2.6, 4.1), 1),
                "anaerobic_te": round(rng.uniform(0, 2.4), 1) if kind == "Intervals" else 0.0,
                "training_load": round(dur * rng.uniform(1.3, 2.0)),
                "vo2max": round(fit, 1),
            })
            aid += 1

        if dow in (0, 4) and rng.random() < 0.8:     # gym, twice a week
            dur = round(rng.uniform(40, 65), 1)
            acts.append({
                "date": d.isoformat(), "activity_id": aid, "name": "Strength",
                "type": "strength_training", "distance_km": None,
                "duration_min": dur, "pace_min_km": None,
                "avg_hr": rng.randint(105, 125), "max_hr": rng.randint(140, 160),
                "calories": round(dur * 6), "elev_gain_m": None,
                "avg_cadence": None, "aerobic_te": round(rng.uniform(1.4, 2.4), 1),
                "anaerobic_te": round(rng.uniform(0.4, 1.6), 1),
                "training_load": round(dur * rng.uniform(0.8, 1.3)),
                "vo2max": None,
            })
            aid += 1

        # a seasonal sport that stops partway through the year
        if d.month in (10, 11, 12, 1, 2) and dow == 2 and rng.random() < 0.65:
            dur = round(rng.uniform(55, 75), 1)
            acts.append({
                "date": d.isoformat(), "activity_id": aid, "name": "Club Game",
                "type": "ice_hockey", "distance_km": None, "duration_min": dur,
                "pace_min_km": None, "avg_hr": rng.randint(128, 142),
                "max_hr": rng.randint(175, 190), "calories": round(dur * 8),
                "elev_gain_m": None, "avg_cadence": None,
                "aerobic_te": round(rng.uniform(2.2, 3.4), 1),
                "anaerobic_te": round(rng.uniform(1.8, 3.4), 1),
                "training_load": round(dur * rng.uniform(1.4, 2.2)), "vo2max": None,
            })
            aid += 1

        # overnight data on ~72% of nights
        rhr = round(48 - 3 * (i / DAYS) + rng.uniform(-2.5, 2.5))
        hrv = round(72 + 10 * (i / DAYS) + rng.uniform(-8, 8))
        worn = rng.random() < 0.72
        if worn:
            total = round(rng.gauss(7.4, 0.8), 2)
            total = max(5.2, min(9.2, total))
            deep = round(total * rng.uniform(0.16, 0.24), 2)
            rem = round(total * rng.uniform(0.18, 0.26), 2)
            awake = round(rng.uniform(0.1, 0.5), 2)
            score = max(40, min(98, round(58 + total * 5 + rng.uniform(-9, 9))))
            sleeps.append({
                "date": d.isoformat(), "score": score,
                "quality": "EXCELLENT" if score >= 90 else "GOOD" if score >= 80
                           else "FAIR" if score >= 60 else "POOR",
                "total_h": total, "deep_h": deep,
                "light_h": round(total - deep - rem - awake, 2), "rem_h": rem,
                "awake_h": awake, "resting_hr": rhr,
                "avg_hr": rhr + rng.randint(2, 7), "avg_hrv": hrv,
                "hrv_7d_avg": hrv + rng.randint(-3, 3), "hrv_status": "BALANCED",
                "avg_spo2": None, "avg_respiration": round(rng.uniform(13, 16), 1),
                "body_battery_change": rng.randint(40, 80),
                "sleep_need_h": 8.0, "skin_temp_c": None,
            })

        dailies.append({
            "date": d.isoformat(), "steps": rng.randint(6000, 19000),
            "resting_hr": rhr if worn else rhr + rng.randint(11, 17),
            "hrv_last_night": hrv if worn else None,
            "hrv_status": "BALANCED" if worn else None,
            "stress_avg": rng.randint(22, 48),
            "body_battery_high": rng.randint(78, 100),
            "body_battery_low": rng.randint(8, 32),
            "intensity_min_moderate": rng.randint(0, 30),
            "intensity_min_vigorous": rng.randint(0, 60) if did_run else 0,
            "active_calories": rng.randint(300, 1100),
            "floors_climbed": round(rng.uniform(2, 25), 1),
            "training_status": "PRODUCTIVE" if i % 11 else "MAINTAINING",
            "vo2max": round(fit, 1), "fitness_age": 22,
            "heat_acclimation_pct": heat or None,
        })
        sc = max(20, min(99, round(70 + (hrv - 78) * 0.7 + rng.uniform(-12, 12))))
        readies.append({
            "date": d.isoformat(), "score": sc,
            "level": "HIGH" if sc >= 75 else "MODERATE" if sc >= 50 else "LOW",
            "feedback": "WELL_RECOVERED" if sc >= 75 else "GOOD_SLEEP_HISTORY",
            "sleep_score": sleeps[-1]["score"] if worn and sleeps else None,
            "hrv_factor": "BALANCED", "recovery_time_h": round(rng.uniform(0, 30), 1),
            "acute_load": rng.randint(180, 420), "stress_factor": "GOOD",
        })

    return acts, sleeps, dailies, readies


def write(rows, name, cols):
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / name, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in sorted(rows, key=lambda x: x["date"], reverse=True):
            w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in cols})
    print(f"  {name}: {len(rows)} rows")


if __name__ == "__main__":
    a, s, d, r = gen()
    print(f"Writing synthetic data to {OUT}")
    write(a, "activities.csv", G.ACTIVITY_COLS)
    write(s, "sleep.csv", G.SLEEP_COLS)
    write(d, "daily.csv", G.DAILY_COLS)
    write(r, "readiness.csv", G.READINESS_COLS)
