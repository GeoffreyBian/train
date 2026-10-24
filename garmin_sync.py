#!/usr/bin/env python3
"""Pull Garmin Connect data into local CSVs under data/.

Incremental sync: ./.venv/bin/python garmin_sync.py
Backfill:         ./.venv/bin/python garmin_sync.py --days 365
Legacy login:     ./.venv/bin/python garmin_sync.py --login   (~6 months)

Two sources, merged per field:
  * Personal API key (GARMIN_PERSONAL_API_KEY in .env, personal_api.py):
    activities, sleep, daily wellness, HRV, weight. Official, no password.
  * Legacy SSO session (~/.garminconnect): everything the key cannot reach --
    readiness, training status, VO2max/heat, endurance score, per-run VO2max,
    and the decimal respiration/floors the key rounds off.
Either one alone still syncs what it can.
"""

import argparse
import csv
import getpass
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from garminconnect import Garmin
from garminconnect.exceptions import GarminConnectTooManyRequestsError

from personal_api import KeyMissing, PersonalAPI, PersonalAPIError

DATA = Path(__file__).resolve().parent / "data"
TOKENS = Path.home() / ".garminconnect"
DEFAULT_BACKFILL = 120


# ---------------------------------------------------------------- helpers

def g(d, *keys, default=None):
    """Nested get that tolerates missing keys and None nodes."""
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
        if d is None:
            return default
    return d


def secs_to_h(s):
    return round(s / 3600, 2) if isinstance(s, (int, float)) else None


def rnd(v, n=1):
    return round(v, n) if isinstance(v, (int, float)) else None


def clean(v):
    """Garmin returns -1 for 'no data' on several wellness fields."""
    return None if v in (-1, -1.0, "-1") else v


def pace_min_km(distance_m, duration_s):
    if not distance_m or not duration_s or distance_m < 100:
        return None
    return round((duration_s / 60) / (distance_m / 1000), 2)


def merge_csv(path, rows, key, columns, fill_only=()):
    """Upsert rows into a CSV keyed on `key`, keeping it sorted by key desc.

    Columns in `fill_only` hold a coarser value: the Personal API drops the
    decimal on respiration and floors that the legacy pull keeps. Such a value
    fills an empty cell, and replaces a finer one only when it actually
    disagrees (by more than 1, e.g. today's floors synced again later) -- never
    when it is just the same number with the decimal cut off.
    """
    if not rows and not path.exists():
        return 0
    existing = {}
    if path.exists():
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                existing[r[key]] = r
    added = sum(1 for r in rows if str(r[key]) not in existing)
    for r in rows:
        k = str(r[key])
        # Merge field by field rather than replacing the row. Sources here are
        # sparse and independent -- a daily pull carries no endurance score, a
        # wide HRV range can come back empty -- and a whole-row replace lets one
        # partial pull silently blank a column that another source had filled.
        prev = existing.get(k, {})
        merged = {}
        for c in columns:
            v = r.get(c)
            v = "" if v is None else str(v)
            if c in fill_only and v != "" and prev.get(c, "") != "":
                try:
                    # The key truncates (15.96 -> 15) where the legacy value
                    # rounds (16.0), so a gap of exactly 1 is still the same
                    # reading.
                    if abs(float(v) - float(prev[c])) <= 1:
                        v = ""
                except ValueError:
                    v = ""
            merged[c] = v if v != "" else prev.get(c, "")
        existing[k] = merged
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        for k in sorted(existing, reverse=True):
            row = existing[k]
            w.writerow({c: row.get(c, "") for c in columns})
    return added


def last_date(path, key="date"):
    if not path.exists():
        return None
    dates = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            v = (r.get(key) or "")[:10]
            if len(v) == 10:
                dates.append(v)
    return max(dates) if dates else None


# ---------------------------------------------------------------- auth

def _credentials():
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    env = Path(__file__).resolve().parent / ".env"
    if (not email or not password) and env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                if v.strip():
                    os.environ.setdefault(k.strip(), v.strip())
        email = email or os.environ.get("GARMIN_EMAIL")
        password = password or os.environ.get("GARMIN_PASSWORD")
    if not email:
        email = input("Garmin email: ").strip()
    if not password:
        password = getpass.getpass("Garmin password: ")
    return email, password


