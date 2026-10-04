"""Engine tests without a browser: multi-site recordings, coordinates and the Darwin Core export."""
import json
import os
import shutil
import subprocess
import sys
import xml.dom.minidom
import zipfile
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
AUDIO_DIR = Path(os.environ.get("BRUTING_TEST_AUDIO", REPO.parent / "recording_output"))
SITE_A = ["SRD00689_20260807_034523.wav", "SRD00689_20260807_044523.wav"]


def cli(*args):
    subprocess.run([sys.executable, "-W", "ignore", str(REPO / "app.py"), *map(str, args)], check=True,
                   capture_output=True, env=dict(os.environ, BRUTING_CACHE=str(args[1].parent)))


@pytest.fixture(scope="module")
def multisite(tmp_path_factory):
    if not all((AUDIO_DIR / f).exists() for f in SITE_A):
        pytest.skip(f"test audio not found in {AUDIO_DIR}")
    ws = tmp_path_factory.mktemp("cache") / "ws"
    (ws / "audio").mkdir(parents=True)
    (ws / "job").mkdir()
    for f in SITE_A:
        shutil.copy(AUDIO_DIR / f, ws / "audio" / f)
    # second site: same audio, different GUANO GPS + note (same byte length keeps the RIFF chunk valid)
    raw = (AUDIO_DIR / SITE_A[1]).read_bytes()
    a, b = b"Loc Position: -33.81713 151.08", b"Loc Position: -34.01234 150.95"
    assert a in raw
    (ws / "audio" / "SRD00777_20260807_044523.wav").write_bytes(raw.replace(a, b).replace(b"Note: Balcony", b"Note: Gardens"))
    (ws / "job" / "params.json").write_text(json.dumps(dict(loc_mode="guano", ts_mode="local", tz="Australia/Sydney",
                                                            min_conf=0.1, sensitivity=1.0, overlap=0)))
    cli("inspect", ws)
    cli("process", ws)
    (ws / "s.json").write_text(json.dumps(dict(thr=0.3, override=True, verified_only=False, gap=0, species=[])))
    cli("zip", ws, ws / "s.json", ws / "out.zip")
    return zipfile.ZipFile(ws / "out.zip")


def read(z, name):
    return pd.read_csv(z.open(name))


def test_every_detection_has_coordinates_and_site(multisite):
    d = read(multisite, "detections_all.csv")
    assert {"site", "lat", "lon", "location_source", "device"} <= set(d.columns)
    assert d["lat"].notna().all() and d["lon"].notna().all()
    assert set(d["location_source"]) == {"GUANO"}
    assert d["site"].nunique() == 2


def test_sites_table_separates_locations(multisite):
    s = read(multisite, "sites.csv")
    assert len(s) == 2
    assert sorted(s["lat"].round(5)) == [-34.01234, -33.81713]
    assert (s["detections"] > 0).all()


def test_daily_counts_are_per_site(multisite):
    daily = read(multisite, "daily_counts.csv")
    assert "site" in daily.columns and daily["site"].nunique() == 2


def test_darwin_core_occurrences(multisite):
    occ = read(multisite, "darwin_core/occurrence.csv")
    assert len(occ) == len(read(multisite, "call_events.csv"))
    assert occ["occurrenceID"].is_unique
    assert set(occ["basisOfRecord"]) == {"MachineObservation"}
    assert occ["decimalLatitude"].between(-90, 90).all() and occ["decimalLongitude"].between(-180, 180).all()
    assert set(occ["geodeticDatum"]) == {"WGS84"}
    assert occ["eventDate"].str.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d/").all()
    assert occ["locationID"].nunique() == 2
    for f in ("darwin_core/meta.xml", "darwin_core/eml.xml"):
        xml.dom.minidom.parseString(multisite.read(f))
    meta = multisite.read("darwin_core/meta.xml").decode()
    assert meta.count("<field ") == len(occ.columns)


# --------------------------------------------------------------------------- site profile / reliability
def _app():
    sys.path.insert(0, str(REPO))
    import app  # noqa: E402
    return app


def _checks(app, sci, common, pairs):
    """pairs: (confidence, decision)"""
    return pd.DataFrame([dict(det_id=f"{sci[:3]}{i}", decision=dec, new_scientific="", new_common="", verifier="t",
                              note="", timestamp_utc=f"2026-10-04T00:00:{i:02d}Z", scientific=sci, common=common,
                              confidence=c, file="f.wav", start_s=i * 3, audio_sha="x")
                         for i, (c, dec) in enumerate(pairs)], columns=app.VER_COLS)


