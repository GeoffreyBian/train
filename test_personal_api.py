"""Guards on the Personal API path. Offline: fixtures mirror real responses.
Run: ./.venv/bin/python -m pytest test_personal_api.py -q"""
import base64, csv, json, sys, time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import garmin_sync as G
import personal_api as P
import sync_details as SD


def _jwt(exp):
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{body}.sig"


class FakeAPI:
    """Stands in for PersonalAPI; returns canned endpoint payloads."""
    def __init__(self, **payloads):
        self.p = payloads

    def activities(self, s, e): return self.p.get("activities", [])
    def sleep(self, s, e): return self.p.get("sleep", [])
    def dailies(self, s, e): return self.p.get("dailies", [])
    def hrv(self, s, e): return self.p.get("hrv", [])
    def weight(self, s, e): return self.p.get("weight", {})
    def activity(self, aid): return self.p.get("activity", {})


def test_chunks_respect_endpoint_limits():
    """Garmin answers an empty 400 past each endpoint's window, so a 70-day
    sleep pull must go out as 31 + 31 + 8 and lose no day at the seams."""
    parts = list(P.chunks(date(2026, 1, 1), date(2026, 3, 11), P.MAX_DAYS["sleep"]))
    assert [(b - a).days + 1 for a, b in parts] == [31, 31, 8]
    assert parts[0][0] == date(2026, 1, 1) and parts[-1][1] == date(2026, 3, 11)
    assert all((parts[i + 1][0] - parts[i][1]).days == 1 for i in range(len(parts) - 1))
    assert list(P.chunks(date(2026, 1, 2), date(2026, 1, 1), 31)) == []


def test_token_cache_reused_until_near_expiry(tmp_path):
    cache = tmp_path / "token"
    api = P.PersonalAPI(key="k", cache=cache)
    minted = []
    api._mint = lambda: minted.append(1) or _jwt(time.time() + 86400)
    tag = api._key_tag()
    cache.write_text(tag + "\n" + _jwt(time.time() + 3600))
    api._bearer()
    assert not minted, "a valid cached token must not trigger a new exchange"
    api.token = None
    cache.write_text(tag + "\n" + _jwt(time.time() + 60))   # inside the 5 min margin
    api._bearer()
    assert minted == [1]


def test_cached_token_from_another_key_is_ignored(tmp_path):
    """A freshly pasted key must be checked against Garmin, not waved through
    by the token the previous key left in the cache."""
    cache = tmp_path / "token"
    old = P.PersonalAPI(key="old-key", cache=cache)
    cache.write_text(old._key_tag() + "\n" + _jwt(time.time() + 3600))
    new = P.PersonalAPI(key="new-key", cache=cache)
    minted = []
    new._mint = lambda: minted.append(1) or _jwt(time.time() + 86400)
    new._bearer()
    assert minted == [1]


def test_network_failure_is_a_source_error_not_a_crash(tmp_path, monkeypatch):
    import urllib.error
    def boom(*a, **k):
        raise urllib.error.URLError("nodename nor servname provided")
    monkeypatch.setattr(P.urllib.request, "urlopen", boom)
    monkeypatch.setattr(P.time, "sleep", lambda s: None)
    api = P.PersonalAPI(key="k", cache=tmp_path / "token")
    for call in (api._mint, api.profile):
        try:
            call()
            assert False, "expected PersonalAPIError"
        except P.PersonalAPIError:
            pass


def test_coarse_value_replaces_stale_but_not_precise(tmp_path):
    """Today's floors keep moving, so a stale 5.0 must give way to a current 12;
    a finished night's 15.8 respiration must not become 15.0."""
    cols = ["date", "floors_climbed", "avg_respiration"]
    p = tmp_path / "daily.csv"
    G.merge_csv(p, [{"date": "2026-10-24", "floors_climbed": 5.0, "avg_respiration": 15.8}],
                "date", cols)
    G.merge_csv(p, [{"date": "2026-10-24", "floors_climbed": 12.0, "avg_respiration": 15.0}],
                "date", cols, fill_only=("floors_climbed", "avg_respiration"))
    row = list(csv.DictReader(open(p)))[0]
    assert row["floors_climbed"] == "12.0" and row["avg_respiration"] == "15.8"


def test_failed_upgrade_stays_partial():
    """If legacy endpoints fail mid-run the record must stay partial (retried
    next run), never be frozen as a complete file with no laps or zones."""
    class Flaky:
        def get_activity_splits(self, aid): raise RuntimeError("429")
        def get_activity_hr_in_timezones(self, aid): raise RuntimeError("429")
        def get_activity_weather(self, aid): return {}
        def get_activity(self, aid): return {"summaryDTO": {"steps": 1}}
    act = {"activity_id": "1", "date": "2026-10-20", "name": "Run", "type": "running"}
    rec = SD.fetch(Flaky(), act, want_series=False)
    assert rec.get("partial") is True
    class Fine(Flaky):
        def get_activity_splits(self, aid): return {"lapDTOs": []}
        def get_activity_hr_in_timezones(self, aid): return []
    assert "partial" not in SD.fetch(Fine(), act, want_series=False)


def test_key_read_from_env_file_not_other_vars(tmp_path, monkeypatch):
    monkeypatch.delenv("GARMIN_PERSONAL_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("GARMIN_EMAIL=x\n# GARMIN_PERSONAL_API_KEY=commented\n"
                   "GARMIN_PERSONAL_API_KEY=GCDC_abc.def\n")
    monkeypatch.setattr(P, "ENV", env)
    assert P.api_key() == "GCDC_abc.def"
    env.write_text("GARMIN_PERSONAL_API_KEY=\n")
    try:
        P.api_key()
        assert False, "an empty key must raise KeyMissing"
    except P.KeyMissing:
        pass