def _prompt_mfa():
    return input("MFA code (check email/SMS): ").strip()


class LegacyUnavailable(RuntimeError):
    pass


def login(interactive=False, prompt=True):
    """Return an authenticated Garmin client (the unofficial SSO session).

    Tokens live in ~/.garminconnect and are shared with the garmin MCP server.
    The library loads and saves them itself when handed the tokenstore path.

    With prompt=False a missing or expired session raises LegacyUnavailable
    instead of asking for a password, so an unattended sync can carry on with
    the Personal API alone.
    """
    tokenstore = str(TOKENS)

    if not interactive and TOKENS.exists():
        try:
            api = Garmin()
            api.login(tokenstore)
            return api
        except Exception as e:
            if not prompt:
                raise LegacyUnavailable(f"cached tokens unusable ({type(e).__name__})") from None
            print(f"  cached tokens unusable ({type(e).__name__}); logging in again",
                  file=sys.stderr)
    elif not interactive and not prompt:
        raise LegacyUnavailable(f"no saved session in {TOKENS}")

    email, password = _credentials()
    api = Garmin(email=email, password=password, prompt_mfa=_prompt_mfa)
    try:
        # Passing the tokenstore makes the library persist tokens on success.
        api.login(tokenstore)
    except GarminConnectTooManyRequestsError:
        print(
            "\nGarmin rate-limited this IP on every login strategy.\n"
            "Wait ~30 minutes and retry, or switch networks (phone hotspot works).\n"
            "This is a per-IP limit, not an account lock.",
            file=sys.stderr,
        )
        sys.exit(2)
    print(f"  tokens saved to {TOKENS} (valid ~6 months)")
    return api


# ---------------------------------------------------------------- pulls

ACTIVITY_COLS = [
    "date", "activity_id", "name", "type", "distance_km", "duration_min",
    "pace_min_km", "avg_hr", "max_hr", "calories", "elev_gain_m",
    "avg_cadence", "aerobic_te", "anaerobic_te", "training_load", "vo2max",
]


def pull_activities(api, start, end):
    raw = api.get_activities_by_date(start.isoformat(), end.isoformat()) or []
    rows = []
    for a in raw:
        dist = a.get("distance")
        dur = a.get("duration")
        rows.append({
            "date": (a.get("startTimeLocal") or "")[:10],
            "activity_id": a.get("activityId"),
            "name": (a.get("activityName") or "").replace("\n", " "),
            "type": g(a, "activityType", "typeKey"),
            "distance_km": rnd(dist / 1000, 2) if dist else None,
            "duration_min": rnd(dur / 60, 1) if dur else None,
            "pace_min_km": pace_min_km(dist, dur),
            "avg_hr": rnd(a.get("averageHR"), 0),
            "max_hr": rnd(a.get("maxHR"), 0),
            "calories": rnd(a.get("calories"), 0),
            "elev_gain_m": rnd(a.get("elevationGain"), 0),
            "avg_cadence": rnd(a.get("averageRunningCadenceInStepsPerMinute"), 0),
            "aerobic_te": rnd(a.get("aerobicTrainingEffect"), 1),
            "anaerobic_te": rnd(a.get("anaerobicTrainingEffect"), 1),
            "training_load": rnd(a.get("activityTrainingLoad"), 0),
            "vo2max": rnd(a.get("vO2MaxValue"), 1),
        })
    return rows


SLEEP_COLS = [
    "date", "score", "quality", "total_h", "deep_h", "light_h", "rem_h",
    "awake_h", "resting_hr", "avg_hr", "avg_hrv", "hrv_7d_avg", "hrv_status",
    "avg_spo2", "avg_respiration", "body_battery_change", "sleep_need_h",
    "skin_temp_c",
]


