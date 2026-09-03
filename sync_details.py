#!/usr/bin/env python3
"""Per-activity detail: splits, HR zones, weather, and a downsampled time series.

    ./.venv/bin/python sync_details.py              # fill in anything missing
    ./.venv/bin/python sync_details.py --limit 40   # cap the work
    ./.venv/bin/python sync_details.py --force      # refetch everything

The summary CSVs answer "how is training going". These answer "what happened on
that run". One JSON per activity under data/details/, fetched once and kept —
Garmin ages detail out, and re-pulling 500 requests on every sync is rude.
"""

import argparse
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from garmin_sync import login, rnd, g

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DET = DATA / "details"
RUN_TYPES = ("running", "treadmill_running", "trail_running")

# Which time-series metrics to keep, and how to name them.
SERIES = {
    "sumDistance": "dist", "directHeartRate": "hr", "directSpeed": "spd",
    "directElevation": "elev", "directRunCadence": "cad",
    "directGradeAdjustedSpeed": "gap", "directStrideLength": "stride",
    "directGroundContactTime": "gct", "directVerticalOscillation": "vo",
    "directPower": "pwr",
}


def lap_row(l, i):
    sp = l.get("averageSpeed") or 0
    gsp = l.get("avgGradeAdjustedSpeed") or 0
    return {
        "i": i + 1,
        "km": rnd((l.get("distance") or 0) / 1000, 2),
        "sec": rnd(l.get("duration"), 0),
        "pace": rnd(1000 / sp / 60, 2) if sp else None,
        "gap": rnd(1000 / gsp / 60, 2) if gsp else None,
        "hr": rnd(l.get("averageHR"), 0),
        "maxhr": rnd(l.get("maxHR"), 0),
        "cad": rnd(l.get("averageRunCadence"), 0),
        "elev": rnd(l.get("elevationGain"), 0),
        "drop": rnd(l.get("elevationLoss"), 0),
        "pwr": rnd(l.get("averagePower"), 0),
        "stride": rnd(l.get("strideLength"), 0),
        "gct": rnd(l.get("groundContactTime"), 0),
        "vo": rnd(l.get("verticalOscillation"), 1),
        "vr": rnd(l.get("verticalRatio"), 1),
    }


def series(api, aid, points=160):
    """Downsampled time series. Full 1 Hz would be ~4000 rows per run."""
    d = api.get_activity_details(aid, maxchart=points, maxpoly=points) or {}
    desc = d.get("metricDescriptors") or []
    idx = {m.get("key"): m.get("metricsIndex") for m in desc}
    rows = d.get("activityDetailMetrics") or []
    if not rows:
        return None
    out = {v: [] for v in SERIES.values()}
    for r in rows:
        m = r.get("metrics") or []
        for key, name in SERIES.items():
            j = idx.get(key)
            v = m[j] if j is not None and j < len(m) else None
            out[name].append(rnd(v, 2) if isinstance(v, (int, float)) else None)
    # drop metrics this device never recorded
    return {k: v for k, v in out.items() if any(x for x in v)}


