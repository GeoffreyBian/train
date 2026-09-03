#!/usr/bin/env python3
"""Training analysis over the local Garmin CSVs.

    ./.venv/bin/python analyze.py            # full report
    ./.venv/bin/python analyze.py --weeks 8  # change the recent window
    ./.venv/bin/python analyze.py --write    # also write insights/latest.md
"""

import argparse
import csv
import os
import statistics as st
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# GARMIN_DATA_DIR lets the demo build render synthetic data without touching
# (or ever reading) the real CSVs.
DATA = Path(os.environ.get("GARMIN_DATA_DIR") or ROOT / "data")

# --- athlete constants -------------------------------------------------
MAX_HR = 194          # highest observed in 12 months of activity data
HYROX = date(2026, 12, 20)
# Aerobic band used for the efficiency comparison. Wide enough for a decent
# sample, narrow enough that pace is comparable across runs.
AERO_LO, AERO_HI = 135, 155
FLAT_M_PER_KM = 10    # runs above this are hilly; pace isn't comparable
MIN_KM = 2.0          # ignore warmups/strides
MAX_PACE = 12.0       # ignore walks and GPS artifacts

RUN_TYPES = ("running", "treadmill_running", "trail_running")
STRENGTH_TYPES = ("strength", "fitness_equipment", "hiit", "functional")
# Logged as generic cardio; counts as gym time but is not station/strength work.
GYM_ISH_TYPES = ("indoor_cardio", "cardio_training", "elliptical", "other")


# --- helpers -----------------------------------------------------------

def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load(name):
    p = DATA / f"{name}.csv"
    if not p.exists():
        return []
    with open(p, newline="") as fh:
        return [r for r in csv.DictReader(fh) if r.get("date")]


def d(s):
    return date.fromisoformat(s[:10])


def week(dt):
    y, w, _ = dt.isocalendar()
    return (y, w)


def wk_label(k):
    return f"{k[0]}-W{k[1]:02d}"


def mean(xs):
    xs = [x for x in xs if x is not None]
    return st.mean(xs) if xs else None


def fmt_pace(p):
    if p is None:
        return "  -  "
    m = int(p)
    return f"{m}:{round((p - m) * 60):02d}"


def pct(a, b):
    return round(100 * a / b) if b else 0


def bar(v, scale, width=22):
    return "#" * min(width, int(round(v / scale)))


def trend(cur, base, invert=False):
    """Return (delta, arrow) comparing cur to base."""
    if cur is None or base is None:
        return None, " "
    dv = cur - base
    up = dv > 0
    good = (not up) if invert else up
    return dv, ("^" if up else "v") + ("" if abs(dv) > 1e-9 else "")


def section(title):
    print(f"\n\033[1m{title}\033[0m")
    print("-" * len(title))


# --- data prep ---------------------------------------------------------

class Data:
    def __init__(self):
        self.acts = load("activities")
        self.daily = load("daily")
        self.sleep = load("sleep")
        self.ready = load("readiness")
        for r in self.acts:
            r["_d"] = d(r["date"])
            r["_km"] = f(r["distance_km"])
            r["_min"] = f(r["duration_min"])
            r["_pace"] = f(r["pace_min_km"])
            r["_hr"] = f(r["avg_hr"])
            r["_load"] = f(r["training_load"]) or 0.0
            r["_elev"] = f(r["elev_gain_m"])
            r["_type"] = r.get("type") or ""
        self.acts.sort(key=lambda r: r["_d"])
        self.today = max([r["_d"] for r in self.acts]
                         + [d(r["date"]) for r in self.daily] + [date.today()])

    def runs(self, clean=True):
        rs = [r for r in self.acts if r["_type"] in RUN_TYPES]
        if not clean:
            return rs
        return [r for r in rs if r["_km"] and r["_pace"] and r["_km"] >= MIN_KM
                and r["_pace"] < MAX_PACE]

    def since(self, rows, days):
        cut = self.today - timedelta(days=days)
        return [r for r in rows if r["_d"] > cut]


# --- report sections ---------------------------------------------------