def pull_sleep_range(api, start, end):
    """Bulk sleep pull.

    get_sleep_data(cdate) needs one request per night and returns an empty
    shell whenever the watch wasn't worn, so it can't distinguish "no data"
    from "request failed". get_sleep_daily(start, end) returns only the nights
    that actually have data, in one request, and carries fields the per-day
    endpoint omits (sleep need, 7-day HRV average, skin temperature).
    """
    try:
        raw = api.get_sleep_daily(start.isoformat(), end.isoformat()) or []
    except Exception as e:
        print(f"  (bulk sleep failed: {type(e).__name__})", file=sys.stderr)
        return []
    rows = []
    for rec in raw:
        v = rec.get("values") or {}
        if not v.get("totalSleepTimeInSeconds"):
            continue
        rows.append({
            "date": rec.get("calendarDate"),
            "score": clean(v.get("sleepScore")),
            "quality": v.get("sleepScoreQuality"),
            "total_h": secs_to_h(v.get("totalSleepTimeInSeconds")),
            "deep_h": secs_to_h(v.get("deepTime")),
            "light_h": secs_to_h(v.get("lightTime")),
            "rem_h": secs_to_h(v.get("remTime")),
            "awake_h": secs_to_h(v.get("awakeTime")),
            "resting_hr": clean(v.get("restingHeartRate")),
            "avg_hr": rnd(v.get("avgHeartRate"), 0),
            "avg_hrv": rnd(v.get("avgOvernightHrv"), 0),
            "hrv_7d_avg": rnd(v.get("hrv7dAverage"), 0),
            "hrv_status": v.get("hrvStatus"),
            "avg_spo2": clean(v.get("spO2")),
            "avg_respiration": rnd(v.get("respiration"), 1),
            "body_battery_change": clean(v.get("bodyBatteryChange")),
            "sleep_need_h": secs_to_h((v.get("sleepNeed") or 0) * 60) or None,
            "skin_temp_c": rnd(v.get("skinTempC"), 1),
        })
    return rows


DAILY_COLS = [
    "date", "steps", "resting_hr", "hrv_last_night", "hrv_status",
    "stress_avg", "body_battery_high", "body_battery_low",
    "intensity_min_moderate", "intensity_min_vigorous",
    "active_calories", "floors_climbed", "training_status", "vo2max",
    "fitness_age", "heat_acclimation_pct", "endurance_score",
]


def pull_daily(api, day, with_status=True):
    ds = day.isoformat()
    stats = api.get_stats(ds) or {}
    row = {
        "date": ds,
        "steps": clean(stats.get("totalSteps")),
        "resting_hr": clean(stats.get("restingHeartRate")),
        "stress_avg": clean(stats.get("averageStressLevel")),
        "body_battery_high": clean(stats.get("bodyBatteryHighestValue")),
        "body_battery_low": clean(stats.get("bodyBatteryLowestValue")),
        "intensity_min_moderate": clean(stats.get("moderateIntensityMinutes")),
        "intensity_min_vigorous": clean(stats.get("vigorousIntensityMinutes")),
        "active_calories": rnd(clean(stats.get("activeKilocalories")), 0),
        "floors_climbed": rnd(clean(stats.get("floorsAscended")), 1),
    }
    if not with_status:
        return row if any(row[k] is not None for k in row if k != "date") else None
    try:
        ts = api.get_training_status(ds) or {}
        latest = ts.get("mostRecentTrainingStatus") or {}
        for v in (g(latest, "latestTrainingStatusData") or {}).values():
            row["training_status"] = v.get("trainingStatusFeedbackPhrase") or v.get("trainingStatus")
            break
        vo2 = g(ts, "mostRecentVO2Max", "generic", "vo2MaxPreciseValue")
        row["vo2max"] = rnd(vo2, 1)
    except Exception:
        pass
    return row if any(row[k] is not None for k in row if k != "date") else None


READINESS_COLS = [
    "date", "score", "level", "feedback", "sleep_score", "hrv_factor",
    "recovery_time_h", "acute_load", "stress_factor",
]


def pull_readiness(api, day):
    ds = day.isoformat()
    try:
        data = api.get_training_readiness(ds) or []
    except Exception:
        return None
    if not data:
        return None
    r = data[0]
    if r.get("score") is None:
        return None
    return {
        "date": ds,
        "score": r.get("score"),
        "level": r.get("level"),
        "feedback": r.get("feedbackShort"),
        "sleep_score": r.get("sleepScore"),
        "hrv_factor": r.get("hrvFactorFeedback"),
        "recovery_time_h": rnd((r.get("recoveryTime") or 0) / 60, 1) or None,
        "acute_load": r.get("acuteLoad"),
        "stress_factor": r.get("stressHistoryFactorFeedback"),
    }


