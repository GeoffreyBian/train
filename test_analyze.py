"""Guards on the analysis logic. Run: ./.venv/bin/python -m pytest test_analyze.py -q"""
import csv, sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze as A
import garmin_sync as G


def test_sentinel_cleaning():
    assert G.clean(-1) is None and G.clean(-1.0) is None
    assert G.clean(0) == 0 and G.clean(55) == 55


def test_pace_and_units():
    assert G.pace_min_km(5000, 1260) == 4.2
    assert G.pace_min_km(0, 100) is None
    assert G.secs_to_h(3600) == 1.0
    assert A.fmt_pace(5.5) == "5:30" and A.fmt_pace(None) == "  -  "


def test_run_filter_drops_artifacts():
    """A 42 min/km 'run' is a walk or GPS noise and must not skew pace stats."""
    D = A.Data.__new__(A.Data)
    D.acts = [
        {"_type": "running", "_km": 10.0, "_pace": 5.5, "_d": date(2026, 8, 1)},
        {"_type": "running", "_km": 5.0, "_pace": 42.0, "_d": date(2026, 8, 2)},
        {"_type": "running", "_km": 0.5, "_pace": 5.0, "_d": date(2026, 8, 3)},
        {"_type": "ice_hockey", "_km": None, "_pace": None, "_d": date(2026, 8, 4)},
    ]
    kept = D.runs()
    assert len(kept) == 1 and kept[0]["_km"] == 10.0


def test_rhr_excludes_unworn_nights():
    """The core correctness guard.

    Garmin reports a restingHeartRate even on nights the watch wasn't worn,
    derived from daytime readings and ~14 bpm high. Recovery must read only
    sleep-backed nights, or the trend tracks watch-wearing, not fitness.
    """
    real = [r for r in csv.DictReader(open(A.DATA / "sleep.csv"))
            if r.get("resting_hr")]
    worn = {r["date"] for r in csv.DictReader(open(A.DATA / "sleep.csv"))}
    daily = list(csv.DictReader(open(A.DATA / "daily.csv")))
    unworn = [float(r["resting_hr"]) for r in daily
              if r["date"] not in worn and r.get("resting_hr")]
    if real and unworn:
        worn_avg = sum(float(r["resting_hr"]) for r in real) / len(real)
        assert sum(unworn) / len(unworn) > worn_avg + 5, \
            "unworn-night RHR should be clearly inflated; check the assumption"


def test_efficiency_uses_flat_runs_only():
    """Terrain must be controlled: hills cost pace at the same HR."""
    src = Path(A.__file__).read_text()
    assert "FLAT_M_PER_KM" in src
    assert "_elev" in src[src.index("def efficiency"):src.index("def vo2_and_heat")]


def test_report_runs_clean_on_real_data():
    import io, contextlib
    D = A.Data()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        A.race_status(D)
        wk_km, wk_n = A.volume(D, 8)
        A.adherence(D, wk_n)
        A.efficiency(D)
        A.vo2_and_heat(D)
        A.intensity(D)
        A.load_ratio(D)
        A.cross_training(D)
        A.hyrox_gap(D, None)
        A.seasonal_load(D)
        A.recovery(D)
        A.sleep(D)
        A.verdict(D)
    out = buf.getvalue()
    assert "VERDICT" in out and "nan" not in out.lower()


def test_empty_data_does_not_crash():
    D = A.Data.__new__(A.Data)
    D.acts, D.daily, D.sleep, D.ready = [], [], [], []
    D.today = date(2026, 9, 2)
    assert D.runs() == []
    assert A.mean([]) is None and A.pct(1, 0) == 0


# --------------------------------------------------------------------------
# Week matcher. The slot order is the whole correctness story here: a long run
# must not be eaten by the easy slot, and a tempo effort must not be filed as
# either intervals or easy.

import week_plan as W


def _acts(*specs):
    """Build minimal activity dicts the way analyze.Data would."""
    out = []
    for i, (day, km, pace) in enumerate(specs):
        out.append({"activity_id": 1000 + i, "name": "Run", "type": "running",
                    "_type": "running", "_d": date(2026, 8, 31) + timedelta(days=day),
                    "_km": km, "_pace": pace, "_hr": 150.0, "_min": km * pace,
                    "_load": 50.0, "_elev": 10.0, "anaerobic_te": "0.0",
                    "max_hr": "170", "date": "x"})
    return out