def test_fill_only_never_overwrites_finer_value(tmp_path):
    """The key rounds respiration and floors to whole numbers. It may fill an
    empty cell but must never replace the legacy decimal."""
    cols = ["date", "steps", "floors_climbed"]
    p = tmp_path / "daily.csv"
    G.merge_csv(p, [{"date": "2026-10-23", "steps": 1, "floors_climbed": 61.1}], "date", cols)
    G.merge_csv(p, [{"date": "2026-10-23", "steps": 2, "floors_climbed": 61.0},
                    {"date": "2026-10-24", "steps": 3, "floors_climbed": 4.0}],
                "date", cols, fill_only=("floors_climbed",))
    rows = {r["date"]: r for r in csv.DictReader(open(p))}
    assert rows["2026-10-23"] == {"date": "2026-10-23", "steps": "2", "floors_climbed": "61.1"}
    assert rows["2026-10-24"]["floors_climbed"] == "4.0"


def test_sleep_mapping_matches_legacy_columns():
    rec = {"calendarDate": "2026-10-24", "sleepTimeSeconds": 33060,
           "deepSleepSeconds": 6000, "lightSleepSeconds": 19860,
           "remSleepSeconds": 7200, "awakeSleepSeconds": 1380, "avgHeartRate": 51.0,
           "averageRespirationValue": 15.0, "sleepNeed": {"actual": 480},
           "sleepScores": {"overall": {"value": 87, "qualifierKey": "GOOD"}}}
    empty = {"calendarDate": "2026-10-20", "sleepTimeSeconds": 0}
    rows = G.personal_sleep(FakeAPI(sleep=[rec, empty]), None, None,
                            hrv={"2026-10-24": (86, "BALANCED", 93)},
                            rhr={"2026-10-24": 47})
    assert len(rows) == 1, "unworn nights must be dropped, as the legacy pull does"
    r = rows[0]
    assert set(r) == set(G.SLEEP_COLS)
    assert (r["score"], r["quality"], r["total_h"], r["deep_h"]) == (87, "GOOD", 9.18, 1.67)
    assert r["sleep_need_h"] == 8.0 and r["resting_hr"] == 47
    # floats, so the CSV reads 86.0 exactly as the legacy pull wrote it
    assert repr(r["avg_hrv"]) == "86.0" and repr(r["hrv_7d_avg"]) == "93.0"
    # what the key does not carry stays blank rather than invented
    assert r["avg_spo2"] is None and r["skin_temp_c"] is None
    assert r["body_battery_change"] is None


def test_daily_mapping_and_unworn_days():
    d = {"calendarDate": "2026-10-23", "includesWellnessData": True,
         "movement": {"steps": {"value": 28316}, "floorsAscended": {"value": 61}},
         "heartRate": {"restingValue": 47}, "stress": {"avgLevel": 16},
         "bodyBattery": {"maxValue": 82, "minValue": 11},
         "intensityMinutes": {"moderate": 50, "vigorous": 126},
         "calories": {"burnedActive": 1869}}
    off = {"calendarDate": "2026-10-01", "includesWellnessData": False}
    rows = G.personal_daily(FakeAPI(dailies=[d, off]), None, None)
    assert [r["date"] for r in rows] == ["2026-10-23"]
    r = rows[0]
    assert set(r) <= set(G.DAILY_COLS)
    assert (r["steps"], r["resting_hr"], r["body_battery_low"]) == (28316, 47, 11)
    assert repr(r["active_calories"]) == "1869.0" and repr(r["floors_climbed"]) == "61.0"


def test_activity_mapping_reuses_legacy_mapper():
    a = {"activityId": 24830722162, "startTimeLocal": "2026-10-23 16:50:58",
         "activityName": "Vancouver Running", "activityType": {"typeKey": "running"},
         "distance": 21169.59, "duration": 7113.24, "averageHR": 139.0,
         "aerobicTrainingEffect": 3.6, "activityTrainingLoad": 135.47}
    r = G.personal_activities(FakeAPI(activities=[a]), date(2026, 10, 23), date(2026, 10, 23))[0]
    assert (r["date"], r["distance_km"], r["pace_min_km"], r["training_load"]) == \
        ("2026-10-23", 21.17, 5.6, 135.0)
    assert r["vo2max"] is None   # key omits vO2MaxValue; legacy fills it


def test_partial_detail_is_marked_and_upgradable(tmp_path):
    listed = {"1": {f"hrTimeInZone_{i}": s for i, s in enumerate([10, 20, 30, 0, 0], 1)}}
    api = FakeAPI(activity={"summaryDTO": {"steps": 9706, "minHR": 97.0,
                                           "trainingEffectLabel": "AEROBIC_BASE"}})
    act = {"activity_id": "1", "date": "2026-10-20", "name": "Run", "type": "running"}
    rec = SD.fetch_partial(api, act, listed, [98, 117, 137, 156, 176])
    assert rec["partial"] is True and rec["laps"] == []
    assert rec["zones"][1] == {"z": 2, "sec": 20, "lo": 117}
    assert rec["summary"]["steps"] == 9706 and rec["summary"]["minHr"] == 97
    p = tmp_path / "1.json"
    p.write_text(json.dumps(rec))
    assert SD.is_partial(p)
    p.write_text(json.dumps({**rec, "partial": False}))
    assert not SD.is_partial(p)