def pull_vo2max_range(api, start, end):
    """Bulk VO2max history, keyed by date.

    get_training_status only carries the current value, so a year of history
    needs this endpoint. Also yields heat/altitude acclimation, which explains
    summer pace drift.
    """
    out = {}
    try:
        raw = api.get_max_metrics_range(start.isoformat(), end.isoformat()) or []
    except Exception:
        return out
    for rec in raw:
        gen = rec.get("generic") or {}
        d = gen.get("calendarDate")
        if d and gen.get("vo2MaxPreciseValue"):
            heat = g(rec, "heatAltitudeAcclimation", "heatAcclimationPercentage")
            out[d] = (rnd(gen["vo2MaxPreciseValue"], 1), gen.get("fitnessAge"), heat)
    return out


WEIGHT_COLS = ["date", "weight_kg", "source"]


def pull_weight_range(api, start, end):
    """Body weight history.

    The DEXA scan is one point in time; weight between scans is the only signal
    that says whether a change was fat or lean, so it is worth carrying.
    Garmin stores several rows per day when a device and the app both report,
    and a USER_SETTING row is a profile default rather than a measurement --
    keep one row per day and prefer a real measurement.
    """
    rows = {}
    try:
        raw = api.get_weigh_ins(start.isoformat(), end.isoformat()) or {}
    except Exception:
        return []
    days = raw.get("dailyWeightSummaries") or raw.get("dateWeightList") or []
    for day in days:
        for w in (day.get("allWeightMetrics") or [day]):
            grams, ds = w.get("weight"), w.get("calendarDate")
            if not grams or not ds:
                continue
            src = w.get("sourceType") or ""
            prev = rows.get(ds)
            # A real measurement always beats the profile default for that day.
            if prev and (prev["source"] != "USER_SETTING" or src == "USER_SETTING"):
                continue
            rows[ds] = {"date": ds, "weight_kg": rnd(grams / 1000.0, 2), "source": src}
    return list(rows.values())


def pull_endurance_range(api, start, end):
    """Endurance score, keyed by date.

    Garmin's single number for sustained aerobic capacity. Unlike VO2max it
    moves with training volume rather than with peak efforts, so it tracks the
    thing the Hyrox build is actually trying to raise.
    """
    out = {}
    try:
        raw = api.get_endurance_score(start.isoformat(), end.isoformat()) or {}
    except Exception:
        return out
    # Garmin reports this weekly, not daily: groupMap is keyed by week-start.
    # The column is therefore sparse by design -- one value per Monday.
    for ds, grp in (raw.get("groupMap") or {}).items():
        sc = grp.get("groupMax") or grp.get("groupAverage")
        if sc:
            out[ds] = round(sc)
    cur = raw.get("enduranceScoreDTO") or {}
    if cur.get("calendarDate") and cur.get("overallScore"):
        out[cur["calendarDate"]] = round(cur["overallScore"])
    return out


def pull_hrv_range(api, start, end):
    """Bulk HRV, keyed by date. One request instead of one per day."""
    out = {}
    try:
        raw = api.get_hrv_data_range(start.isoformat(), end.isoformat()) or {}
    except Exception:
        return out
    for rec in (raw.get("hrvSummaries") or raw.get("hrvSummary") or []):
        if isinstance(rec, dict) and rec.get("calendarDate"):
            out[rec["calendarDate"]] = (
                rec.get("lastNightAvg"), rec.get("status"), rec.get("weeklyAvg"))
    return out


# ---------------------------------------------------------------- Personal API pulls
#
# Same CSV columns as the legacy pulls above, read through the official key
# (personal_api.py) instead of the scraped SSO session. Fields the key does not
# carry are left None, and merge_csv never blanks a value with None, so a
# legacy pull on the same run can still fill them.

