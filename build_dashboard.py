#!/usr/bin/env python3
"""Regenerate dashboard.html from the current CSVs.

    ./.venv/bin/python build_dashboard.py

Everything the page renders is computed here and injected as one JSON blob, so
the published dashboard is never hand-edited — sync, rebuild, republish.
"""

import argparse
import json
import re
import statistics as st
import sys
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import analyze as A
import week_plan as WP


def build_health(D, out):
    """Body composition from the DEXA scan, read against the watch data.

    The scan is a single deep snapshot; Garmin is shallow but continuous. The
    value is in the join: the scan supplies a measured RMR and fat-free mass,
    Garmin supplies what is actually burned and slept each day, and only
    together do they produce a calorie target or an energy-availability floor
    that means anything for this athlete.
    """
    if not D.dexa:
        return None
    s = D.dexa[-1]
    prev = D.dexa[-2] if len(D.dexa) > 1 else None
    h = {"date": s["date"], "provider": s.get("provider") or "",
         "n_scans": len(D.dexa)}
    lean, fat, bmc = s["lean_g"], s["fat_g"], s["bmc_g"]
    ffm = lean + bmc
    h["comp"] = {
        "weight": s["weight_kg"], "fat_kg": round(fat / 1000, 2),
        "lean_kg": round(lean / 1000, 2), "bmc_kg": round(bmc / 1000, 2),
        "ffm_kg": round(ffm / 1000, 2),
        "fat_pct": s["fat_pct_tissue"], "fat_pct_total": s["fat_pct_total"],
        "percentile": s.get("fat_percentile"),
    }

    # --- where the fat percentage sits on the ACE scale the report prints
    fp = s["fat_pct_tissue"]
    band = next((b[0] for b in A.ACE_BANDS_M if b[1] <= fp <= b[2]), "—")
    h["ace"] = [{"name": b[0], "lo": b[1], "hi": b[2], "on": b[0] == band}
                for b in A.ACE_BANDS_M]
    h["ace_band"] = band

    # --- visceral fat against the male reference table
    pc, ageband = A.vat_percentile(s["vat_g"], s["age_years"])
    vband = next((b[0] for b in A.VAT_BANDS
                  if b[1] <= s["vat_g"] < b[2]), A.VAT_BANDS[-1][0])
    ref = A.load_vat_reference().get(ageband, {})
    h["vat"] = {"g": s["vat_g"], "cm3": s.get("vat_cm3"),
                "sat_g": s.get("sat_g"), "pc": pc, "ageband": ageband,
                "band": vband,
                "risk_floor": A.VAT_BANDS[0][2],
                "curve": [{"pc": k, "g": ref[k]} for k in sorted(ref)],
                "ag": s.get("ag_ratio")}

    # --- bone. A runner adding a marathon block cares about exactly one number.
    h["bone"] = {"bmd": s.get("bmd_total"), "t": s.get("bmd_tscore"),
                 "z": s.get("bmd_zscore"), "spine": s.get("bmd_spine"),
                 "pelvis": s.get("bmd_pelvis"), "ribs": s.get("bmd_ribs"),
                 "bmc_kg": round(bmc / 1000, 2)}

    # --- lean mass by segment. Hyrox loads the upper body far harder than a
    # running-only program ever does, so the arms:legs split is a real read.
    seg = [("Arms", s["arms_lean_g"], s["arms_fat_g"]),
           ("Legs", s["legs_lean_g"], s["legs_fat_g"]),
           ("Trunk", s["trunk_lean_g"], s["trunk_fat_g"])]
    h["segments"] = [{"name": n, "lean": l, "fat": ft,
                      "lean_share": round(100 * l / lean, 1),
                      "fat_pct": round(100 * ft / (l + ft), 1)} for n, l, ft in seg]
    h["rsmi"] = {"v": s["rsmi"], "cut": A.SARCOPENIA_RSMI_M,
                 "arms_legs_ratio": round(s["arms_lean_g"] / s["legs_lean_g"], 3)}

    # --- left/right balance. Under about 10% is noise, not an imbalance.
    def bal(name, r, l):
        d_ = r - l
        return {"name": name, "r": r, "l": l, "d": d_,
                "pct": round(100 * abs(d_) / max(r, l), 1)}
    h["balance"] = [bal("Arms", s["arm_lean_r_g"], s["arm_lean_l_g"]),
                    bal("Legs", s["leg_lean_r_g"], s["leg_lean_l_g"]),
                    bal("Trunk", s["trunk_lean_r_g"], s["trunk_lean_l_g"])]

    # --- the join: a measured RMR and a measured fat-free mass on one side,
    # what the watch says is actually burned on the other. Neither number alone
    # produces a calorie target; together they produce a starting point and,
    # more usefully, a floor.
    rmr = s["rmr_cal"]
    recent = [r for r in D.daily
              if A.d(r["date"]) > D.today - timedelta(days=90)]
    act = A.mean([A.f(r.get("active_calories")) for r in recent]) or 0
    # Energy availability is defined against EXERCISE energy expenditure, not
    # against everything Garmin counts as active. Splitting the two matters:
    # using the whole active figure understates availability badly.
    span = 90
    cutd = D.today - timedelta(days=span)
    tcal = defaultdict(float)
    for r in D.acts:
        if r["_d"] > cutd and A.f(r["calories"]):
            tcal[r["_d"]] += A.f(r["calories"])
    per_day = [tcal.get(cutd + timedelta(days=i + 1), 0.0) for i in range(span)]
    sess = [v for v in per_day if v > 0]
    eee_all = A.mean(per_day) or 0
    eee_sess = A.mean(sess) or 0
    neat = max(0.0, act - eee_all)          # steps, stairs, everything untracked

    ffm_kg = ffm / 1000
    maint = round((rmr + act) / (1 - A.TEF))
    # Two other standard estimates, to show the spread rather than assert one
    # number as fact. Katch-McArdle is FFM-based like the scan's; Mifflin uses
    # body weight and runs higher on a lean athlete.
    km = 370 + 21.6 * ffm_kg
    mifflin = (10 * s["weight_kg"] + 6.25 * s["height_cm"]
               - 5 * s["age_years"] + 5)
    maint_hi = round((max(km, mifflin) + act) / (1 - A.TEF))
    rest_maint = round((rmr + neat) / (1 - A.TEF))
    sess_maint = round((rmr + neat + eee_sess) / (1 - A.TEF))

    h["energy"] = {
        "rmr": rmr, "rmr_km": round(km), "rmr_mifflin": round(mifflin),
        "active": round(act), "neat": round(neat),
        "eee_sess": round(eee_sess), "sess_days": len(sess), "span": span,
        "maint": maint, "maint_hi": maint_hi,
        "rest_maint": rest_maint, "sess_maint": sess_maint,
        "ffm": round(ffm_kg, 1),
        # Floors, not targets: the intake below which a day stops being a
        # deficit and starts being a problem.
        "floor_rest": round(A.EA_FLOOR * ffm_kg),
        "floor_sess": round(A.EA_FLOOR * ffm_kg + eee_sess),
        "ea_floor": A.EA_FLOOR,
        # Cycled: the deficit sits on rest days, session days stay fed.
        "eat_rest": round((A.EA_FLOOR * ffm_kg) / 25) * 25,
        "eat_sess": round((A.EA_FLOOR * ffm_kg + eee_sess) / 25) * 25,
        "protein_lo": round(1.8 * s["weight_kg"]),
        "protein_hi": round(2.2 * s["weight_kg"]),
        "act_n": len(recent),
    }
    # A week, priced. Session days are fed to the availability floor and rest
    # days carry the deficit -- so the weekly deficit is set by the floors, not
    # by willpower, and it is much smaller than a generic "500 a day" would be.
    def week_at(n_sess):
        n_rest = 7 - n_sess
        intake = n_rest * h["energy"]["eat_rest"] + n_sess * h["energy"]["eat_sess"]
        burn = n_rest * rest_maint + n_sess * sess_maint
        return {"n": n_sess, "intake": intake, "maint": burn,
                "deficit": burn - intake,
                "kg_wk": round((burn - intake) / 7700, 3)}

    cur_sess = round(len(sess) / (span / 7), 1)
    plan_sess = 5      # what Phase 2b actually asks for
    now = week_at(plan_sess)
    h["energy"]["sess_per_week"] = cur_sess
    h["energy"]["plan_sess"] = plan_sess
    h["energy"]["wk_intake"] = now["intake"]
    h["energy"]["wk_maint"] = now["maint"]
    h["energy"]["wk_deficit"] = now["deficit"]
    h["energy"]["kg_per_week"] = now["kg_wk"]

    # --- fat-loss targets, all computed at constant fat-free mass. Losing lean
    # mass would cost sled push and pull time directly, so it is never a target.
    def at(pct):
        f_ = pct / 100 * lean / (1 - pct / 100)      # tissue %fat definition
        return {"pct": pct, "fat_kg": round(f_ / 1000, 1),
                "drop_kg": round((fat - f_) / 1000, 1),
                "weight": round((f_ + ffm) / 1000, 1)}

    h["targets"] = [at(p) for p in (17, 16, 15, 14, 13)]

    left = (A.HYROX - D.today).days
    # The cut has to finish before the race, not on it -- the last four weeks
    # are taper and carb loading, which do not happen in a deficit.
    cut_weeks = max(1, (left - 28) // 7)
    # Mark each target with the weeks it needs at the EA-safe rate, and pick
    # the leanest one that actually fits before the taper. Asserting 13% as a
    # goal when the energy budget cannot deliver it would be a wish, not a plan.
    rate_wk = h["energy"]["kg_per_week"]
    for t in h["targets"]:
        t["weeks"] = round(t["drop_kg"] / rate_wk) if rate_wk > 0 else None
        t["reachable"] = t["weeks"] is not None and t["weeks"] <= cut_weeks
    h["target"] = next((t for t in reversed(h["targets"]) if t["reachable"]),
                       h["targets"][0])
    h["target"]["is_target"] = True
    rate = round(h["target"]["drop_kg"] / cut_weeks, 2)
    # Where the EA-safe rate actually gets you by race day.
    landed_fat = fat - h["energy"]["kg_per_week"] * cut_weeks * 1000
    h["plan"] = {
        "days_left": left, "cut_weeks": cut_weeks, "rate": rate,
        "cut_end": (A.HYROX - timedelta(days=28)).isoformat(),
        "achievable": h["energy"]["kg_per_week"] * cut_weeks >= h["target"]["drop_kg"],
        "weeks_needed": (round(h["target"]["drop_kg"] / h["energy"]["kg_per_week"])
                         if h["energy"]["kg_per_week"] > 0 else None),
        "landed_pct": round(100 * landed_fat / (landed_fat + lean), 1),
        "landed_kg": round((landed_fat + ffm) / 1000, 1),
    }

    # --- weight history, with the scan marked
    h["weights"] = [{"d": r["date"], "kg": A.f(r["weight_kg"]),
                     "src": r.get("source") or ""}
                    for r in D.weight if A.f(r.get("weight_kg"))]

    # --- the Garmin metrics that speak to health rather than to performance
    def series(rows, col, days):
        cut = D.today - timedelta(days=days)
        return [(r["date"], A.f(r[col])) for r in rows
                if A.d(r["date"]) > cut and A.f(r.get(col)) is not None]

    def avg(rows, col, days):
        v = [x[1] for x in series(rows, col, days)]
        return round(A.mean(v), 1) if v else None

    wk_int = defaultdict(float)
    for r in D.daily:
        wk_int[A.week(A.d(r["date"]))] += ((A.f(r.get("intensity_min_moderate")) or 0)
                                           + 2 * (A.f(r.get("intensity_min_vigorous")) or 0))
    ik = sorted(wk_int)[-13:-1]
    tot_sleep = [A.f(r["total_h"]) for r in D.sleep if A.f(r["total_h"])]
    need = [A.f(r["sleep_need_h"]) for r in D.sleep if A.f(r.get("sleep_need_h"))]
    deep = [A.f(r["deep_h"]) for r in D.sleep if A.f(r.get("deep_h"))]
    rem = [A.f(r["rem_h"]) for r in D.sleep if A.f(r.get("rem_h"))]
    resp = [A.f(r["avg_respiration"]) for r in D.sleep if A.f(r.get("avg_respiration"))]
    # daily.csv is written newest-first, so sort before taking "latest".
    endur = sorted(((r["date"], A.f(r["endurance_score"])) for r in D.daily
                    if A.f(r.get("endurance_score"))), key=lambda x: x[0])
    # Resting HR is only real on nights the watch was worn; the rest is derived
    # from daytime readings and runs high. Same correction the overview uses.
    rhr = [A.f(r["resting_hr"]) for r in D.sleep
           if A.f(r.get("resting_hr")) and A.d(r["date"]) > D.today - timedelta(days=180)]

    sleep_mean = A.mean(tot_sleep)
    h["vitals"] = {
        "rhr": round(A.mean(rhr), 1) if rhr else None,
        "resp": round(A.mean(resp), 1) if resp else None,
        "stress": avg(D.daily, "stress_avg", 90),
        "bb_high": avg(D.daily, "body_battery_high", 90),
        "bb_low": avg(D.daily, "body_battery_low", 90),
        "steps": round(avg(D.daily, "steps", 90) or 0),
        "floors": avg(D.daily, "floors_climbed", 90),
        "sleep": round(sleep_mean, 2) if sleep_mean else None,
        "sleep_need": round(A.mean(need), 2) if need else None,
        "sleep_debt_wk": (round((A.mean(need) - sleep_mean) * 7, 1)
                          if need and sleep_mean else None),
        "deep_pct": round(100 * A.mean(deep) / sleep_mean) if deep and sleep_mean else None,
        "rem_pct": round(100 * A.mean(rem) / sleep_mean) if rem and sleep_mean else None,
        "sleep_n": len(D.sleep),
        "int_wk": round(A.mean([wk_int[k] for k in ik])) if ik else None,
        "int_target": A.WHO_INTENSITY_MIN,
        "int_weeks_ok": sum(1 for k in ik if wk_int[k] >= A.WHO_INTENSITY_MIN),
        "int_weeks": len(ik),
        "endurance": round(endur[-1][1]) if endur else None,
        "endurance_peak": round(max(x[1] for x in endur)) if endur else None,
        # The latest value, not the twelve-month peak -- the peak is a
        # different claim and the overview already makes it.
        "vo2": next((A.f(r["vo2max"]) for r in sorted(
            D.daily, key=lambda x: x["date"], reverse=True)
            if A.f(r.get("vo2max"))), None),
    }
    h["trends"] = {
        "stress": series(D.daily, "stress_avg", 180),
        "bb_low": series(D.daily, "body_battery_low", 180),
        "sleep": [(r["date"], A.f(r["total_h"])) for r in D.sleep if A.f(r["total_h"])],
        "endurance": endur[-180:],
    }

    # --- findings, conditional on the numbers rather than written in
    fnd = []
    tgt = h["target"]
    fnd.append({
        "sev": "warn", "num": f"{tgt['drop_kg']:.1f}",
        "title": "kg of fat between you and race weight",
        "body": f"At {fp}% tissue fat you are in the ACE “{band}” band, one "
                f"step below “Fitness” and two below the 6–13% athlete range. "
                f"Dropping to {tgt['pct']}% at unchanged lean mass means "
                f"{tgt['weight']} kg on race day. Every one of those kilos is "
                "carried through eight 1 km runs, 80 m of burpee broad jumps and "
                "100 m of lunges."})
    if h["vitals"]["sleep_debt_wk"] and h["vitals"]["sleep_debt_wk"] > 3:
        fnd.append({
            "sev": "warn", "num": f"{h['vitals']['sleep_debt_wk']:.1f}",
            "title": "hours short of your sleep need, every week",
            "body": f"{h['vitals']['sleep']} h a night against a "
                    f"{h['vitals']['sleep_need']} h need. In a deficit that gap "
                    "stops being a recovery problem and becomes a body "
                    "composition one — short sleep shifts weight loss away "
                    "from fat and toward lean mass."})
    e = h["energy"]
    if e["floor_sess"] >= e["maint"]:
        fnd.append({
            "sev": "crit", "num": f"{e['floor_sess']:,}",
            "title": "kcal you must eat on a session day, above maintenance",
            "body": f"Your measured fat-free mass is {e['ffm']} kg and a typical "
                    f"session burns {e['eee_sess']:,} kcal, so staying above the "
                    f"{A.EA_FLOOR} kcal/kg energy-availability floor on a training "
                    f"day costs more than your {e['maint']:,} kcal maintenance. "
                    "A flat daily deficit is therefore not available to you. The "
                    "deficit has to live on rest days; session days get fed."})
    if h["rsmi"]["arms_legs_ratio"] < 0.42:
        fnd.append({
            "sev": "warn", "num": f"{s['arms_lean_g'] / 1000:.1f}",
            "title": "kg of arm lean mass against 20.3 in the legs",
            "body": "A runner's distribution. Hyrox is not a running race: ski "
                    "erg, row, sled pull, 200 m of farmers carry and 100 wall "
                    "balls all run through the upper body and grip. This is the "
                    "one place worth adding mass rather than losing it."})
    if not any(r["_type"] in A.STRENGTH_TYPES for r in D.acts):
        fnd.append({
            "sev": "crit", "num": "0",
            "title": "logged strength sessions to protect that lean mass",
            "body": "In a deficit, resistance training is what decides whether "
                    "the weight comes off as fat or as the 59.1 kg of lean mass "
                    "the sled push runs on. Without it roughly a quarter of any "
                    "loss is lean tissue."})
    h["findings"] = fnd

    # --- the good news, stated as plainly as the problems
    good = []
    if pc is not None and pc <= 20:
        good.append({
            "t": f"Visceral fat at the {pc}th percentile",
            "b": f"{s['vat_g']} g against a {h['vat']['risk_floor']} g low-risk "
                 f"ceiling and a {ref.get(50, 0):.0f} g median for men "
                 f"{ageband}. There is no metabolic risk here to train away, and "
                 "no reason to chase abdominal fat for health rather than "
                 "for performance."})
    if s.get("bmd_tscore") and s["bmd_tscore"] >= 1:
        good.append({
            "t": f"Bone density T-score +{s['bmd_tscore']}",
            "b": f"{s['bmd_total']} g/cm², {s['bmd_tscore']} standard deviations "
                 "above the young-adult average. Practically: stress-fracture "
                 "risk is low, so the long run can be rebuilt at the top of the "
                 "usual 10%-a-week guidance rather than the bottom."})
    if s["rsmi"] >= A.SARCOPENIA_RSMI_M * 1.15:
        good.append({
            "t": f"Skeletal muscle index {s['rsmi']}",
            "b": f"Against a {A.SARCOPENIA_RSMI_M} sarcopenia cutoff. The muscle "
                 "to race on is already there — the job is keeping it while "
                 "the fat comes off, not building it from nothing."})
    bal_max = max(h["balance"], key=lambda b: b["pct"])
    if bal_max["pct"] < 10:
        good.append({
            "t": f"Left-right balance within {bal_max['pct']}%",
            "b": "Nothing asymmetric enough to matter, which is worth knowing "
                 "before 100 m of single-leg sandbag lunges. Re-scan after the "
                 "build to check it held."})
    h["good"] = good

    if prev:
        h["delta"] = {k: round(s[k] - prev[k], 2) for k in
                      ("weight_kg", "fat_pct_tissue", "fat_g", "lean_g", "vat_g")
                      if s.get(k) is not None and prev.get(k) is not None}
    return h


def build_data():
    D = A.Data()
    runs = D.runs()
    out = {}

    wk_km, wk_n, wk_long = defaultdict(float), Counter(), defaultdict(float)
    for r in runs:
        k = A.week(r["_d"])
        wk_km[k] += r["_km"]
        wk_n[k] += 1
        wk_long[k] = max(wk_long[k], r["_km"])
    keys = sorted(wk_km)[-16:]
    out["weekly"] = [{"w": A.wk_label(k), "km": round(wk_km[k], 1),
                      "n": wk_n[k], "long": round(wk_long[k], 1)} for k in keys]

    q, qa = defaultdict(list), defaultdict(list)
    for r in runs:
        if r["_hr"] and A.AERO_LO <= r["_hr"] <= A.AERO_HI:
            qk = f"{r['_d'].year}-Q{(r['_d'].month - 1) // 3 + 1}"
            qa[qk].append(r)
            if r["_elev"] is not None and r["_elev"] / r["_km"] < A.FLAT_M_PER_KM:
                q[qk].append(r)
    out["eff"] = [{"q": k,
                   "flat": round(A.mean([x["_pace"] for x in q[k]]), 2) if q.get(k) else None,
                   "all": round(A.mean([x["_pace"] for x in qa[k]]), 2),
                   "n": len(q.get(k, [])),
                   "elev": round(A.mean([x["_elev"] / x["_km"] for x in qa[k]
                                         if x["_elev"] is not None]) or 0, 1)}
                  for k in sorted(qa) if len(qa[k]) >= 3]

    mv, heat = defaultdict(list), defaultdict(list)
    for r in D.daily:
        if A.f(r.get("vo2max")):
            mv[r["date"][:7]].append(A.f(r["vo2max"]))
        if A.f(r.get("heat_acclimation_pct")):
            heat[r["date"][:7]].append(A.f(r["heat_acclimation_pct"]))
    out["vo2"] = [{"m": k, "v": max(mv[k]), "h": max(heat[k]) if heat.get(k) else 0}
                  for k in sorted(mv)]

    # Measured seconds in each zone, not runs bucketed by their average HR.
    # Averaging a whole run into one zone hides both ends of the session; on
    # this athlete it under-reported Z4 by half.
    secs, zex, zap = A.zone_seconds(D)
    floors = A.ZONE_FLOORS
    lbl = ["Easy", "Aerobic", "Tempo", "Threshold", "VO\u2082"]
    ztot = sum(secs.values()) or 1
    out["zones"] = [{"z": f"Z{i + 1}", "l": lbl[i],
                     "r": (f"{floors[i]}-{floors[i + 1] - 1} bpm"
                           if i + 1 < len(floors) else f"{floors[i]}+ bpm"),
                     "sec": round(secs[A.ZONE_NAMES[i]]),
                     "pct": round(100 * secs[A.ZONE_NAMES[i]] / ztot)}
                    for i in range(5)]
    out["zoneMeta"] = {"runs": zex, "approx": zap, "hours": round(ztot / 3600),
                       "easy": round(100 * (secs[A.ZONE_NAMES[0]]
                                            + secs[A.ZONE_NAMES[1]]) / ztot),
                       "mid": round(100 * secs[A.ZONE_NAMES[2]] / ztot),
                       "hard": round(100 * (secs[A.ZONE_NAMES[3]]
                                            + secs[A.ZONE_NAMES[4]]) / ztot)}

    out["longruns"] = [{"d": r["_d"].isoformat(), "km": round(r["_km"], 1),
                        "p": round(r["_pace"], 2)} for r in runs if r["_km"] >= 15]
    hk = Counter(r["date"][:7] for r in D.acts if r["_type"] == "ice_hockey")
    months = sorted({r["date"][:7] for r in D.daily})[-12:]
    out["hockey"] = [{"m": m, "n": hk.get(m, 0)} for m in months]

    # --- header + stat tiles, so a re-sync never leaves stale numbers on the page
    left = (A.HYROX - D.today).days
    by_day = defaultdict(float)
    for r in D.acts:
        by_day[r["_d"]] += r["_load"]
    acute = sum(by_day.get(D.today - timedelta(days=i), 0) for i in range(7))
    chronic = sum(by_day.get(D.today - timedelta(days=i), 0) for i in range(28)) / 4
    acwr = acute / chronic if chronic else 0
    curwk = A.week(D.today)
    complete = [k for k in sorted(wk_km) if k != curwk]
    last8 = complete[-8:]
    sess8 = A.mean([wk_n[k] for k in last8]) or 0
    # The ceiling should include this week — a long run done today still counts.
    long8 = max((wk_long[k] for k in sorted(wk_km)[-8:]), default=0)
    cut = D.today - timedelta(days=60)
    rhr = [A.f(r["resting_hr"]) for r in D.sleep
           if A.f(r.get("resting_hr")) and A.d(r["date"]) > cut] or \
          [A.f(r["resting_hr"]) for r in D.sleep if A.f(r.get("resting_hr"))]
    hrv = [A.f(r["hrv_last_night"]) for r in D.daily
           if A.f(r.get("hrv_last_night")) and A.d(r["date"]) > cut] or \
          [A.f(r["hrv_last_night"]) for r in D.daily if A.f(r.get("hrv_last_night"))]
    rdy = [A.f(r["score"]) for r in D.ready
           if A.f(r.get("score")) and A.d(r["date"]) > D.today - timedelta(days=30)]
    longs = [r for r in runs if r["_km"] >= 15]
    strength = [r for r in D.acts if any(x in r["_type"] for x in A.STRENGTH_TYPES)]
    gap_days = (D.today - max(r["_d"] for r in longs)).days if longs else None

    def band(v, good, warn, invert=False):
        x = (v <= good, v <= warn) if not invert else (v >= good, v >= warn)
        return "good" if x[0] else "warn" if x[1] else "crit"

    out["hdr"] = {
        "days": left, "weeks": left // 7, "race": A.HYROX.isoformat(),
        "through": D.today.isoformat(), "runs": len(runs),
        "km": round(sum(r["_km"] for r in runs)),
        "maxhr": A.MAX_HR, "lthr": A.LTHR,
    }
    out["stats"] = {
        "acwr": round(acwr, 2), "acute": round(acute), "chronic": round(chronic),
        "acwr_state": "crit" if acwr < 0.8 else "warn" if acwr > 1.5 else "good",
        "sess8": round(sess8, 1), "adherence": round(100 * sess8 / 4),
        "adh_state": band(round(100 * sess8 / 4), 85, 60, invert=True),
        "long8": round(long8, 1),
        "long_state": band(long8, 22, 15, invert=True),
        "rhr": round(A.mean(rhr), 1) if rhr else None,
        "hrv": round(A.mean(hrv)) if hrv else None,
        "rdy": round(A.mean(rdy)) if rdy else None,
        "gap_days": gap_days,
        "ceiling": round(long8, 1),
        "strength_n": len(strength),
    }

    # --- narrative figures, substituted into the prose at build time.
    # They live here rather than in the template so the committed template
    # carries no personal health values and stays reusable by anyone.
    fq = [(e["q"], e["flat"]) for e in out["eff"] if e["flat"] is not None]
    best_q, best_p = min(fq, key=lambda x: x[1]) if fq else ("", 0)
    cur_q, cur_p = fq[-1] if fq else ("", 0)
    raw_last = out["eff"][-1]["all"] if out["eff"] else 0
    vols = [w["km"] for w in out["weekly"]]
    cv = round(st.pstdev(vols) / A.mean(vols) * 100) if vols else 0
    fast = [r for r in runs if r["_pace"] and r["_pace"] < 5.0]
    kmish = [r for r in runs if 0.8 <= (r["_km"] or 0) <= 1.5]
    by_d = defaultdict(list)
    for r in D.acts:
        by_d[r["_d"]].append(r)
    comp = [k for k, v in by_d.items()
            if len(v) > 1 and any(x["_type"] in A.RUN_TYPES for x in v)]
    hk_in = [h["n"] for h in out["hockey"] if h["n"]]
    hk_all = Counter(r["date"][:7] for r in D.acts if r["_type"] == "ice_hockey")
    worn = {r["date"] for r in D.sleep}
    unworn = [A.f(r["resting_hr"]) for r in D.daily
              if r["date"] not in worn and A.f(r.get("resting_hr"))]
    tot = [A.f(r["total_h"]) for r in D.sleep if A.f(r["total_h"])]
    need = [A.f(r["sleep_need_h"]) for r in D.sleep if A.f(r.get("sleep_need_h"))]
    span = (D.today - min(A.d(r["date"]) for r in D.daily)).days + 1
    rhr_base = [A.f(r["resting_hr"]) for r in D.sleep
                if A.f(r.get("resting_hr")) and A.d(r["date"]) <= cut]
    hrv_base = [A.f(r["hrv_last_night"]) for r in D.daily
                if A.f(r.get("hrv_last_night")) and A.d(r["date"]) <= cut]
    rdy_all = [A.f(r["score"]) for r in D.ready if A.f(r.get("score"))]
    elev_by_q = {e["q"]: e["elev"] for e in out["eff"]}

    out["narr"] = {
        "rhr": f"{A.mean(rhr):.0f}" if rhr else "\u2014",
        "rhr_exact": f"{A.mean(rhr):.1f}" if rhr else "\u2014",
        "rhr_base": f"{A.mean(rhr_base):.1f}" if rhr_base else "\u2014",
        "hrv": f"{A.mean(hrv):.0f}" if hrv else "\u2014",
        "hrv_exact": f"{A.mean(hrv):.1f}" if hrv else "\u2014",
        "hrv_base": f"{A.mean(hrv_base):.1f}" if hrv_base else "\u2014",
        "rdy": f"{A.mean(rdy):.1f}" if rdy else "\u2014",
        "rdy_all": f"{A.mean(rdy_all):.1f}" if rdy_all else "\u2014",
        "sess8": f"{sess8:.1f}",
        "acwr": f"{acwr:.2f}",
        "eff_loss": f"{(cur_p - best_p) * 60:.0f}",
        "eff_raw_loss": f"{(raw_last - best_p) * 60:.0f}",
        "best_q": best_q.replace("20", "'"),
        "cv": str(cv),
        "vol_hi": f"{max(vols):.0f}" if vols else "0",
        "vol_lo": f"{min(vols):.0f}" if vols else "0",
        "elev_hi": str(elev_by_q.get(cur_q, 0)),
        "elev_lo": str(min(elev_by_q.values()) if elev_by_q else 0),
        "q_lo": best_q.replace("2026-", "").replace("2025-", ""),
        "fast_gap": str((D.today - max(r["_d"] for r in fast)).days) if fast else "\u2014",
        "fast_last": max(r["_d"] for r in fast).strftime("%-d %B") if fast else "\u2014",
        "fast_n": str(len(fast)),
        "strength_n": str(len(strength)),
        "kmish_n": str(len(kmish)),
        "comp_n": str(len(comp)),
        "long_n": str(len(out["longruns"])),
        "long_last": (A.d(out["longruns"][-1]["d"]).strftime("%-d %B")
                      if out["longruns"] else "\u2014"),
        "ceiling": f"{long8:.1f}",
        "gap_days": str(gap_days) if gap_days else "\u2014",
        "hk_lo": str(min(hk_in)) if hk_in else "0",
        "hk_hi": str(max(hk_in)) if hk_in else "0",
        "hk_total": str(sum(hk_all.values())),
        "hk_first": (min(hk_all).replace("-", "\u00b7") if hk_all else "\u2014"),
        "hk_last": (max(hk_all).replace("-", "\u00b7") if hk_all else "\u2014"),
        "sleep_n": str(len(D.sleep)),
        "sleep_span": str(span),
        "sleep_cov": str(round(100 * len(D.sleep) / span)) if span else "0",
        "sleep_blind": str(100 - round(100 * len(D.sleep) / span)) if span else "0",
        "sleep_avg": f"{A.mean(tot):.1f}" if tot else "\u2014",
        "sleep_need": f"{A.mean(need):.1f}" if need else "\u2014",
        "sleep_short": str(sum(1 for r in D.sleep if (A.f(r["total_h"]) or 9) < 7)),
        "unworn_n": str(len(unworn)),
        "unworn_avg": f"{A.mean(unworn):.0f}" if unworn else "\u2014",
        "unworn_delta": (f"{A.mean(unworn) - A.mean(rhr):.0f}"
                         if unworn and rhr else "\u2014"),
        "rhr_n": str(len(rhr)), "hrv_n": str(len(hrv)),
        "runs": str(len(runs)),
        "daily_n": str(len(D.daily)),
        "ready_n": str(len(D.ready)),
    }

    # --- verdict and findings are CONDITIONAL. They were hardcoded, which meant
    # the page asserted the same conclusions no matter what the data said.
    n = out["narr"]
    rec_ok = (not rhr or not rhr_base or A.mean(rhr) <= A.mean(rhr_base) + 2) and \
             (not hrv or not hrv_base or A.mean(hrv) >= A.mean(hrv_base) - 4)
    under = sess8 < 3.2 or acwr < 0.9
    vol4 = A.mean([wk_km[k] for k in sorted(wk_km)[-4:]]) or 0

    if rec_ok and under:
        head = "The body is fine. The training is the problem."
    elif not rec_ok and not under:
        head = "You are absorbing more than you are recovering from."
    elif rec_ok and not under:
        head = "The base is there. The race-specific work is not."
    else:
        head = "Low volume and poor recovery at the same time."

    p1 = ("Twelve months of watch data say your recovery has headroom to spare: "
          f"resting heart rate <strong>{n['rhr']} bpm</strong>, overnight HRV "
          f"<strong>{n['hrv']}</strong>. Nothing here looks overtrained."
          ) if rec_ok else (
          "Recovery is the thing to watch: resting heart rate "
          f"<strong>{n['rhr']} bpm</strong> against a {n['rhr_base']} baseline, "
          f"overnight HRV <strong>{n['hrv']}</strong> against {n['hrv_base']}.")

    bits = [f"you are <strong>running {n['sess8']} times a week</strong> at "
            f"{vol4:.0f} km"]
    if acwr < 0.8:
        bits.append(f"your seven-day load has fallen to <strong>{n['acwr']}&times; "
                    "your 28-day average</strong>")
    elif acwr > 1.5:
        bits.append(f"your seven-day load has spiked to <strong>{n['acwr']}&times; "
                    "your 28-day average</strong>")
    if float(n["eff_loss"]) > 8:
        bits.append(f"at the same heart rate you are {n['eff_loss']} s/km slower "
                    "than at your peak")
    tail = ("You are not at risk of breaking. You are at risk of arriving "
            "underdone.") if under and rec_ok else \
           ("The load is real; protect the recovery that is absorbing it."
            if not rec_ok else "Keep the consistency and sharpen the specifics.")
    p2 = "What the data says is that " + ", ".join(bits) + ". " + tail
    out["verdict"] = {"head": head, "p1": p1, "p2": p2}

    loss, raw = float(n["eff_loss"]), float(n["eff_raw_loss"])
    if loss <= 5:
        n["eff_sentence"] = ("Corrected for terrain there is no meaningful decline "
                             f"&mdash; {loss:.0f} s/km off your best quarter.")
    elif raw - loss >= 8:
        n["eff_sentence"] = (f"Corrected for terrain the drop is <strong>{loss:.0f} "
                             f"s/km</strong>, not {raw:.0f}.")
    else:
        n["eff_sentence"] = (f"The drop is <strong>{loss:.0f} s/km</strong> and "
                             "terrain does not explain it.")

    fnd = []
    if not kmish:
        fnd.append({"sev": "crit", "num": "0", "title": "1 km efforts, ever",
                    "body": "Not one run in twelve months falls in the 0.8\u20131.5 km "
                            "band. The race is that distance, eight times, on tired "
                            "legs. You have never rehearsed the single repeating "
                            "unit of the event."})
    if not strength:
        fnd.append({"sev": "crit", "num": "0",
                    "title": "strength or station sessions logged",
                    "body": "The plan calls for two lifting days plus a station "
                            "circuit. Garmin has no record of any. If it is happening "
                            "untracked it still fatigues you while staying invisible "
                            "to readiness and load."})
    if fast and (D.today - max(r["_d"] for r in fast)).days > 45:
        fnd.append({"sev": "warn", "num": n["fast_gap"],
                    "title": "days since a run under 5:00/km",
                    "body": f"Last one was {n['fast_last']}. You have {n['fast_n']} "
                            "sub-5:00 runs in the year, and the plan\u2019s interval "
                            "session has not appeared in the data."})
    if len(comp) < 12:
        fnd.append({"sev": "warn", "num": n["comp_n"],
                    "title": "days pairing a run with a second session",
                    "body": "Your plan names compromised running \u2014 running hard off "
                            "station fatigue \u2014 as the limiter for a strong result. "
                            "Few days in the year put a run next to anything else."})
    if long8 < 20:
        fnd.append({"sev": "warn", "num": f"{long8:.0f}",
                    "title": "km longest run in eight weeks",
                    "body": "Phase 2b expects 22\u201326 km and the marathon behind it "
                            "needs 30 km+. Build back at about 2 km a week rather "
                            "than jumping."})
    out["findings"] = fnd

    # --- per-activity detail for the drill-down views
    det, index = {}, []
    dd = A.DATA / "details"
    for r in D.acts:
        aid = str(r["activity_id"])
        p = dd / f"{aid}.json"
        row = {"id": aid, "d": r["date"], "n": (r["name"] or r["_type"])[:60],
               "t": r["_type"], "km": r["_km"], "min": r["_min"],
               "pace": r["_pace"], "hr": r["_hr"], "maxhr": A.f(r["max_hr"]),
               "elev": r["_elev"], "load": r["_load"] or None,
               "cal": A.f(r["calories"]), "cad": A.f(r["avg_cadence"]),
               "ate": A.f(r["aerobic_te"]), "ane": A.f(r["anaerobic_te"]),
               "vo2": A.f(r["vo2max"]), "det": p.exists()}
        index.append(row)
        if p.exists():
            try:
                det[aid] = json.loads(p.read_text())
            except Exception:
                pass
    index.sort(key=lambda x: x["d"], reverse=True)
    out["acts"] = index
    out["detail"] = det
    out["zones_def"] = {"max": A.MAX_HR, "floors": A.ZONE_FLOORS, "lthr": A.LTHR}

    # sleep and daily, keyed by date, for the night and day views
    out["sleepIdx"] = sorted(
        [{k: (A.f(r[k]) if k not in ("date", "quality", "hrv_status") else r[k])
          for k in r} for r in D.sleep],
        key=lambda x: x["date"], reverse=True)
    out["dayIdx"] = sorted(
        [{k: (A.f(r[k]) if k not in ("date", "hrv_status", "training_status") else r[k])
          for k in r} for r in D.daily],
        key=lambda x: x["date"], reverse=True)
    out["readyIdx"] = {r["date"]: {"score": A.f(r["score"]), "level": r["level"],
                                   "feedback": r["feedback"],
                                   "rec": A.f(r["recovery_time_h"]),
                                   "acute": A.f(r["acute_load"])}
                       for r in D.ready}

    out["week"] = WP.build(D)
    out["health"] = build_health(D, out)
    return out, D


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None, help="output path (default dashboard.html)")
    ap.add_argument("--no-health", action="store_true",
                    help="omit the DEXA/health tab (for a published copy)")
    args = ap.parse_args()
    data, D = build_data()
    if args.no_health:
        data["health"] = None
    tpl = (ROOT / "dashboard.template.html").read_text(encoding="utf-8")
    if "__DATA__" not in tpl:
        raise SystemExit("template is missing the __DATA__ placeholder")
    html = tpl.replace("__DATA__", json.dumps(data, separators=(",", ":")))
    for k, v in data["narr"].items():
        html = html.replace("{{" + k + "}}", str(v))
    # The page is often served without a charset header; literal UTF-8 inside
    # <script> garbles there. Escape it at build time rather than policing the
    # template by hand.
    a = html.index("<script>")
    b = html.rindex("</script>")
    html = (html[:a]
            + "".join(c if ord(c) < 128 else "\\u%04x" % ord(c) for c in html[a:b])
            + html[b:])
    leftover = re.findall(r"\{\{(\w+)\}\}", html)
    if leftover:
        raise SystemExit(f"template has unfilled tokens: {sorted(set(leftover))}")
    out = Path(args.out) if args.out else ROOT / "dashboard.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    w = data["week"]
    done = sum(1 for s in w["sessions"] if s["done"] and not s.get("optional"))
    req = sum(1 for s in w["sessions"] if not s.get("optional"))
    print(f"wrote {out}")
    print(f"  week {w['week_label']}: {done}/{req} auto-matched, "
          f"{w['km_done']} / {w['km_floor']} km")


if __name__ == "__main__":
    main()