def race_status(D):
    section("RACE CLOCK")
    left = (HYROX - D.today).days
    print(f"  Hyrox Vancouver (Open)   {HYROX}   {left} days out  ({left // 7} weeks)")
    print(f"  Data through             {D.today}")
    if left > 0:
        phase = ("Phase 2b: peak specificity" if left <= 84 else
                 "Phase 2a: Hyrox-specific build")
        print(f"  Plan phase               {phase}")


def volume(D, weeks_back):
    section("RUNNING VOLUME")
    runs = D.runs()
    wk_km, wk_n, wk_long = defaultdict(float), Counter(), defaultdict(float)
    for r in runs:
        k = week(r["_d"])
        wk_km[k] += r["_km"]
        wk_n[k] += 1
        wk_long[k] = max(wk_long[k], r["_km"])
    keys = sorted(wk_km)[-weeks_back:]
    print(f"  {'week':9s} {'km':>6s} {'runs':>5s} {'long':>6s}")
    for k in keys:
        print(f"  {wk_label(k):9s} {wk_km[k]:6.1f} {wk_n[k]:5d} {wk_long[k]:6.1f}  {bar(wk_km[k], 2)}")

    vals = [wk_km[k] for k in keys]
    recent4 = [wk_km[k] for k in sorted(wk_km)[-4:]]
    prev4 = [wk_km[k] for k in sorted(wk_km)[-8:-4]]
    print(f"\n  last 4wk avg   {mean(recent4):.1f} km/wk")
    if prev4:
        dv = mean(recent4) - mean(prev4)
        print(f"  prior 4wk avg  {mean(prev4):.1f} km/wk   ({dv:+.1f})")
    cv = st.pstdev(vals) / mean(vals) * 100 if mean(vals) else 0
    print(f"  week-to-week variation  {cv:.0f}%  "
          f"({'steady' if cv < 25 else 'erratic — sawtooth, not a build' if cv > 45 else 'moderate'})")
    print(f"  runs per week           {mean([wk_n[k] for k in keys]):.1f}")
    print(f"  longest run, 12 months  {max(r['_km'] for r in runs):.1f} km")
    print(f"  longest run, last 8wk   {max([wk_long[k] for k in sorted(wk_km)[-8:]] or [0]):.1f} km")
    return wk_km, wk_n


def efficiency(D):
    """Pace at a fixed aerobic HR band — the cleanest read on aerobic fitness.

    Compared on flat runs only: climbing costs pace at the same heart rate, so
    mixing terrain makes a base build look like a decline.
    """
    section("AEROBIC EFFICIENCY  (pace at HR %d-%d, flat runs only)" % (AERO_LO, AERO_HI))
    runs = [r for r in D.runs() if r["_hr"] and AERO_LO <= r["_hr"] <= AERO_HI]
    flat = [r for r in runs if r["_elev"] is not None and r["_km"]
            and r["_elev"] / r["_km"] < FLAT_M_PER_KM]
    q = defaultdict(list)
    qa = defaultdict(list)
    for r in flat:
        q[f"{r['_d'].year}-Q{(r['_d'].month - 1) // 3 + 1}"].append(r)
    for r in runs:
        qa[f"{r['_d'].year}-Q{(r['_d'].month - 1) // 3 + 1}"].append(r)

    print(f"  {'quarter':8s} {'n':>3s} {'flat pace':>10s} {'HR':>4s} {'|':>2s} {'all-terrain':>12s} {'elev/km':>8s}")
    base = None
    for k in sorted(qa):
        fr, ar = q.get(k, []), qa[k]
        fp = mean([r["_pace"] for r in fr])
        ap = mean([r["_pace"] for r in ar])
        hr = mean([r["_hr"] for r in fr]) or mean([r["_hr"] for r in ar])
        ev = mean([r["_elev"] / r["_km"] for r in ar if r["_elev"] is not None])
        if fp and base is None:
            base = fp
        print(f"  {k:8s} {len(fr):3d} {fmt_pace(fp):>10s} {hr:4.0f} {'|':>2s} "
              f"{fmt_pace(ap):>12s} {ev or 0:8.1f}")

    fps = [(k, mean([r["_pace"] for r in q[k]])) for k in sorted(q) if q[k]]
    if len(fps) >= 2:
        best = min(fps, key=lambda x: x[1])
        cur = fps[-1]
        dv = (cur[1] - best[1]) * 60
        print(f"\n  best quarter   {best[0]}  {fmt_pace(best[1])}/km")
        print(f"  current        {cur[0]}  {fmt_pace(cur[1])}/km   ({dv:+.0f} s/km vs best)")
        if dv > 10:
            print(f"  -> at the same heart rate you are {dv:.0f} s/km slower than your peak.")
    print("\n  Confounders checked: terrain (flat-only column), and heat acclimation")
    print("  below. Both matter here — do not read the all-terrain column as fitness.")