def personal_activities(papi, start, end):
    # Same search endpoint get_activities_by_date wraps, so pull_activities'
    # mapping applies unchanged. The key omits vO2MaxValue.
    class _Shim:
        def get_activities_by_date(self, s, e):
            return papi.activities(date.fromisoformat(s), date.fromisoformat(e))
    return pull_activities(_Shim(), start, end)


def personal_sleep(papi, start, end, hrv=None, rhr=None):
    """Sleep rows from dailySleepSummariesByDate.

    The key's sleep record has no HRV, SpO2, skin temperature or overnight body
    battery. HRV comes from the HRV endpoint (pass its map in); the rest stay
    blank rather than guessed. Resting HR comes from the daily summary (pass
    {date: bpm}); on all 115 overlapping nights it equals the legacy value.
    """
    hrv, rhr = hrv or {}, rhr or {}
    rows = []
    for s in papi.sleep(start, end):
        if not s.get("sleepTimeSeconds"):
            continue
        ds = s.get("calendarDate")
        overall = g(s, "sleepScores", "overall") or {}
        need = g(s, "sleepNeed", "actual")
        last, status, weekly = hrv.get(ds, (None, None, None))
        rows.append({
            "date": ds,
            "score": clean(overall.get("value")),
            "quality": overall.get("qualifierKey"),
            "total_h": secs_to_h(s.get("sleepTimeSeconds")),
            "deep_h": secs_to_h(s.get("deepSleepSeconds")),
            "light_h": secs_to_h(s.get("lightSleepSeconds")),
            "rem_h": secs_to_h(s.get("remSleepSeconds")),
            "awake_h": secs_to_h(s.get("awakeSleepSeconds")),
            "resting_hr": rhr.get(ds),
            "avg_hr": rnd(s.get("avgHeartRate"), 0),
            # float() so the CSV reads 86.0 like the legacy pull, not 86
            "avg_hrv": rnd(float(last), 0) if last is not None else None,
            "hrv_7d_avg": rnd(float(weekly), 0) if weekly is not None else None,
            "hrv_status": status,
            "avg_spo2": None,
            "avg_respiration": rnd(s.get("averageRespirationValue"), 1),
            "body_battery_change": None,
            "sleep_need_h": secs_to_h(need * 60) if need else None,
            "skin_temp_c": None,
        })
    return rows


def personal_daily(papi, start, end):
    """Daily rows from v2/dailysummaries: up to a year per request, where the
    legacy path makes one get_stats call per day."""
    rows = []
    for d in papi.dailies(start, end):
        if not d.get("includesWellnessData"):
            continue
        row = {
            "date": d.get("calendarDate"),
            "steps": clean(g(d, "movement", "steps", "value")),
            "resting_hr": clean(g(d, "heartRate", "restingValue")),
            "stress_avg": clean(g(d, "stress", "avgLevel")),
            "body_battery_high": clean(g(d, "bodyBattery", "maxValue")),
            "body_battery_low": clean(g(d, "bodyBattery", "minValue")),
            "intensity_min_moderate": clean(g(d, "intensityMinutes", "moderate")),
            "intensity_min_vigorous": clean(g(d, "intensityMinutes", "vigorous")),
            "active_calories": None,
            "floors_climbed": None,
        }
        # float() so the CSV reads 0.0 / 61.0 like the legacy pull.
        cal = clean(g(d, "calories", "burnedActive"))
        row["active_calories"] = rnd(float(cal), 0) if cal is not None else None
        fl = clean(g(d, "movement", "floorsAscended", "value"))
        row["floors_climbed"] = rnd(float(fl), 1) if fl is not None else None
        if any(row[k] is not None for k in row if k != "date"):
            rows.append(row)
    return rows


def personal_hrv(papi, start, end):
    return {r["calendarDate"]: (r.get("lastNightAvg"), r.get("status"), r.get("weeklyAvg"))
            for r in papi.hrv(start, end) if r.get("calendarDate")}


