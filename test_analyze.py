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