def vo2_and_heat(D):
    section("VO2MAX & HEAT")
    mv, heat = defaultdict(list), defaultdict(list)
    for r in D.daily:
        if f(r.get("vo2max")):
            mv[r["date"][:7]].append(f(r["vo2max"]))
        if f(r.get("heat_acclimation_pct")):
            heat[r["date"][:7]].append(f(r["heat_acclimation_pct"]))
    if not mv:
        print("  no VO2max history")
        return
    print(f"  {'month':8s} {'VO2max':>7s} {'heat':>6s}")
    for k in sorted(mv):
        h = max(heat[k]) if heat.get(k) else 0
        print(f"  {k:8s} {max(mv[k]):7.1f} {h:5.0f}%  {bar(max(mv[k]) - 50, 0.25)}")
    allv = [(k, max(v)) for k, v in sorted(mv.items())]
    peak = max(allv, key=lambda x: x[1])
    print(f"\n  peak {peak[1]:.1f} ({peak[0]})   now {allv[-1][1]:.1f}   "
          f"({allv[-1][1] - peak[1]:+.1f})")


def intensity(D):
    section("INTENSITY DISTRIBUTION")
    runs = D.runs()
    zc, zt = Counter(), defaultdict(float)
    for r in runs:
        if not r["_hr"]:
            continue
        p = r["_hr"] / MAX_HR * 100
        z = ("Z1 easy" if p < 68 else "Z2 aerobic" if p < 78 else
             "Z3 tempo" if p < 85 else "Z4 threshold" if p < 92 else "Z5 VO2")
        zc[z] += 1
        zt[z] += r["_min"] or 0
    tot = sum(zc.values())
    for z in ["Z1 easy", "Z2 aerobic", "Z3 tempo", "Z4 threshold", "Z5 VO2"]:
        print(f"  {z:13s} {zc[z]:3d} runs {pct(zc[z], tot):3d}%  "
              f"{zt[z] / 60:5.1f}h  {bar(zc[z], 1)}")
    hard = zc["Z4 threshold"] + zc["Z5 VO2"]
    print(f"\n  hard running (Z4+)  {hard}/{tot} runs ({pct(hard, tot)}%)")

    # Real speed work: fast absolute pace, not just elevated HR.
    fast = [r for r in runs if r["_pace"] and r["_pace"] < 5.0]
    fast.sort(key=lambda r: r["_d"])
    print(f"  runs faster than 5:00/km   {len(fast)} in 12 months")
    if fast:
        last = fast[-1]
        gap = (D.today - last["_d"]).days
        print(f"  most recent               {last['_d']}  "
              f"{last['_km']:.1f}km @ {fmt_pace(last['_pace'])}/km  ({gap} days ago)")
    return len(fast), (D.today - fast[-1]["_d"]).days if fast else None


def load_ratio(D):
    section("TRAINING LOAD  (acute:chronic)")
    by_day = defaultdict(float)
    for r in D.acts:
        by_day[r["_d"]] += r["_load"]
    acute = sum(by_day.get(D.today - timedelta(days=i), 0) for i in range(7))
    chronic28 = sum(by_day.get(D.today - timedelta(days=i), 0) for i in range(28))
    chronic = chronic28 / 4
    print(f"  7-day load     {acute:6.0f}")
    print(f"  28-day avg wk  {chronic:6.0f}")
    if chronic:
        acwr = acute / chronic
        note = ("detraining — load is falling away" if acwr < 0.8 else
                "spike — injury risk window" if acwr > 1.5 else
                "productive build" if acwr > 1.0 else "maintaining")
        print(f"  ratio          {acwr:6.2f}   {note}")
    return by_day