def _match(sessions, acts):
    D = A.Data.__new__(A.Data)
    D.acts = acts
    D.today = date(2026, 9, 6)
    matched, _ = W.match(D, sessions, date(2026, 8, 31))
    return {s["id"]: (s["done"]["km"] if s["done"] else None) for s in matched}


def test_long_run_not_eaten_by_easy_slot():
    D = A.Data()
    s = W.prescribe(D, date(2026, 8, 31))
    long_km = next(x["target"]["min_km"] for x in s if x["id"] == "long")
    got = _match(s, _acts((0, long_km + 3, 6.5), (2, 6.0, 6.5)))
    assert got["long"] == round(long_km + 3, 1), got
    assert got["easy"] == 6.0, got


def test_fast_run_goes_to_intervals_not_tempo():
    D = A.Data()
    s = W.prescribe(D, date(2026, 8, 31))
    got = _match(s, _acts((1, 8.0, 4.9)))
    assert got["reps"] == 8.0 and got["tempo"] is None, got


def test_tempo_pace_run_goes_to_tempo_not_easy():
    """The Sept 3 case: 8.2 km at 5:26 is not easy running."""
    D = A.Data()
    s = W.prescribe(D, date(2026, 8, 31))
    got = _match(s, _acts((3, 8.2, 5.44)))
    assert got["tempo"] == 8.2, got
    assert got["easy"] is None, got


def test_easy_run_stays_easy():
    D = A.Data()
    s = W.prescribe(D, date(2026, 8, 31))
    got = _match(s, _acts((3, 7.0, 6.9)))
    assert got["easy"] == 7.0 and got["tempo"] is None, got


def test_each_activity_claimed_once():
    D = A.Data()
    s = W.prescribe(D, date(2026, 8, 31))
    acts = _acts((0, 20.0, 6.4), (1, 8.0, 4.8), (3, 7.5, 5.6), (4, 6.0, 7.0))
    matched, extra = W.match(D_stub(acts), s, date(2026, 8, 31))
    claimed = [s2["done"]["km"] for s2 in matched if s2["done"]]
    assert len(claimed) == len(set(claimed)) == 4, claimed
    assert extra == [], extra


def D_stub(acts):
    D = A.Data.__new__(A.Data)
    D.acts = acts
    D.today = date(2026, 9, 6)
    return D


def test_merge_csv_does_not_blank_columns(tmp_path):
    """A partial pull must not wipe a column another source filled.

    Syncing a wide date range once returned no HRV at all, and the whole-row
    upsert quietly blanked twelve months of it. Sources here are sparse and
    independent, so the merge has to be per field.
    """
    import garmin_sync as G
    cols = ["date", "hrv_last_night", "endurance_score"]
    p = tmp_path / "daily.csv"
    G.merge_csv(p, [{"date": "2026-09-01", "hrv_last_night": 90}], "date", cols)
    # a later pull that carries only the endurance score
    G.merge_csv(p, [{"date": "2026-09-01", "endurance_score": 6475}], "date", cols)
    row = list(csv.DictReader(open(p)))[0]
    assert row["hrv_last_night"] == "90", row
    assert row["endurance_score"] == "6475", row


def test_dexa_scan_loads_and_places_visceral_fat():
    """The Health tab is built off these two; both degrade to empty, not crash."""
    scans = A.load_dexa()
    if not scans:
        return                      # no scan on file is a valid state
    s = scans[-1]
    assert s["lean_g"] > 0 and s["fat_g"] > 0
    # tissue %fat is fat / (fat + lean) -- the number reference tables use
    calc = 100 * s["fat_g"] / (s["fat_g"] + s["lean_g"])
    assert abs(calc - s["fat_pct_tissue"]) < 0.15, (calc, s["fat_pct_tissue"])
    pc, band = A.vat_percentile(s["vat_g"], s["age_years"])
    assert pc is None or 1 <= pc <= 100
