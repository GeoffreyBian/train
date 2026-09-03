#!/usr/bin/env python3
"""Regenerate dashboard.html from the current CSVs.

    ./.venv/bin/python build_dashboard.py

Everything the page renders is computed here and injected as one JSON blob, so
the published dashboard is never hand-edited — sync, rebuild, republish.
"""

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

    zc = Counter()
    for r in runs:
        if r["_hr"]:
            p = r["_hr"] / A.MAX_HR * 100
            zc["Z1" if p < 68 else "Z2" if p < 78 else "Z3" if p < 85
               else "Z4" if p < 92 else "Z5"] += 1
    meta = {"Z1": ("Easy", "<68%"), "Z2": ("Aerobic", "68–78%"),
            "Z3": ("Tempo", "78–85%"), "Z4": ("Threshold", "85–92%"),
            "Z5": ("VO₂", "92%+")}
    out["zones"] = [{"z": z, "l": meta[z][0], "r": meta[z][1], "n": zc[z]}
                    for z in ["Z1", "Z2", "Z3", "Z4", "Z5"]]

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
    last8 = sorted(wk_km)[-8:]
    sess8 = A.mean([wk_n[k] for k in last8]) or 0
    long8 = max((wk_long[k] for k in last8), default=0)
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
    gap_days = (D.today - max(r["_d"] for r in longs)).days if longs else None

    def band(v, good, warn, invert=False):
        x = (v <= good, v <= warn) if not invert else (v >= good, v >= warn)
        return "good" if x[0] else "warn" if x[1] else "crit"

    out["hdr"] = {
        "days": left, "weeks": left // 7, "race": A.HYROX.isoformat(),
        "through": D.today.isoformat(), "runs": len(runs),
        "km": round(sum(r["_km"] for r in runs)),
        "maxhr": A.MAX_HR,
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
    strength = [r for r in D.acts if any(x in r["_type"] for x in A.STRENGTH_TYPES)]
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
        "runs": str(len(runs)),
        "daily_n": str(len(D.daily)),
        "ready_n": str(len(D.ready)),
    }

    out["week"] = WP.build(D)
    return out, D


def main():
    data, D = build_data()
    tpl = (ROOT / "dashboard.template.html").read_text(encoding="utf-8")
    if "__DATA__" not in tpl:
        raise SystemExit("template is missing the __DATA__ placeholder")
    html = tpl.replace("__DATA__", json.dumps(data, separators=(",", ":")))
    for k, v in data["narr"].items():
        html = html.replace("{{" + k + "}}", str(v))
    leftover = re.findall(r"\{\{(\w+)\}\}", html)
    if leftover:
        raise SystemExit(f"template has unfilled tokens: {sorted(set(leftover))}")
    out = ROOT / "dashboard.html"
    out.write_text(html, encoding="utf-8")
    w = data["week"]
    done = sum(1 for s in w["sessions"] if s["done"] and not s.get("optional"))
    req = sum(1 for s in w["sessions"] if not s.get("optional"))
    print(f"wrote {out}")
    print(f"  week {w['week_label']}: {done}/{req} auto-matched, "
          f"{w['km_done']} / {w['km_floor']} km")


if __name__ == "__main__":
    main()