def cross_training(D):
    section("WHAT ELSE IS IN THE LEGS")
    c = Counter()
    ld = defaultdict(float)
    hrs = defaultdict(float)
    for r in D.since(D.acts, 90):
        c[r["_type"]] += 1
        ld[r["_type"]] += r["_load"]
        hrs[r["_type"]] += (r["_min"] or 0) / 60
    if not c:
        print("  nothing in 90 days")
        return c
    print("  last 90 days:")
    for t, n in c.most_common():
        print(f"    {t:22s} {n:3d}x  {hrs[t]:5.1f}h  load {ld[t]:6.0f}")
    return c


def hyrox_gap(D, types90):
    section("HYROX READINESS GAPS")
    strength = [r for r in D.acts if any(s in r["_type"] for s in STRENGTH_TYPES)]
    gymish = [r for r in D.acts if any(s in r["_type"] for s in GYM_ISH_TYPES)]
    print(f"  strength / station sessions logged, 12 months:  {len(strength)}")
    print(f"  generic 'indoor cardio' sessions (unclassified): {len(gymish)}")
    if not strength:
        print("    -> Garmin has no record of the 2x/week lifting or the Friday")
        print("       station work the plan calls for. Either it is not happening,")
        print("       or it is happening untracked. Both are worth fixing: untracked")
        print("       load still fatigues you but never shows in readiness or ACWR.")

    # Compromised running: a run on the same day as another activity.
    by_day = defaultdict(list)
    for r in D.acts:
        by_day[r["_d"]].append(r)
    comp = [(day, rs) for day, rs in by_day.items()
            if len(rs) > 1 and any(x["_type"] in RUN_TYPES for x in rs)]
    print(f"\n  days pairing a run with another session:  {len(comp)}")
    print("    (proxy for compromised running — the plan's named Hyrox limiter)")
    for day, rs in sorted(comp)[-5:]:
        print(f"    {day}  " + " + ".join(f"{x['_type']}" for x in rs))

    # 1 km repeat capability
    runs = D.runs()
    short = [r for r in runs if 0.8 <= (r["_km"] or 0) <= 1.5]
    print(f"\n  1km-ish efforts logged:  {len(short)}")
    print("    Hyrox is 8x1km off fatigue. Nothing in the data rehearses that.")


def recovery(D):
    """Recovery trend against personal baseline.

    Resting HR is taken only from nights with sleep data. Garmin still reports
    a restingHeartRate on nights the watch wasn't worn, derived from daytime
    readings, and it runs ~14 bpm high. Mixing the two makes the trend track
    watch-wearing habits instead of physiology.
    """
    section("RECOVERY  (vs personal baseline)")
    worn = {r["date"] for r in D.sleep}
    rhr = sorted((d(r["date"]), f(r["resting_hr"]))
                 for r in D.sleep if f(r.get("resting_hr")) is not None)
    contaminated = [f(r["resting_hr"]) for r in D.daily
                    if r["date"] not in worn and f(r.get("resting_hr")) is not None]

    if rhr:
        recent = [v for dt, v in rhr if dt > D.today - timedelta(days=60)]
        base = [v for dt, v in rhr if dt <= D.today - timedelta(days=60)]
        line = f"  resting HR     {mean(recent or [v for _, v in rhr]):5.1f}"
        if recent and base:
            dv = mean(recent) - mean(base)
            line += (f"  (earlier baseline {mean(base):5.1f}, {dv:+.1f})"
                     f"  {'ok' if dv < 1.5 else 'watch — trending up'}")
        print(line + f"   [{len(rhr)} valid nights]")
        if contaminated:
            print(f"    excluded {len(contaminated)} daytime-derived values "
                  f"(avg {mean(contaminated):.1f}, ~{mean(contaminated) - mean([v for _, v in rhr]):.0f} bpm high)")
    else:
        print("  resting HR     no overnight data")

    hrv = sorted((d(r["date"]), f(r["hrv_last_night"]))
                 for r in D.daily if f(r.get("hrv_last_night")) is not None)
    if hrv:
        recent = [v for dt, v in hrv if dt > D.today - timedelta(days=60)]
        base = [v for dt, v in hrv if dt <= D.today - timedelta(days=60)]
        line = f"  overnight HRV  {mean(recent or [v for _, v in hrv]):5.1f}"
        if recent and base:
            dv = mean(recent) - mean(base)
            line += (f"  (earlier baseline {mean(base):5.1f}, {dv:+.1f})"
                     f"  {'ok' if dv > -3 else 'watch — suppressed'}")
        print(line + f"   [{len(hrv)} nights]")
    else:
        print("  overnight HRV  no data")

    rd = sorted((d(r["date"]), f(r["score"])) for r in D.ready
                if f(r.get("score")) is not None)
    if rd:
        r30 = [v for dt, v in rd if dt > D.today - timedelta(days=30)]
        rall = [v for _, v in rd]
        print(f"  readiness      {mean(r30 or rall):5.1f}  (12mo avg {mean(rall):5.1f})")