def test_reliability_learns_lowest_safe_threshold(tmp_path):
    app = _app()
    # wrong at 0.12 and 0.2, right from 0.3 up -> lowest 0.05-step threshold excluding the errors is 0.25
    v = _checks(app, "Trichoglossus moluccanus", "Rainbow Lorikeet",
                [(0.12, "reject"), (0.2, "reject"), (0.3, "confirm"), (0.4, "confirm"), (0.5, "confirm"),
                 (0.6, "confirm"), (0.8, "confirm")])
    rel = app.reliability(pd.DataFrame(columns=app.DET_COLS), v, {"species": {}}, target=0.9, min_n=5)
    r = rel.iloc[0]
    assert (r.checked, r.correct, r.wrong) == (7, 5, 2)
    assert r.learned_threshold == 0.25 and r.effective_threshold == 0.25
    # too few checks -> no learned threshold, status says how many more
    few = app.reliability(pd.DataFrame(columns=app.DET_COLS), v.head(3), {"species": {}}, target=0.9, min_n=5)
    assert pd.isna(few.iloc[0].learned_threshold) and "more check" in few.iloc[0].status


def test_profile_roundtrip_reuses_checks_and_rules(tmp_path):
    app = _app()
    a, b = tmp_path / "a", tmp_path / "b"
    for w in (a, b):
        w.mkdir()
    v = _checks(app, "Gallinula tenebrosa", "Dusky Moorhen", [(0.2, "reject"), (0.3, "reject")])
    v.to_csv(a / "verifications.csv", index=False)
    app.set_rule(a, "Gallinula tenebrosa", "block")
    app.set_rule(a, "Cracticus torquatus", "threshold", 0.25)
    prof = app.export_profile(a, pd.DataFrame(columns=app.DET_COLS), app.load_ver(a), 0.9, 5)
    json.dumps(prof)  # serialisable
    info = app.import_profile(b, json.loads(json.dumps(prof, default=str)))
    assert info["verifications"] == 2 and info["rules"] == 2
    assert len(app.load_ver(b)) == 2
    th, blocked = app.species_rules(app.reliability(pd.DataFrame(columns=app.DET_COLS), app.load_ver(b),
                                                    app.load_profile(b)))
    assert blocked == ["Gallinula tenebrosa"] and th == {"Cracticus torquatus": 0.25}
    # rules change what is reported: block removes the moorhen, the butcherbird threshold beats the global one
    det = pd.DataFrame([dict(det_id="d1", scientific="Gallinula tenebrosa", common="Dusky Moorhen", confidence=0.95),
                        dict(det_id="d2", scientific="Cracticus torquatus", common="Gray Butcherbird", confidence=0.3)])
    for c in app.DET_COLS:
        if c not in det:
            det[c] = None
    det["datetime"] = pd.Timestamp("2026-08-07")
    kept = app.filter_det(det, app.load_ver(b).iloc[0:0], dict(thr=0.5, thresholds=th, blocked=blocked))
    assert kept["common"].tolist() == ["Gray Butcherbird"]
    with pytest.raises(ValueError):
        app.import_profile(b, {"hello": 1})


def test_verification_shows_in_results(tmp_path):
    """Fixes 1–3: verified events are counted, rejected species are listed even when they vanish."""
    app = _app()
    det = pd.DataFrame([
        dict(det_id="a1", scientific="Trichoglossus moluccanus", common="Rainbow Lorikeet", confidence=0.9, start_s=0.0),
        dict(det_id="a2", scientific="Trichoglossus moluccanus", common="Rainbow Lorikeet", confidence=0.4, start_s=60.0),
        dict(det_id="m1", scientific="Gymnorhina tibicen", common="Australian Magpie", confidence=0.95, start_s=30.0)])
    for c in app.DET_COLS:
        if c not in det:
            det[c] = None
    det = det.assign(file="f.wav", end_s=det["start_s"] + 3, site="S", date="2026-08-07", species_id="BN0000",
                     sci_code="X", datetime=pd.Timestamp("2026-08-07 04:00") + pd.to_timedelta(det["start_s"], unit="s"))
    by = det.set_index("det_id")
    ver = pd.DataFrame([app.ver_row(by.loc["a2"].to_dict() | {"det_id": "a2"}, "confirm"),
                        app.ver_row(by.loc["m1"].to_dict() | {"det_id": "m1"}, "reject")], columns=app.VER_COLS)
    R = app.compute(det, ver, pd.DataFrame(columns=["file", "status", "duration_s"]), dict(thr=0.5))
    lor = R["summary"].set_index("common").loc["Rainbow Lorikeet"]
    assert (lor.call_events, lor.verified_events) == (2, 1)
    assert R["ev"]["verified"].tolist().count(True) == 1
    assert "Australian Magpie" not in set(R["summary"]["common"])          # fully rejected -> gone from results
    rej = R["rejected"].set_index("common").loc["Australian Magpie"]       # ...but still listed
    assert rej.rejected == 1 and rej.still_reported.startswith("no")


def test_page_javascript_is_valid(tmp_path):
    """A broken script silently disables verification, drag & drop and the call bar."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    app = _app()
    f = tmp_path / "page.js"
    f.write_text(app.JS)
    r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