def personal_weight(papi, start, end):
    class _Shim:
        def get_weigh_ins(self, s, e):
            return papi.weight(s, e)
    return pull_weight_range(_Shim(), start, end)


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--login", action="store_true", help="force interactive login")
    p.add_argument("--days", type=int, help="sync the last N days")
    p.add_argument("--full", action="store_true", help=f"backfill {DEFAULT_BACKFILL}+ days")
    p.add_argument("--verbose", action="store_true", help="show login strategy details")
    p.add_argument("--personal-only", action="store_true",
                   help="skip the legacy login; Personal API data only")
    p.add_argument("--legacy-only", action="store_true",
                   help="ignore the Personal API key (the pre-Oct-2026 behaviour)")
    args = p.parse_args()

    logging.getLogger("garminconnect").setLevel(
        logging.DEBUG if args.verbose else logging.ERROR
    )
    DATA.mkdir(parents=True, exist_ok=True)

    if args.login:
        print("Connecting to Garmin Connect (legacy login)...")
        api = login(interactive=True)
        print(f"  logged in as {api.display_name or api.username}")
        print("Login OK. Run without --login to sync.")
        return

    # Two sources. The Personal API key (official, no password, up to a year
    # per key) carries the core data. The legacy SSO session is still the only
    # way to readiness, training status, VO2max/heat, endurance score and a
    # few finer-grained fields, so it runs on top when its tokens are good --
    # and when they are not, the core data still lands instead of nothing.
    papi = api = None
    if not args.legacy_only:
        try:
            papi = PersonalAPI()
            papi._bearer()
            print("Personal API: key OK")
        except KeyMissing as e:
            print(f"Personal API: not configured ({e})", file=sys.stderr)
        except PersonalAPIError as e:
            print(f"Personal API: {e}", file=sys.stderr)
            papi = None
    if not args.personal_only:
        try:
            # Prompt for a password only when there is no key to fall back on.
            api = login(prompt=papi is None)
            print(f"Legacy session: logged in as {api.display_name or api.username}")
        except LegacyUnavailable as e:
            print(f"Legacy session: unavailable ({e}). Readiness, training status, "
                  "VO2max and endurance score will not update this run.\n"
                  "  Renew it with: ./.venv/bin/python garmin_sync.py --login",
                  file=sys.stderr)
    if papi is None and api is None:
        print("No Garmin source available.", file=sys.stderr)
        sys.exit(1)

    today = date.today()
    if args.days:
        start = today - timedelta(days=args.days)
    elif args.full:
        start = today - timedelta(days=DEFAULT_BACKFILL)
    else:
        seen = last_date(DATA / "daily.csv")
        if api:
            # Days synced on the key alone have no readiness. Reach back to the
            # last readiness row so a renewed legacy session fills that gap.
            ready = last_date(DATA / "readiness.csv")
            if seen and ready:
                seen = min(seen, max(ready, (today - timedelta(days=60)).isoformat()))
        start = (date.fromisoformat(seen) - timedelta(days=2)) if seen else today - timedelta(days=DEFAULT_BACKFILL)
    days = (today - start).days + 1
    print(f"Syncing {start} -> {today} ({days} days)\n")

    LOSSY = ("avg_respiration", "floors_climbed")

    def step(label, fn):
        """Run one pull; a failure in one source never sinks the whole sync."""
        try:
            return fn()
        except Exception as e:
            print(f"\n  {label} failed: {type(e).__name__}: {e}", file=sys.stderr)
            return None

    print("Activities...", end=" ", flush=True)
    acts, src = [], []
    if papi:
        r = step("Personal API activities", lambda: personal_activities(papi, start, today))
        if r is not None:
            acts += r; src.append("key")
            merge_csv(DATA / "activities.csv", r, "activity_id", ACTIVITY_COLS)
    if api:
        # One request; the only source of per-run VO2max.
        r = step("legacy activities", lambda: pull_activities(api, start, today))
        if r is not None:
            acts += r; src.append("legacy")
            merge_csv(DATA / "activities.csv", r, "activity_id", ACTIVITY_COLS)
    ids = {str(a["activity_id"]) for a in acts}
    print(f"{len(ids)} found [{'+'.join(src) or 'none'}]")

    hrv = {}
    if papi:
        hrv = step("Personal API HRV", lambda: personal_hrv(papi, start, today)) or {}
    if not hrv and api:
        hrv = pull_hrv_range(api, start, today)

    key_daily = None
    if papi:
        key_daily = step("Personal API dailies", lambda: personal_daily(papi, start, today))
    rhr = {x["date"]: x["resting_hr"] for x in key_daily or [] if x.get("resting_hr")}

    print("Sleep...", end=" ", flush=True)
    nights, src = set(), []
    if papi:
        r = step("Personal API sleep", lambda: personal_sleep(papi, start, today, hrv, rhr))
        if r is not None:
            nights |= {x["date"] for x in r}; src.append("key")
            merge_csv(DATA / "sleep.csv", r, "date", SLEEP_COLS, fill_only=LOSSY)
    if api:
        # One request; adds resting HR, SpO2, skin temp, overnight body battery
        # and decimal respiration, none of which the key's sleep record has.
        r = step("legacy sleep", lambda: pull_sleep_range(api, start, today))
        if r:
            nights |= {x["date"] for x in r}; src.append("legacy")
            merge_csv(DATA / "sleep.csv", r, "date", SLEEP_COLS)
    pct = round(100 * len(nights) / days) if days else 0
    print(f"{len(nights)}/{days} nights recorded ({pct}%) [{'+'.join(src) or 'none'}]")

    print("Daily...", end=" ", flush=True)
    got, src, errs = set(), [], 0
    if papi:
        r = key_daily
        if r is not None:
            for row in r:
                if row["date"] in hrv:
                    row["hrv_last_night"], row["hrv_status"], _ = hrv[row["date"]]
            got |= {x["date"] for x in r}; src.append("key")
            merge_csv(DATA / "daily.csv", r, "date", DAILY_COLS, fill_only=LOSSY)
    if api:
        vo2 = pull_vo2max_range(api, start, today)
        endur = pull_endurance_range(api, start, today)
        rows = []
        # training_status has no bulk endpoint and is slow; it only matters
        # recently. The per-day get_stats here also carries decimal floors.
        status_cutoff = today - timedelta(days=30)
        for i in range(days):
            d = start + timedelta(days=i)
            try:
                r = pull_daily(api, d, with_status=(d >= status_cutoff))
                if r:
                    ds_ = d.isoformat()
                    if ds_ in hrv:
                        r["hrv_last_night"], r["hrv_status"], _ = hrv[ds_]
                    if ds_ in vo2:
                        r["vo2max"], r["fitness_age"], r["heat_acclimation_pct"] = vo2[ds_]
                    if ds_ in endur:
                        r["endurance_score"] = endur[ds_]
                    rows.append(r)
            except Exception:
                errs += 1
            time.sleep(0.2)
        got |= {x["date"] for x in rows}; src.append("legacy")
        merge_csv(DATA / "daily.csv", rows, "date", DAILY_COLS)
    print(f"{len(got)} days [{'+'.join(src) or 'none'}]" + (f", {errs} errors" if errs else ""))

    if api:
        print("Readiness...", end=" ", flush=True)
        rows, errs = [], 0
        for i in range(days):
            d = start + timedelta(days=i)
            try:
                r = pull_readiness(api, d)
                if r:
                    rows.append(r)
            except Exception:
                errs += 1
            time.sleep(0.2)
        n = merge_csv(DATA / "readiness.csv", rows, "date", READINESS_COLS)
        print(f"{len(rows)} days, {n} new" + (f", {errs} errors" if errs else ""))
    else:
        print("Readiness... skipped (legacy session only; the key has no readiness)")

    print("Weight...", end=" ", flush=True)
    wstart = start - timedelta(days=365)
    wrows = []
    if papi:
        # personal_weight swallows errors and returns [], so fall back on empty
        wrows = step("Personal API weight", lambda: personal_weight(papi, wstart, today)) or []
    if not wrows and api:
        wrows = pull_weight_range(api, wstart, today)
    n = merge_csv(DATA / "weight.csv", wrows, "date", WEIGHT_COLS)
    print(f"{len(wrows)} weigh-ins, {n} new")

    print(f"\nDone. Data in {DATA}")
    with open(DATA / ".last_sync", "w") as f:
        f.write(datetime.now().isoformat(timespec="seconds"))


if __name__ == "__main__":
    main()
