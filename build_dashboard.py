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
    return out, D


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None, help="output path (default dashboard.html)")
    args = ap.parse_args()
    data, D = build_data()
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