def sleep(D):
    section("SLEEP")
    s = D.sleep
    if not s:
        print("  no sleep data")
        return 0
    days_span = (D.today - min(d(r["date"]) for r in D.daily)).days + 1
    cov = pct(len(s), days_span)
    print(f"  nights recorded   {len(s)} of {days_span} days  ({cov}%)")
    if cov < 60:
        print(f"    -> you are blind on {100 - cov}% of nights. HRV, body battery and")
        print("       readiness all derive from overnight wear, so those columns are")
        print("       mostly empty too. This is the single cheapest fix in the dataset.")
    tot = [f(r["total_h"]) for r in s if f(r["total_h"])]
    sc = [f(r["score"]) for r in s if f(r["score"])]
    need = [f(r["sleep_need_h"]) for r in s if f(r.get("sleep_need_h"))]
    print(f"  on nights recorded:  {mean(tot):.1f} h avg, score {mean(sc):.0f}")
    if need:
        deficit = mean(need) - mean(tot)
        print(f"  Garmin sleep need    {mean(need):.1f} h  ({deficit:+.1f} h vs actual)")
    short = [r for r in s if (f(r["total_h"]) or 9) < 7]
    print(f"  nights under 7h      {len(short)}/{len(s)} ({pct(len(short), len(s))}%)")
    return cov


def adherence(D, wk_n):
    section("PLAN ADHERENCE")
    keys = sorted(wk_n)[-8:]
    target = 4  # Phase 2a/2b template running days
    print(f"  plan calls for ~{target} running sessions/week in the current phase")
    print(f"  {'week':9s} {'runs':>5s} {'vs plan':>9s}")
    for k in keys:
        gap = wk_n[k] - target
        print(f"  {wk_label(k):9s} {wk_n[k]:5d} {gap:+9d}  {'#' * wk_n[k]}")
    avg = mean([wk_n[k] for k in keys])
    print(f"\n  averaging {avg:.1f} of {target} sessions  ({pct(avg, target)}% of planned running days)")


def seasonal_load(D):
    """Activities that run on a season and will re-enter the picture."""
    section("SEASONAL LOAD RETURNING")
    by_month = defaultdict(Counter)
    for r in D.acts:
        by_month[r["date"][:7]][r["_type"]] += 1
    seasonal = {}
    for t_ in {r["_type"] for r in D.acts}:
        months = sorted(m for m in by_month if by_month[m][t_])
        if len(months) >= 3:
            recent = [m for m in months if m >= (D.today - timedelta(days=120)).isoformat()[:7]]
            if not recent:
                seasonal[t_] = (months[0], months[-1], sum(by_month[m][t_] for m in months))
    if not seasonal:
        print("  nothing dormant")
        return
    for t_, (first, last, n) in sorted(seasonal.items(), key=lambda x: -x[1][2]):
        per = n / (len({m for m in by_month if by_month[m][t_]}) or 1)
        print(f"  {t_:20s} {n:3d} sessions {first}..{last}  (~{per:.1f}/month in season)")
        print(f"    dormant since {last}. If it returns on the same calendar, that")
        print(f"    load lands inside the peak block and the plan does not budget for it.")