def fetch(api, act, want_series):
    aid = act["activity_id"]
    rec = {"activity_id": aid, "date": act["date"], "name": act["name"],
           "type": act["type"]}
    try:
        sp = api.get_activity_splits(aid) or {}
        laps = sp.get("lapDTOs") or []
        rec["laps"] = [lap_row(l, i) for i, l in enumerate(laps)]
    except Exception:
        rec["laps"] = []
    try:
        z = api.get_activity_hr_in_timezones(aid) or []
        rec["zones"] = [{"z": x.get("zoneNumber"), "sec": rnd(x.get("secsInZone"), 0),
                         "lo": x.get("zoneLowBoundary")} for x in z]
    except Exception:
        rec["zones"] = []
    try:
        w = api.get_activity_weather(aid) or {}
        if w.get("temp") is not None:
            rec["weather"] = {
                "tempC": rnd((w["temp"] - 32) * 5 / 9, 1),
                "feelsC": rnd(((w.get("apparentTemp") or w["temp"]) - 32) * 5 / 9, 1),
                "humidity": w.get("relativeHumidity"),
                "wind": w.get("windSpeed"),
                "windDir": w.get("windDirectionCompassPoint"),
                "desc": g(w, "weatherTypeDTO", "desc"),
            }
    except Exception:
        pass
    if want_series:
        try:
            s = series(api, aid)
            if s:
                rec["series"] = s
        except Exception:
            pass
    try:
        s = (api.get_activity(aid) or {}).get("summaryDTO") or {}
        rec["summary"] = {
            "gapPace": rnd(1000 / s["avgGradeAdjustedSpeed"] / 60, 2)
                       if s.get("avgGradeAdjustedSpeed") else None,
            "minHr": rnd(s.get("minHR"), 0),
            "steps": s.get("steps"),
            "movingSec": rnd(s.get("movingDuration"), 0),
            "elapsedSec": rnd(s.get("elapsedDuration"), 0),
            "np": rnd(s.get("normalizedPower"), 0),
            "teLabel": s.get("trainingEffectLabel"),
            "aeMsg": s.get("aerobicTrainingEffectMessage"),
            "anMsg": s.get("anaerobicTrainingEffectMessage"),
            "bbDelta": s.get("differenceBodyBattery"),
            "rpe": s.get("directWorkoutRpe"),
            "feel": s.get("directWorkoutFeel"),
            "water": rnd(s.get("waterEstimated"), 0),
        }
    except Exception:
        pass
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    DET.mkdir(parents=True, exist_ok=True)
    with open(DATA / "activities.csv", newline="") as f:
        acts = list(csv.DictReader(f))
    todo = [a for a in acts
            if args.force or not (DET / f"{a['activity_id']}.json").exists()]
    # newest first, and runs before everything else
    todo.sort(key=lambda a: (a["type"] not in RUN_TYPES, a["date"]), reverse=False)
    todo.sort(key=lambda a: (a["type"] not in RUN_TYPES, -int(a["activity_id"])))
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        print(f"Details already complete for {len(acts)} activities.")
    else:
        print(f"Connecting to Garmin Connect...")
        api = login()
        print(f"  fetching detail for {len(todo)} of {len(acts)} activities\n")
        ok = err = 0
        for n, a in enumerate(todo, 1):
            try:
                rec = fetch(api, a, a["type"] in RUN_TYPES)
                (DET / f"{a['activity_id']}.json").write_text(
                    json.dumps(rec, separators=(",", ":")))
                ok += 1
                print(f"  [{n}/{len(todo)}] {a['date']} {a['type']:20s} "
                      f"{len(rec.get('laps', []))} laps"
                      + (f", {len(rec.get('series', {}).get('hr', []))} pts"
                         if rec.get("series") else ""))
            except Exception as e:
                err += 1
                print(f"  [{n}/{len(todo)}] {a['date']} FAILED {type(e).__name__}")
            time.sleep(0.3)
        print(f"\n{ok} written, {err} failed")

        # athlete HR zones, so nothing has to guess a max HR
        try:
            z = (api.get_heart_rate_zones() or [{}])[0]
            (DATA / "zones.json").write_text(json.dumps({
                "maxHr": z.get("maxHeartRateUsed"),
                "restingHr": z.get("restingHeartRateUsed"),
                "lthr": z.get("lactateThresholdHeartRateUsed"),
                "floors": [z.get(f"zone{i}Floor") for i in range(1, 6)],
            }, indent=1))
            print(f"wrote {DATA / 'zones.json'}")
        except Exception as e:
            print(f"zones: {type(e).__name__}")

    total = len(list(DET.glob("*.json")))
    size = sum(p.stat().st_size for p in DET.glob("*.json"))
    print(f"{total} detail files, {size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