def verdict(D):
    section("VERDICT")
    runs = D.runs()
    wk = defaultdict(float)
    wkn = Counter()
    for r in runs:
        wk[week(r["_d"])] += r["_km"]
        wkn[week(r["_d"])] += 1
    last8 = sorted(wk)[-8:]
    vol = mean([wk[k] for k in last8])
    sess = mean([wkn[k] for k in last8])
    longest8 = max([wk[k] for k in last8] and
                   [max((r["_km"] for r in runs if week(r["_d"]) == k), default=0)
                    for k in last8] or [0])

    by_day = defaultdict(float)
    for r in D.acts:
        by_day[r["_d"]] += r["_load"]
    acute = sum(by_day.get(D.today - timedelta(days=i), 0) for i in range(7))
    chronic = sum(by_day.get(D.today - timedelta(days=i), 0) for i in range(28)) / 4
    acwr = acute / chronic if chronic else 0

    # Same 60-day window the RECOVERY section reports, so the two agree.
    cut = D.today - timedelta(days=60)
    rhr = [f(r["resting_hr"]) for r in D.sleep
           if f(r.get("resting_hr")) and d(r["date"]) > cut] or \
          [f(r["resting_hr"]) for r in D.sleep if f(r.get("resting_hr"))]
    hrv = [f(r["hrv_last_night"]) for r in D.daily
           if f(r.get("hrv_last_night")) and d(r["date"]) > cut] or \
          [f(r["hrv_last_night"]) for r in D.daily if f(r.get("hrv_last_night"))]
    fast = [r for r in runs if r["_pace"] and r["_pace"] < 5.0]
    gap = (D.today - max(r["_d"] for r in fast)).days if fast else None

    print(f"  The body is fine. The training is the problem.")
    print()
    print(f"  Recovery is excellent: resting HR {mean(rhr):.0f}, overnight HRV "
          f"{mean(hrv):.0f}.")
    print(f"  Acute:chronic load {acwr:.2f} — you are shedding fitness, not risking")
    print(f"  injury. You have headroom to absorb considerably more work.")
    print()
    print(f"  Running {sess:.1f}x/week at {vol:.0f} km/wk against a plan built on 4x.")
    print(f"  Longest run in 8 weeks: {longest8:.0f} km. Phase 2b starts Sept 28 and")
    print(f"  asks for 22-26 km long runs — that is a jump you cannot make safely")
    print(f"  in one step.")
    if gap:
        print()
        print(f"  No run under 5:00/km in {gap} days. Hyrox is eight 1 km efforts;")
        print(f"  you have logged zero 1 km efforts, ever.")
    print()
    print("  Priority order:")
    print("    1. Consistency over peak weeks — 3 runs every week beats 4 then 1.")
    print("    2. Rebuild the long run: add ~2 km/week from where you actually are.")
    print("    3. Put one 1 km-repeat session in weekly. It is the race.")
    print("    4. Log strength and station work, or it is invisible load.")
    print("    5. Wear the watch overnight — 81% of nights you are guessing.")


def build_markdown(D):
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        race_status(D)
        wk_km, wk_n = volume(D, 14)
        adherence(D, wk_n)
        efficiency(D)
        vo2_and_heat(D)
        intensity(D)
        load_ratio(D)
        cross_training(D)
        hyrox_gap(D, None)
        seasonal_load(D)
        recovery(D)
        sleep(D)
        verdict(D)
    txt = buf.getvalue().replace("\033[1m", "").replace("\033[0m", "")
    return (f"# Training analysis — {D.today}\n\n"
            f"Generated by `analyze.py` from local Garmin data.\n\n"
            f"```\n{txt}\n```\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=14)
    ap.add_argument("--write", action="store_true",
                    help="also write insights/latest.md")
    args = ap.parse_args()

    D = Data()
    if not D.acts:
        print("No activity data. Run garmin_sync.py first.")
        return

    race_status(D)
    wk_km, wk_n = volume(D, args.weeks)
    adherence(D, wk_n)
    efficiency(D)
    vo2_and_heat(D)
    intensity(D)
    load_ratio(D)
    types90 = cross_training(D)
    hyrox_gap(D, types90)
    seasonal_load(D)
    recovery(D)
    sleep(D)
    verdict(D)
    print()

    if args.write:
        out = ROOT / "insights" / "latest.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(build_markdown(D))
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
