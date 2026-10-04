"""
Bruting Calls — BirdNET bird-call detection, human verification and reporting.

One script, three roles:
  shiny run app.py                          -> the Shiny for Python web app
  python app.py process <workspace>         -> background BirdNET worker (spawned by the app)
  python app.py inspect <workspace>         -> read audio metadata into manifest.json
  python app.py report  <workspace> <settings.json> <out.pdf>   -> build the PDF report
The R front-end (app.R) uses the same CLI, so both apps produce identical results.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import html
import importlib.util
import io
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

import numpy as np
import pandas as pd
import soundfile as sf
import matplotlib
import matplotlib.ticker

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.dates as mdates  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402

# --------------------------------------------------------------------------- metadata (shown on the About tab)
APP_TITLE = "Bruting Calls"
APP_VERSION = "0.4.0"  # semantic versioning; bump together with DESCRIPTION, CITATION.cff and CHANGELOG.md
APP_FAMILY = "Demeter Insights"
APP_AUTHOR = "Abhimanyu Raj Singh"
APP_LICENCE = "MIT (app code) · BirdNET model: CC BY-NC-SA 4.0"
APP_PURPOSE = ("Finds bird calls in passive acoustic recordings (e.g. Smith Robotics Demeter recorders) with BirdNET, "
               "lets a person listen to and verify uncertain detections, and summarises which species called, how often, "
               "when, for how long and at what pitch — ending in a downloadable PDF report and CSV tables.")
APP_INPUTS = [
    "Audio recordings: WAV (16-bit PCM, any sample rate, mono or stereo), FLAC or OGG. Many files at once.",
    "Recommended: recorder GUANO metadata in each WAV (start Timestamp, Loc Position). Otherwise the start time is "
    "read from a YYYYMMDD_HHMMSS file name, and location can be typed in.",
    "Typical input: 5-minute 48 kHz mono files of ~28 MB each.",
]
APP_LIMITS = [
    "Upload / temporary storage: {gb:.0f} GB shared by all users at once (uploads, clips and results).",
    "Data is kept only for the browser session — download the PDF report and CSV bundle before leaving.",
    "Runtime: ~4–6 s per 5-minute file on a recent laptop CPU (≈1 h of audio in ~1 min); "
    "{jobs} job(s) run at once, others queue.",
    "Empty (0-byte) or undecodable files are skipped and listed in the report.",
    "Species identifications are automated (BirdNET GLOBAL 6K v2.4) and should be verified by ear before use.",
]
CITATION = (f"{APP_AUTHOR} ({datetime.now():%Y}). {APP_TITLE}: BirdNET call detection, verification and reporting "
            f"for Demeter acoustic recordings (v{APP_VERSION}) [Software]. {APP_FAMILY}. See CITATION.cff.")

# Data dictionary for every exported table (FAIR: interoperable / reusable)
DATA_DICTIONARY = [
    # table, column, unit, description
    ("detections", "det_id", "", "Stable ID of one detection: hash of audio SHA-1 + start time + species"),
    ("detections", "file", "", "Recording file name"),
    ("detections", "audio_sha", "", "SHA-1 of the recording (provenance; identical files share it)"),
    ("detections", "start_s / end_s", "s", "Start / end of the 3-s BirdNET window from the start of the file"),
    ("detections", "datetime", "local time", "Recording start + start_s (ISO 8601, no zone; zone in settings)"),
    ("detections", "species_id", "", "BirdNET class index, BN#### (GLOBAL 6K v2.4 label order)"),
    ("detections", "sci_code", "", "6-letter code: first 3 letters of genus + species"),
    ("detections", "scientific / common", "", "BirdNET scientific and English common name (after relabelling)"),
    ("detections", "confidence", "0–1", "BirdNET score for the species in that window"),
    ("detections", "peak_hz", "Hz", "Dominant frequency after subtracting the recording's median noise spectrum"),
    ("detections", "low_hz / high_hz", "Hz", "5 % / 95 % points of the noise-subtracted call energy (300–15,000 Hz)"),
    ("detections", "snr_db", "dB", "Window energy vs. the recording's median background, 300–15,000 Hz"),
    ("detections", "decision", "", "Human verification: pending, confirm, reject or relabel"),
    ("detections", "site", "", "Recording site: GUANO note (or device ID) with coordinates, so multi-site data stays apart"),
    ("detections", "lat / lon", "decimal degrees, WGS84", "Location of the recorder for that file"),
    ("detections", "location_source", "", "GUANO, manual, manual (file had no GPS) or unknown"),
    ("detections", "device", "", "Recorder make, model and ID from GUANO"),
    ("call_events", "start_dt / end_dt", "local time", "Start / end of a call event"),
    ("call_events", "duration_s", "s", "Length of the call event (merged consecutive detections)"),
    ("call_events", "windows", "count", "Number of 3-s detections merged into the event"),
    ("call_events", "gap_to_next_s", "s", "Silence until the next event of the same species in the same file"),
    ("species_summary", "detections / call_events", "count", "Totals at the chosen threshold"),
    ("species_summary", "calls_per_hour", "events/h", "call_events ÷ hours of audio analysed"),
    ("species_summary", "total_call_s / mean_call_s", "s", "Summed / mean call-event duration"),
    ("species_summary", "median_gap_s / min_gap_s", "s", "Median / shortest time between calls"),
    ("species_summary", "first_heard / last_heard", "local time", "First / last detection"),
    ("daily_counts", "date, call_events, detections", "date, count", "Counts per species per calendar day"),
    ("files", "status", "", "analysed, empty, unreadable or error"),
    ("sites", "site, lat, lon, files, hours, species, detections, call_events", "", "One row per recording site"),
    ("reliability", "checked, correct, wrong, precision", "count, 0–1", "Human checks of BirdNET per species; "
     "precision = confirmed ÷ checked"),
    ("reliability", "learned_threshold / manual_threshold / effective_threshold", "0–1",
     "Lowest confidence reaching the target precision / set by hand / the one applied"),
    ("site_profile.json", "species, verifications", "", "Load next session to reuse thresholds, blocks and checks"),
    ("darwin_core/occurrence", "Darwin Core terms", "", "One occurrence per call event for GBIF / ALA "
     "(MachineObservation; eventDate with UTC offset; decimalLatitude/Longitude WGS84; verification status)"),
    ("files", "duration_s", "s", "Audio length analysed"),
    ("verifications", "decision, new_scientific, verifier, timestamp_utc", "", "Every human decision, latest wins"),
]

# --------------------------------------------------------------------------- config
APP_FILE = Path(__file__).resolve()
APP_DIR = APP_FILE.parent
CACHE_ROOT = Path(os.environ.get("BRUTING_CACHE", APP_DIR / "cache")).resolve()
CACHE_LIMIT = int(float(os.environ.get("BRUTING_CACHE_GB", "20")) * 1024**3)
MAX_JOBS = int(os.environ.get("BRUTING_MAX_JOBS", "2"))
ORPHAN_TTL_S = int(float(os.environ.get("BRUTING_ORPHAN_HOURS", "2")) * 3600)
AUDIO_EXT = (".wav", ".flac", ".ogg")
CLIP_PAD_S = 0.5
DEFAULT_TZ = os.environ.get("BRUTING_TZ", "Australia/Sydney")
CACHE_ROOT.mkdir(parents=True, exist_ok=True)
WWW_DIR = APP_DIR / "www"      # bundled images / audio for the About page
DEMO_DIR = APP_DIR / "demo"    # bundled demo recordings (see demo/ATTRIBUTION.md)
ALLOW_STOP = os.environ.get("BRUTING_ALLOW_STOP") == "1"  # local use only: lets the browser stop the server

# Palette: one teal hue (the King Parrot's wing) on warm cream, matching the app's 70s theme.
SURF, INK, INK2, MUTED, GRID = "#fffbf3", "#3b2a1e", "#6e5a48", "#9a8670", "#eadbc4"
BLUE, BLUE_D, BLUE_L, NEUTRAL = "#2f7469", "#1f4f47", "#a9d3c9", "#f3e8d6"
TEAL_MID = "#6fb0a2"  # BirdNET-only part of a bar; verified part uses BLUE_D
SEQ = ["#fffbf3", "#e4f1ec", "#c3e0d7", "#9ccbbf", "#6fb0a2", "#428f81", "#2f7469", "#1c4a42"]
SEQ_CMAP = LinearSegmentedColormap.from_list("bc_seq", SEQ)
plt.rcParams.update({
    "font.size": 9, "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2,
    "ytick.color": INK2, "axes.titlesize": 10, "axes.titleweight": "bold", "axes.titlecolor": INK,
    "figure.facecolor": SURF, "axes.facecolor": SURF, "savefig.facecolor": SURF,
})

DET_COLS = ["det_id", "file", "audio_sha", "start_s", "end_s", "datetime", "date", "hour",
            "species_id", "sci_code", "scientific", "common", "confidence",
            "peak_hz", "low_hz", "high_hz", "snr_db",
            "site", "lat", "lon", "location_source", "device"]
VER_COLS = ["det_id", "decision", "new_scientific", "new_common", "verifier", "note", "timestamp_utc",
            # context of the verified detection, so a site profile can learn from it in later sessions
            "scientific", "common", "confidence", "file", "start_s", "audio_sha"]


# --------------------------------------------------------------------------- small helpers
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, default=str))
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def mtime(path: Path) -> float:
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0.0


def dir_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            with contextlib.suppress(OSError):
                total += os.path.getsize(os.path.join(root, f))
    return total


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def fmt_secs(s: float) -> str:
    if s is None or (isinstance(s, float) and math.isnan(s)):
        return "–"
    s = float(s)
    if s < 60:
        return f"{s:.0f} s"
    if s < 3600:
        return f"{s // 60:.0f} min {s % 60:02.0f} s"
    return f"{s // 3600:.0f} h {(s % 3600) // 60:02.0f} min"


# --------------------------------------------------------------------------- species labels
def labels_file() -> Path | None:
    spec = importlib.util.find_spec("birdnetlib")
    if not spec or not spec.submodule_search_locations:
        return None
    hits = sorted(Path(list(spec.submodule_search_locations)[0]).glob("models/analyzer/*Labels*.txt"))
    return hits[0] if hits else None


def load_labels() -> list[tuple[str, str]]:
    p = labels_file()
    if not p:
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if "_" in line:
            sci, common = line.split("_", 1)
            out.append((sci.strip(), common.strip()))
    return out


LABELS = load_labels()
LABEL_IDX = {sci: i for i, (sci, _) in enumerate(LABELS)}
COMMON_BY_SCI = {sci: c for sci, c in LABELS}
SCI_BY_COMMON = {c.lower(): sci for sci, c in LABELS}


def species_id(sci: str) -> str:
    i = LABEL_IDX.get(sci)
    return f"BN{i:04d}" if i is not None else "BN----"


def sci_code(sci: str) -> str:
    parts = re.sub(r"[^A-Za-z ]", "", str(sci)).split()
    if len(parts) >= 2:
        return (parts[0][:3] + parts[1][:3]).upper()
    return str(sci)[:6].upper()


def parse_label_choice(text: str) -> tuple[str, str] | None:
    """Accept 'Common name (Scientific name)', a scientific name or a common name."""
    text = (text or "").strip()
    m = re.match(r"^(.*)\((.*)\)\s*$", text)
    if m and m.group(2).strip() in COMMON_BY_SCI:
        sci = m.group(2).strip()
        return sci, COMMON_BY_SCI[sci]
    if text in COMMON_BY_SCI:
        return text, COMMON_BY_SCI[text]
    if text.lower() in SCI_BY_COMMON:
        sci = SCI_BY_COMMON[text.lower()]
        return sci, COMMON_BY_SCI[sci]
    return None


# --------------------------------------------------------------------------- audio metadata
def read_guano(path: Path) -> dict:
    """Read the GUANO metadata chunk (bat/bird recorder standard) from a WAV file."""
    out = {}
    try:
        with open(path, "rb") as fh:
            hdr = fh.read(12)
            if hdr[:4] != b"RIFF" or hdr[8:12] != b"WAVE":
                return out
            while True:
                ch = fh.read(8)
                if len(ch) < 8:
                    break
                cid, size = ch[:4], struct.unpack("<I", ch[4:])[0]
                if cid == b"guan":
                    for line in fh.read(size).decode("utf-8", "replace").splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            out[k.strip()] = v.strip()
                    break
                fh.seek(size + (size & 1), 1)
    except OSError:
        pass
    return out


def parse_start(name: str, guano: dict, path: Path) -> tuple[str, bool, str]:
    """Return (naive ISO start time, explicitly-UTC flag, source)."""
    ts = guano.get("Timestamp")
    if ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            is_utc = dt.tzinfo is not None and dt.utcoffset() == timedelta(0)
            if dt.tzinfo is not None and not is_utc:
                dt = dt.astimezone(timezone.utc)
                is_utc = True
            return dt.replace(tzinfo=None).isoformat(), is_utc, "GUANO timestamp"
        except ValueError:
            pass
    m = re.search(r"(\d{8})[_-]?(\d{6})", name)
    if m:
        with contextlib.suppress(ValueError):
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").isoformat(), False, "file name"
    return datetime.fromtimestamp(mtime(path)).replace(microsecond=0).isoformat(), False, "file modified time"


def file_sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def inspect_file(path: Path) -> dict:
    size = path.stat().st_size
    rec = dict(name=path.name, size=size, status="ok", duration_s=None, samplerate=None,
               channels=None, start=None, start_is_utc=False, start_source=None,
               lat=None, lon=None, note=None, device=None, sha1=None, problem="")
    if size == 0:
        rec.update(status="empty", problem="0 bytes — recorder wrote no audio")
        return rec
    try:
        info = sf.info(str(path))
        rec.update(duration_s=round(info.frames / info.samplerate, 2), samplerate=info.samplerate,
                   channels=info.channels)
        if info.frames == 0:
            rec.update(status="empty", problem="no audio frames")
    except Exception as e:  # noqa: BLE001
        rec.update(status="unreadable", problem=f"cannot decode: {type(e).__name__}")
    g = read_guano(path) if path.suffix.lower() == ".wav" else {}
    rec["start"], rec["start_is_utc"], rec["start_source"] = parse_start(path.name, g, path)
    pos = g.get("Loc Position", "").split()
    if len(pos) == 2:
        with contextlib.suppress(ValueError):
            rec["lat"], rec["lon"] = float(pos[0]), float(pos[1])
    rec["note"] = g.get("Note")
    rec["device"] = " ".join(x for x in (g.get("Make"), g.get("Model"), g.get("SR|Device_ID")) if x) or None
    rec["sha1"] = file_sha1(path)
    return rec


def build_manifest(ws: Path) -> list[dict]:
    ws = Path(ws)
    old = {m["name"]: m for m in read_json(ws / "manifest.json", []) or []}
    out = []
    for p in sorted((ws / "audio").glob("*")):
        if p.suffix.lower() not in AUDIO_EXT:
            continue
        m = old.get(p.name)
        if not m or m.get("size") != p.stat().st_size:
            m = inspect_file(p)
        out.append(m)
    write_json(ws / "manifest.json", out)
    return out


def local_start(m: dict, ts_mode: str, tz: str) -> datetime | None:
    if not m.get("start"):
        return None
    dt = datetime.fromisoformat(m["start"])
    if ts_mode == "utc":
        return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz)).replace(tzinfo=None)
    return dt


# --------------------------------------------------------------------------- signal measures
def _frames(y: np.ndarray, n: int, hop: int) -> np.ndarray:
    if len(y) < n:
        y = np.pad(y, (0, n - len(y)))
    return np.lib.stride_tricks.sliding_window_view(y, n)[::hop]


def power_spec(y: np.ndarray, n: int = 1024, hop: int = 512, max_frames: int | None = None) -> np.ndarray:
    fr = _frames(y, n, hop)
    if max_frames and len(fr) > max_frames:
        fr = fr[np.linspace(0, len(fr) - 1, max_frames).astype(int)]
    return np.abs(np.fft.rfft(fr * np.hanning(n), axis=1)) ** 2  # frames x bins


def noise_profile(y: np.ndarray) -> np.ndarray:
    """Median power per frequency bin over the whole recording (steady hiss/background)."""
    return np.median(power_spec(y, hop=1024, max_frames=6000), axis=0)


def measure_freq(seg: np.ndarray, sr: int, noise: np.ndarray) -> dict:
    """Noise-subtracted dominant frequency and 5–95 % energy band of one detection window."""
    P = power_spec(seg).mean(axis=0)
    f = np.fft.rfftfreq(1024, 1 / sr)
    band = (f >= 300) & (f <= min(15000, sr / 2))
    excess = np.clip(P - noise, 0, None) * band
    snr = 10 * np.log10(max(P[band].sum(), 1e-20) / max(noise[band].sum(), 1e-20))
    if excess.sum() <= 0:
        return dict(peak_hz=np.nan, low_hz=np.nan, high_hz=np.nan, snr_db=round(snr, 1))
    c = np.cumsum(excess) / excess.sum()
    return dict(peak_hz=int(f[np.argmax(excess)]), low_hz=int(f[np.searchsorted(c, 0.05)]),
                high_hz=int(f[min(np.searchsorted(c, 0.95), len(f) - 1)]), snr_db=round(snr, 1))


def render_clip_png(y: np.ndarray, sr: int, d0: float, d1: float, out: Path, title: str) -> None:
    fig = plt.figure(figsize=(4.8, 2.7), dpi=90)
    gs = fig.add_gridspec(2, 1, height_ratios=[1, 3], hspace=0.06)
    ax1, ax2 = fig.add_subplot(gs[0]), fig.add_subplot(gs[1])
    t = np.arange(len(y)) / sr
    ax1.plot(t, y, lw=0.5, color=BLUE)
    ax1.set_xlim(0, t[-1] if len(t) else 1)
    ax1.axis("off")
    ax1.set_title(title, loc="left", fontsize=8, color=INK)
    n, hop = 512, 128
    S = power_spec(y, n, hop)
    db = 10 * np.log10(S.T + 1e-12)
    fmax = min(12000, sr / 2)
    vis = db[: max(1, int(len(db) * fmax / (sr / 2)))]
    vmin, vmax = np.percentile(vis, 50), np.percentile(vis, 99.7)
    ax2.imshow(db, origin="lower", aspect="auto", cmap=SEQ_CMAP, vmin=vmin, vmax=max(vmax, vmin + 1),
               extent=[0, len(y) / sr, 0, sr / 2 / 1000])
    ax2.set_ylim(0, fmax / 1000)
    for x in (d0, d1):
        ax2.axvline(x, color=INK2, lw=0.8, ls="--")
    ax2.set_xlabel("seconds", fontsize=7)
    ax2.set_ylabel("kHz", fontsize=7)
    ax2.tick_params(labelsize=7)
    fig.savefig(out, bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)


# --------------------------------------------------------------------------- location / site
def file_location(m: dict, loc_mode: str, man_lat, man_lon) -> tuple:
    """Coordinates for one recording. Each file keeps its own GPS, so multi-site uploads stay separate.
    manual = typed coordinates for every file; otherwise GUANO first, typed coordinates fill files without GPS."""
    if loc_mode == "manual" and man_lat is not None and man_lon is not None:
        return float(man_lat), float(man_lon), "manual"
    if m.get("lat") is not None and m.get("lon") is not None:
        return float(m["lat"]), float(m["lon"]), "GUANO"
    if man_lat is not None and man_lon is not None:
        return float(man_lat), float(man_lon), "manual (file had no GPS)"
    return None, None, "unknown"


def site_label(m: dict, lat, lon) -> str:
    name = m.get("note") or (m.get("device") or "").split(" ")[-1] or "site"
    return f"{name} ({lat:.4f}, {lon:.4f})" if lat is not None and lon is not None else name


# --------------------------------------------------------------------------- worker
def _status(job: Path, st: dict) -> None:
    st["updated"] = now_iso()
    write_json(job / "status.json", st)


def _acquire_slot(job: Path, st: dict):
    slots = CACHE_ROOT / ".slots"
    slots.mkdir(exist_ok=True)
    while True:
        for i in range(MAX_JOBS):
            fh = open(slots / f"slot{i}.lock", "w")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fh
            except BlockingIOError:
                fh.close()
        if (job / "cancel").exists():
            return None
        st.update(state="queued", message="Waiting for a free processing slot (other users are processing)…")
        _status(job, st)
        time.sleep(2)


def run_job(ws: Path) -> None:
    ws = Path(ws)
    job = ws / "job"
    params = read_json(job / "params.json", {})
    manifest = read_json(ws / "manifest.json", []) or []
    files = [m for m in manifest if m["status"] == "ok"]
    st = dict(state="queued", stage="queued", n_files=len(files), n_skipped=len(manifest) - len(files),
              done=0, pct=0.0, current="", message="Queued…",
              started=now_iso(), finished=None, log=[], n_detections=0)
    _status(job, st)
    slot = _acquire_slot(job, st)
    if slot is None:
        st.update(state="cancelled", message="Cancelled before start.", finished=now_iso())
        _status(job, st)
        return
    t_start = time.time()
    try:
        st.update(state="running", stage="loading", message="Loading BirdNET model…")
        _status(job, st)
        with contextlib.redirect_stdout(io.StringIO()):
            from birdnetlib import Recording
            from birdnetlib.analyzer import Analyzer
            analyzer = Analyzer()
        clips = job / "clips"
        clips.mkdir(exist_ok=True)
        rows, file_rows = [], []
        for m in manifest:
            if m["status"] != "ok":
                file_rows.append(dict(file=m["name"], status=m["status"], start_local=None,
                                      duration_s=0, detections=0, problem=m.get("problem", "")))
        for i, m in enumerate(files):
            if (job / "cancel").exists():
                st.update(state="cancelled", message=f"Cancelled after {i} of {len(files)} files.",
                          finished=now_iso())
                _status(job, st)
                return
            t0 = time.time()
            st.update(stage="analysing", current=m["name"], pct=round(100 * i / max(len(files), 1), 1),
                      message=f"Analysing file {i + 1} of {len(files)}: {m['name']}")
            _status(job, st)
            start = local_start(m, params.get("ts_mode", "local"), params.get("tz", DEFAULT_TZ))
            kw = dict(min_conf=float(params.get("min_conf", 0.1)),
                      sensitivity=float(params.get("sensitivity", 1.0)),
                      overlap=float(params.get("overlap", 0.0)))
            loc = params.get("loc_mode", "guano")
            lat, lon, loc_src = file_location(m, loc, params.get("lat"), params.get("lon"))
            site = site_label(m, lat, lon)
            where = dict(site=site, lat=lat, lon=lon, location_source=loc_src, device=m.get("device") or "")
            if loc != "none" and lat is not None and lon is not None:
                kw.update(lat=float(lat), lon=float(lon), date=start.date() if start else None)
            path = ws / "audio" / m["name"]
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rec = Recording(analyzer, str(path), **kw)
                    rec.analyze()
                y, sr = sf.read(str(path), dtype="float32", always_2d=True)
                y = y.mean(axis=1)
            except Exception as e:  # noqa: BLE001
                st["log"].append(dict(file=m["name"], status="error", detections=0,
                                      seconds=round(time.time() - t0, 1), note=str(e)[:120]))
                file_rows.append(dict(file=m["name"], status="error", start_local=None, duration_s=0,
                                      detections=0, problem=str(e)[:120], **where))
                continue
            noise = noise_profile(y)
            for d in rec.detections:
                s, e = float(d["start_time"]), float(d["end_time"])
                sci, common = d["scientific_name"], d["common_name"]
                det_id = hashlib.sha1(f"{m['sha1']}|{s:.2f}|{sci}".encode()).hexdigest()[:12]
                a, b = max(0, int((s - CLIP_PAD_S) * sr)), min(len(y), int((e + CLIP_PAD_S) * sr))
                clip = y[a:b]
                peak = float(np.abs(clip).max()) if len(clip) else 0
                sf.write(str(clips / f"{det_id}.wav"), clip * (0.9 / peak) if peak > 0 else clip, sr,
                         subtype="PCM_16")
                dt = start + timedelta(seconds=s) if start else None
                render_clip_png(clip, sr, s - a / sr, e - a / sr, clips / f"{det_id}.png",
                                f"{common}  ·  {d['confidence']:.2f}")
                rows.append(dict(det_id=det_id, file=m["name"], audio_sha=m["sha1"], start_s=s, end_s=e,
                                 datetime=dt.isoformat(sep=" ", timespec="seconds") if dt else "",
                                 date=dt.date().isoformat() if dt else "", hour=dt.hour if dt else "",
                                 species_id=species_id(sci), sci_code=sci_code(sci), scientific=sci,
                                 common=common, confidence=round(float(d["confidence"]), 4),
                                 **measure_freq(y[int(s * sr):int(e * sr)], sr, noise), **where))
            n_det = len(rec.detections)
            st["log"].append(dict(file=m["name"], status="analysed", detections=n_det,
                                  seconds=round(time.time() - t0, 1), note=""))
            file_rows.append(dict(file=m["name"], status="analysed",
                                  start_local=start.isoformat(sep=" ") if start else None,
                                  duration_s=len(y) / sr, detections=n_det, problem="", **where))
            pd.DataFrame(rows, columns=DET_COLS).to_csv(job / "detections.csv", index=False)
            pd.DataFrame(file_rows).to_csv(job / "files.csv", index=False)
            st.update(done=i + 1, n_detections=len(rows))
        st.update(stage="finishing", message="Saving results…")
        _status(job, st)
        pd.DataFrame(rows, columns=DET_COLS).to_csv(job / "detections.csv", index=False)
        pd.DataFrame(file_rows).to_csv(job / "files.csv", index=False)
        st.update(state="done", stage="done", pct=100.0, current="", finished=now_iso(),
                  message=f"Done: {len(rows)} detections in {len(files)} files ({fmt_secs(time.time() - t_start)}).")
        _status(job, st)
    except Exception as e:  # noqa: BLE001
        st.update(state="error", message=f"Processing failed: {type(e).__name__}: {e}", finished=now_iso())
        _status(job, st)
        raise
    finally:
        slot.close()


# --------------------------------------------------------------------------- cache management
def touch_alive(ws: Path) -> None:
    with contextlib.suppress(OSError):
        (ws / ".alive").touch()


def cache_usage() -> int:
    return dir_size(CACHE_ROOT)


def sweep_cache(keep: str | None = None) -> None:
    """Delete orphaned workspaces (no heartbeat for ORPHAN_TTL_S, e.g. after a server crash)."""
    now = time.time()
    for d in CACHE_ROOT.iterdir():
        if not d.is_dir() or d.name.startswith(".") or d.name == keep:
            continue
        if now - mtime(d / ".alive") > ORPHAN_TTL_S:
            shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- analysis (shared by UI + report)
def load_det(ws: Path) -> pd.DataFrame:
    p = Path(ws) / "job" / "detections.csv"
    if not p.exists() or p.stat().st_size == 0:
        return pd.DataFrame(columns=DET_COLS)
    d = pd.read_csv(p)
    for c in DET_COLS:
        if c not in d:
            d[c] = np.nan
    d["datetime"] = pd.to_datetime(d["datetime"], errors="coerce")
    d["site"] = d["site"].fillna("site")
    return d


def load_files(ws: Path) -> pd.DataFrame:
    p = Path(ws) / "job" / "files.csv"
    if not p.exists():
        return pd.DataFrame(columns=["file", "status", "start_local", "duration_s", "detections", "problem"])
    f = pd.read_csv(p)
    f["start_local"] = pd.to_datetime(f["start_local"], errors="coerce")
    return f


def load_ver(ws: Path) -> pd.DataFrame:
    p = Path(ws) / "verifications.csv"
    if not p.exists():
        return pd.DataFrame(columns=VER_COLS)
    v = pd.read_csv(p, dtype=str).fillna("")
    for c in VER_COLS:
        if c not in v:
            v[c] = ""
    v = v.sort_values("timestamp_utc", kind="stable").drop_duplicates("det_id", keep="last")
    return v[v["decision"] != "clear"][VER_COLS]


def append_ver(ws: Path, row: dict) -> None:
    p = Path(ws) / "verifications.csv"
    new = not p.exists()
    with open(p, "a", newline="") as fh:
        pd.DataFrame([row], columns=VER_COLS).to_csv(fh, header=new, index=False)


def ver_row(det_row, decision: str, new_sci: str = "", new_common: str = "", verifier: str = "") -> dict:
    """A verification record that carries the detection's context (needed by the site profile)."""
    return dict(det_id=det_row["det_id"], decision=decision, new_scientific=new_sci, new_common=new_common,
                verifier=verifier, note="", timestamp_utc=now_iso(), scientific=det_row["scientific"],
                common=det_row["common"], confidence=det_row["confidence"], file=det_row["file"],
                start_s=det_row["start_s"], audio_sha=det_row["audio_sha"])


# --------------------------------------------------------------------------- site profile (learns from verifications)
PROFILE_VERSION = 1


def load_profile(ws: Path) -> dict:
    p = read_json(Path(ws) / "profile.json", None) or {}
    p.setdefault("name", "")
    p.setdefault("species", {})
    return p


def save_profile(ws: Path, prof: dict) -> None:
    write_json(Path(ws) / "profile.json", prof)


def reliability(det_all: pd.DataFrame, ver: pd.DataFrame, prof: dict, target: float = 0.9,
                min_n: int = 5) -> pd.DataFrame:
    """Per-species BirdNET precision from human checks, and the lowest threshold that reaches the target.

    A check is 'correct' when the reviewer confirmed BirdNET's species; reject or relabel count as wrong.
    Uses this session's checks plus any loaded from a site profile."""
    cols = ["scientific", "common", "species_id", "checked", "correct", "wrong", "precision", "learned_threshold",
            "manual_threshold", "action", "effective_threshold", "status"]
    v = ver[ver["decision"].isin(["confirm", "reject", "relabel"])].copy()
    if not det_all.empty and not v.empty:  # fill context for checks recorded before it was stored
        ctx = det_all.set_index("det_id")[["scientific", "common", "confidence"]]
        for c in ("scientific", "common", "confidence"):
            miss = v[c].astype(str).isin(["", "nan"])
            v.loc[miss, c] = v.loc[miss, "det_id"].map(ctx[c])
    v["confidence"] = pd.to_numeric(v["confidence"], errors="coerce")
    v = v.dropna(subset=["confidence"])
    v = v[v["scientific"].astype(str) != ""]
    rules = prof.get("species", {})
    names = dict(zip(v["scientific"], v["common"]))
    names.update({k: r.get("common", COMMON_BY_SCI.get(k, k)) for k, r in rules.items()})
    grid = np.round(np.arange(0.05, 0.951, 0.05), 2)
    rows = []
    for sci in sorted(names, key=lambda k: names[k]):
        g = v[v["scientific"] == sci]
        ok = (g["decision"] == "confirm").to_numpy()
        conf = g["confidence"].to_numpy()
        learned = None
        for t in grid:
            m = conf >= t - 1e-9
            if m.sum() >= min_n and ok[m].mean() >= target:
                learned = float(t)
                break
        rule = rules.get(sci, {})
        manual = rule.get("threshold")
        action = rule.get("action", "allow")
        eff = None if action == "block" else (manual if manual is not None else learned)
        if action == "block":
            status = "blocked — never reported unless confirmed"
        elif manual is not None:
            status = f"manual threshold {manual:.2f}"
        elif learned is not None:
            status = f"learned: ≥{learned:.2f} gives ≥{target:.0%} correct"
        elif len(g) < min_n:
            status = f"needs {min_n - len(g)} more check(s)"
        else:
            status = f"never reaches {target:.0%} correct — consider blocking"
        rows.append(dict(scientific=sci, common=names[sci], species_id=species_id(sci), checked=len(g),
                         correct=int(ok.sum()), wrong=int((~ok).sum()),
                         precision=round(float(ok.mean()), 2) if len(g) else np.nan,
                         learned_threshold=learned, manual_threshold=manual, action=action,
                         effective_threshold=eff, status=status))
    return pd.DataFrame(rows, columns=cols)


def species_rules(rel: pd.DataFrame) -> tuple[dict, list]:
    """(thresholds by scientific name, blocked scientific names) for filter_det."""
    if rel.empty:
        return {}, []
    th = {r.scientific: float(r.effective_threshold) for r in rel.itertuples() if pd.notna(r.effective_threshold)}
    return th, rel.loc[rel["action"] == "block", "scientific"].tolist()


def export_profile(ws: Path, det_all: pd.DataFrame, ver: pd.DataFrame, target: float, min_n: int) -> dict:
    prof = load_profile(ws)
    rel = reliability(det_all, ver, prof, target, min_n)
    sites = sorted(set(det_all["site"].dropna())) if not det_all.empty else []
    return dict(
        profile_version=PROFILE_VERSION, app=APP_TITLE, app_version=APP_VERSION, name=prof.get("name", ""),
        updated_utc=now_iso(), target_precision=target, min_checks=min_n, sites=sites,
        species={r.scientific: dict(common=r.common, threshold=r.manual_threshold, action=r.action,
                                    learned_threshold=r.learned_threshold, checked=r.checked, correct=r.correct)
                 for r in rel.itertuples()},
        verifications=ver.replace({np.nan: None}).to_dict("records"),
    )


def import_profile(ws: Path, data: dict) -> dict:
    """Merge a downloaded site profile into this workspace: rules + past verifications (latest wins)."""
    if not isinstance(data, dict) or data.get("profile_version") != PROFILE_VERSION:
        raise ValueError("Not a Bruting Calls site profile (profile_version mismatch).")
    prof = load_profile(ws)
    prof["name"] = data.get("name") or prof["name"]
    for sci, r in (data.get("species") or {}).items():
        if r.get("threshold") is not None or r.get("action", "allow") != "allow":
            prof["species"][sci] = dict(common=r.get("common", ""), threshold=r.get("threshold"),
                                        action=r.get("action", "allow"))
    save_profile(ws, prof)
    old = pd.DataFrame(data.get("verifications") or [], columns=VER_COLS).fillna("")
    cur = load_ver(ws)
    allv = pd.concat([old, cur], ignore_index=True).sort_values("timestamp_utc", kind="stable")
    allv[VER_COLS].to_csv(Path(ws) / "verifications.csv", index=False)
    return dict(name=prof["name"], rules=len(prof["species"]), verifications=len(old))


def set_rule(ws: Path, sci: str, action: str, value: float | None = None) -> None:
    """action: threshold | block | allow (clears the rule)."""
    prof = load_profile(ws)
    if action == "allow":
        prof["species"].pop(sci, None)
    elif action == "block":
        prof["species"][sci] = dict(common=COMMON_BY_SCI.get(sci, sci), threshold=None, action="block")
    else:
        prof["species"][sci] = dict(common=COMMON_BY_SCI.get(sci, sci), threshold=float(value), action="allow")
    save_profile(ws, prof)


def with_decisions(det: pd.DataFrame, ver: pd.DataFrame) -> pd.DataFrame:
    d = det.merge(ver[["det_id", "decision", "new_scientific", "new_common", "verifier"]], on="det_id", how="left")
    d["decision"] = d["decision"].fillna("pending").replace("", "pending")
    d["original_common"] = d["common"]
    d["original_scientific"] = d["scientific"]
    rl = d["decision"] == "relabel"
    if rl.any():
        d.loc[rl, "scientific"] = d.loc[rl, "new_scientific"]
        d.loc[rl, "common"] = d.loc[rl, "new_common"]
        d.loc[rl, "species_id"] = d.loc[rl, "scientific"].map(species_id)
        d.loc[rl, "sci_code"] = d.loc[rl, "scientific"].map(sci_code)
    return d


def filter_det(det: pd.DataFrame, ver: pd.DataFrame, s: dict) -> pd.DataFrame:
    d = with_decisions(det, ver)
    d = d[d["decision"] != "reject"]
    verified = d["decision"].isin(["confirm", "relabel"])
    thr = pd.Series(float(s.get("thr", 0.5)), index=d.index)
    if s.get("per_species", True):  # site-profile rules apply to BirdNET's own label
        th_map = s.get("thresholds") or {}
        thr = d["original_scientific"].map(th_map).fillna(thr)
    keep = d["confidence"] >= thr
    if s.get("per_species", True) and s.get("blocked"):
        keep &= ~d["original_scientific"].isin(s["blocked"])
    if s.get("override", True):
        keep |= verified
    if s.get("verified_only", False):
        keep &= verified
    d = d[keep]
    if s.get("species"):
        d = d[d["common"].isin(s["species"])]
    return d.sort_values("datetime")


def call_events(d: pd.DataFrame, gap: float = 0.0) -> pd.DataFrame:
    """Merge consecutive/overlapping 3-s detection windows of one species in one file into call events."""
    cols = ["common", "scientific", "species_id", "file", "site", "lat", "lon", "start_dt", "end_dt", "start_s",
            "end_s", "duration_s", "windows", "max_conf", "gap_to_next_s", "first_det_id", "verified"]
    if d.empty:
        return pd.DataFrame(columns=cols)
    out = []
    for (f, sp), g in d.sort_values("start_s").groupby(["file", "common"], sort=False):
        cur = None
        for r in g.itertuples():
            if cur and r.start_s <= cur["end_s"] + gap:
                cur["verified"] = cur["verified"] or r.decision in ("confirm", "relabel")
                cur["end_s"] = max(cur["end_s"], r.end_s)
                cur["windows"] += 1
                cur["max_conf"] = max(cur["max_conf"], r.confidence)
            else:
                if cur:
                    out.append(cur)
                cur = dict(common=sp, scientific=r.scientific, species_id=r.species_id, file=f,
                           site=r.site, lat=r.lat, lon=r.lon, first_det_id=r.det_id,
                           verified=r.decision in ("confirm", "relabel"),
                           dt0=r.datetime - pd.Timedelta(seconds=r.start_s) if pd.notna(r.datetime) else pd.NaT,
                           start_s=r.start_s, end_s=r.end_s, windows=1, max_conf=r.confidence)
        out.append(cur)
    ev = pd.DataFrame(out)
    ev["start_dt"] = ev["dt0"] + pd.to_timedelta(ev["start_s"], unit="s")
    ev["end_dt"] = ev["dt0"] + pd.to_timedelta(ev["end_s"], unit="s")
    ev["duration_s"] = ev["end_s"] - ev["start_s"]
    ev = ev.sort_values(["common", "file", "start_s"])
    nxt = ev.groupby(["common", "file"])["start_s"].shift(-1)
    ev["gap_to_next_s"] = (nxt - ev["end_s"]).round(1)
    return ev[cols].sort_values("start_dt").reset_index(drop=True)


def recorded_hours(files: pd.DataFrame) -> float:
    a = files[files["status"] == "analysed"] if not files.empty else files
    return float(a["duration_s"].sum()) / 3600 if not a.empty else 0.0


def species_summary(d: pd.DataFrame, ev: pd.DataFrame, det_all: pd.DataFrame, ver: pd.DataFrame,
                    hours: float) -> pd.DataFrame:
    cols = ["species_id", "sci_code", "common", "scientific", "detections", "call_events",
            "calls_per_hour", "total_call_s", "mean_call_s", "median_gap_s", "min_gap_s",
            "peak_hz", "low_hz", "high_hz", "first_heard", "last_heard", "days_heard", "sites",
            "max_conf", "mean_conf", "confirmed", "rejected", "verified_events"]
    if d.empty:
        return pd.DataFrame(columns=cols)
    dec = with_decisions(det_all, ver)
    rows = []
    for sp, g in d.groupby("common"):
        e = ev[ev["common"] == sp]
        orig = dec[dec["original_common"] == sp]
        rows.append(dict(
            species_id=g["species_id"].iloc[0], sci_code=g["sci_code"].iloc[0], common=sp,
            scientific=g["scientific"].iloc[0], detections=len(g), call_events=len(e),
            calls_per_hour=round(len(e) / hours, 2) if hours else np.nan,
            total_call_s=round(e["duration_s"].sum(), 1), mean_call_s=round(e["duration_s"].mean(), 1),
            median_gap_s=e["gap_to_next_s"].median(), min_gap_s=e["gap_to_next_s"].min(),
            peak_hz=g["peak_hz"].median(), low_hz=g["low_hz"].median(), high_hz=g["high_hz"].median(),
            first_heard=g["datetime"].min(), last_heard=g["datetime"].max(), days_heard=g["date"].nunique(),
            sites=g["site"].nunique(),
            max_conf=round(g["confidence"].max(), 3), mean_conf=round(g["confidence"].mean(), 3),
            confirmed=int((g["decision"].isin(["confirm", "relabel"])).sum()),
            verified_events=int(e["verified"].sum()) if "verified" in e else 0,
            rejected=int((orig["decision"] == "reject").sum()),
        ))
    s = pd.DataFrame(rows, columns=cols).sort_values(["call_events", "detections"], ascending=False)
    for c in ("peak_hz", "low_hz", "high_hz"):
        s[c] = s[c].round(0).astype("Int64")
    return s.reset_index(drop=True)


def rejected_table(det_all: pd.DataFrame, ver: pd.DataFrame, d: pd.DataFrame) -> pd.DataFrame:
    """Species with reviewer rejections — including ones that no longer appear in the results at all."""
    cols = ["common", "scientific", "rejected", "relabelled_away", "still_reported", "highest_rejected_conf"]
    dec = with_decisions(det_all, ver)
    bad = dec[dec["decision"].isin(["reject", "relabel"])]
    if bad.empty:
        return pd.DataFrame(columns=cols)
    rows = []
    for sp, g in bad.groupby("original_common"):
        rows.append(dict(common=sp, scientific=g["original_scientific"].iloc[0],
                         rejected=int((g["decision"] == "reject").sum()),
                         relabelled_away=int((g["decision"] == "relabel").sum()),
                         still_reported="yes" if sp in set(d["common"]) else "no — all its calls were rejected or below threshold",
                         highest_rejected_conf=round(float(g["confidence"].max()), 3)))
    return pd.DataFrame(rows, columns=cols).sort_values("rejected", ascending=False)


def daily_table(ev: pd.DataFrame, d: pd.DataFrame) -> pd.DataFrame:
    """Counts per day, per site, per species (sum over sites for an overall daily figure)."""
    if ev.empty:
        return pd.DataFrame(columns=["date", "site", "common", "call_events", "detections"])
    keys = ["date", "site", "common"]
    e = ev.assign(date=ev["start_dt"].dt.date.astype(str)).groupby(keys, dropna=False).size().rename("call_events")
    k = d.groupby(keys, dropna=False).size().rename("detections")
    return pd.concat([e, k], axis=1).fillna(0).astype(int).reset_index()


def sites_table(files: pd.DataFrame, d: pd.DataFrame, ev: pd.DataFrame) -> pd.DataFrame:
    cols = ["site", "lat", "lon", "location_source", "device", "files", "hours", "first_recording",
            "last_recording", "species", "detections", "call_events"]
    a = files[files["status"] == "analysed"] if not files.empty and "site" in files else pd.DataFrame()
    if a.empty:
        return pd.DataFrame(columns=cols)
    rows = []
    for site, g in a.groupby("site", dropna=False):
        dd, ee = d[d["site"] == site], ev[ev["site"] == site]
        rows.append(dict(site=site, lat=g["lat"].iloc[0], lon=g["lon"].iloc[0],
                         location_source=g["location_source"].iloc[0], device=g["device"].iloc[0], files=len(g),
                         hours=round(g["duration_s"].sum() / 3600, 2), first_recording=g["start_local"].min(),
                         last_recording=(g["start_local"] + pd.to_timedelta(g["duration_s"], unit="s")).max().floor("s"),
                         species=dd["common"].nunique(), detections=len(dd), call_events=len(ee)))
    return pd.DataFrame(rows, columns=cols)


def compute(det_all: pd.DataFrame, ver: pd.DataFrame, files: pd.DataFrame, s: dict) -> dict:
    d = filter_det(det_all, ver, s)
    ev = call_events(d, float(s.get("gap", 0.0)))
    hours = recorded_hours(files)
    return dict(det=d, ev=ev, hours=hours, files=files, ver=ver, det_all=det_all,
                summary=species_summary(d, ev, det_all, ver, hours), daily=daily_table(ev, d),
                sites=sites_table(files, d, ev), rejected=rejected_table(det_all, ver, d))


def review_pool(det_all: pd.DataFrame, ver: pd.DataFrame, lo: float, hi: float) -> pd.DataFrame:
    d = with_decisions(det_all, ver)
    return d[(d["confidence"] >= lo) & (d["confidence"] <= hi)]


def review_sample(pool: pd.DataFrame, n: int, order: str, seed: int, species: list, status: str) -> pd.DataFrame:
    d = pool
    if species:
        d = d[d["original_common"].isin(species)]
    if status == "pending":
        d = d[d["decision"] == "pending"]
    parts = []
    for _, g in d.groupby("original_common"):
        if order == "lowest":
            parts.append(g.nsmallest(n, "confidence"))
        elif order == "highest":
            parts.append(g.nlargest(n, "confidence"))
        elif order == "random":
            parts.append(g.sample(min(n, len(g)), random_state=seed))
        else:  # spread across the confidence range
            g = g.sort_values("confidence")
            parts.append(g.iloc[np.unique(np.linspace(0, len(g) - 1, min(n, len(g))).round().astype(int))])
    if not parts:
        return pool.iloc[0:0]
    return pd.concat(parts).sort_values(["original_common", "confidence"])


# --------------------------------------------------------------------------- plots (UI + PDF)
def _style(ax, grid="x"):
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.spines["left"].set_color(GRID)
    ax.spines["bottom"].set_color(GRID)
    if grid:
        ax.grid(axis=grid, color=GRID, lw=0.6)
        ax.set_axisbelow(True)


def _empty(ax, msg="No detections match the current filters"):
    ax.axis("off")
    ax.text(0.5, 0.5, msg, ha="center", va="center", color=MUTED, transform=ax.transAxes)


def plot_species(ax, summ: pd.DataFrame):
    if summ.empty:
        return _empty(ax)
    s = summ.sort_values("call_events").tail(30)
    y = np.arange(len(s))
    ver_n = s["verified_events"].fillna(0).astype(int) if "verified_events" in s else pd.Series(0, index=s.index)
    ax.barh(y, ver_n, height=0.6, color=BLUE_D, label="verified by listening")
    ax.barh(y, s["call_events"] - ver_n, left=ver_n, height=0.6, color=TEAL_MID, edgecolor=SURF, linewidth=1.5,
            label="not verified (BirdNET only)")
    ax.legend(loc="lower right", frameon=False, fontsize=8)
    ax.set_ylim(len(s) - 0.4 - max(len(s), 8), len(s) - 0.4)
    ax.xaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))
    ax.set_yticks(y, [f"{c}  ({i})" for c, i in zip(s["common"], s["species_id"])])
    for yi, v, n, vv in zip(y, s["call_events"], s["detections"], ver_n):
        ax.text(v, yi, f"  {v}  ({n} det.{', ' + str(vv) + ' verified' if vv else ''})", va="center", fontsize=8,
                color=INK2)
    ax.set_xlim(0, s["call_events"].max() * 1.35)
    ax.set_xlabel("Call events (detections merged when consecutive)")
    ax.set_title("Calls per species", loc="left")
    _style(ax, "x")


def plot_hourly(ax, ev: pd.DataFrame):
    if ev.empty or ev["start_dt"].isna().all():
        return _empty(ax)
    h = ev["start_dt"].dt.hour.value_counts().reindex(range(24), fill_value=0)
    ax.bar(h.index, h.values, width=0.8, color=BLUE)
    ax.set_xticks(range(0, 24, 2))
    ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))
    ax.set_xlabel("Hour of day (local)")
    ax.set_ylabel("Call events")
    ax.set_title("Activity by hour of day (all species)", loc="left")
    _style(ax, "y")


def plot_conf(ax, det_all: pd.DataFrame, thr: float):
    if det_all.empty:
        return _empty(ax, "No detections yet")
    bins = np.arange(0, 1.0001, 0.05)
    c, e = np.histogram(det_all["confidence"], bins=bins)
    ax.bar(e[:-1], c, width=0.045, align="edge", color=[BLUE if x >= thr - 1e-9 else BLUE_L for x in e[:-1]])
    ax.axvline(thr, color=INK, lw=1)
    ax.text(thr, ax.get_ylim()[1] * 0.95, f"  threshold {thr:.2f}", color=INK, fontsize=8, va="top")
    ax.set_xlabel("BirdNET confidence")
    ax.set_ylabel("Detections")
    ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))
    ax.set_title("Confidence distribution (dark = kept)", loc="left")
    _style(ax, "y")


def plot_timeline(ax, d: pd.DataFrame, files: pd.DataFrame):
    if d.empty or d["datetime"].isna().all():
        return _empty(ax)
    order = d.groupby("common")["datetime"].min().sort_values().index.tolist()
    ymap = {sp: i for i, sp in enumerate(order)}
    a = files[files["status"] == "analysed"] if not files.empty else files
    for r in a.itertuples():
        if pd.notna(r.start_local):
            ax.axvspan(r.start_local, r.start_local + pd.Timedelta(seconds=r.duration_s), color=NEUTRAL, lw=0, zorder=0)
    v = d["decision"].isin(["confirm", "relabel"]) if "decision" in d else pd.Series(False, index=d.index)
    size = 14 + 70 * d["confidence"]
    ax.scatter(d.loc[~v, "datetime"], d.loc[~v, "common"].map(ymap), s=size[~v], facecolor="white", edgecolor=BLUE,
               linewidth=1.4, zorder=3, label="BirdNET only")
    ax.scatter(d.loc[v, "datetime"], d.loc[v, "common"].map(ymap), s=size[v], color=BLUE_D, edgecolor="white",
               linewidth=0.8, zorder=4, label="verified by listening")
    ax.legend(loc="upper left", bbox_to_anchor=(1.0, 1.0), frameon=False, fontsize=8)
    ax.set_yticks(range(len(order)), order)
    ax.set_ylim(-0.7, len(order) - 0.3)
    loc = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    ax.set_title("When each species called (shaded = recorded periods; dot size = confidence; filled = verified)",
                 loc="left")
    _style(ax, "y")


def plot_daily(ax, daily: pd.DataFrame):
    if daily.empty:
        return _empty(ax)
    p = daily.pivot_table(index="common", columns="date", values="call_events", fill_value=0, aggfunc="sum")
    p = p.loc[p.sum(axis=1).sort_values().index]
    im = ax.imshow(p.values, cmap=SEQ_CMAP, aspect="auto", vmin=0)
    ax.set_yticks(range(len(p.index)), p.index)
    ax.set_xticks(range(len(p.columns)), p.columns, rotation=30, ha="right")
    vmax = p.values.max() or 1
    for i in range(p.shape[0]):
        for j in range(p.shape[1]):
            v = p.values[i, j]
            ax.text(j, i, int(v), ha="center", va="center", fontsize=8, color="white" if v > vmax * 0.55 else INK)
    ax.set_title("Call events per species per day", loc="left")
    for sp in ax.spines.values():
        sp.set_visible(False)
    plt.colorbar(im, ax=ax, fraction=0.03, pad=0.02).outline.set_visible(False)


def plot_freq(ax, summ: pd.DataFrame):
    s = summ.dropna(subset=["peak_hz"]) if not summ.empty else summ
    if s.empty:
        return _empty(ax)
    s = s.astype({"peak_hz": float, "low_hz": float, "high_hz": float}).sort_values("peak_hz")
    y = np.arange(len(s))
    ax.hlines(y, s["low_hz"] / 1000, s["high_hz"] / 1000, color=BLUE_L, lw=5, zorder=2)
    ax.scatter(s["peak_hz"] / 1000, y, color=BLUE_D, s=30, zorder=3, edgecolor="white", linewidth=1)
    for yi, pk in zip(y, s["peak_hz"]):
        ax.annotate(f"{pk / 1000:.1f} kHz", (pk / 1000, yi), xytext=(0, 7), textcoords="offset points",
                    ha="center", fontsize=7, color=INK2)
    ax.set_ylim(len(s) - 0.4 - max(len(s), 8), len(s) - 0.4)
    ax.set_yticks(y, s["common"])
    ax.set_xlabel("Frequency (kHz) — dot = median dominant frequency, bar = median 5–95 % energy band")
    ax.set_title("Call frequency (pitch) per species", loc="left")
    _style(ax, "x")


def plot_gaps(ax, ev: pd.DataFrame):
    g = ev["gap_to_next_s"].dropna() if not ev.empty else pd.Series(dtype=float)
    if g.empty:
        return _empty(ax, "No repeated calls within a recording — no intervals to show")
    ax.hist(g, bins=min(30, max(5, len(g))), color=BLUE, edgecolor="white")
    ax.set_xlabel("Silence between consecutive calls of the same species (s, within one recording)")
    ax.set_ylabel("Count")
    ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))
    ax.set_title("Time between calls", loc="left")
    _style(ax, "y")


# --------------------------------------------------------------------------- PDF report
A4P, A4L = (8.27, 11.69), (11.69, 8.27)


def _pdf_text_page(pdf, title: str, blocks: list[tuple[str, list[str]]], subtitle: str = ""):
    fig = plt.figure(figsize=A4P)
    fig.text(0.07, 0.95, title, fontsize=18, weight="bold", color=INK)
    y = 0.925
    if subtitle:
        fig.text(0.07, y, subtitle, fontsize=9, color=INK2)
        y -= 0.03
    for head, lines in blocks:
        y -= 0.015
        fig.text(0.07, y, head, fontsize=11, weight="bold", color=INK)
        y -= 0.022
        for ln in lines:
            fig.text(0.09, y, ln, fontsize=8.5, color=INK2, wrap=True)
            y -= 0.018
            if y < 0.05:
                pdf.savefig(fig)
                plt.close(fig)
                fig = plt.figure(figsize=A4P)
                y = 0.95
    pdf.savefig(fig)
    plt.close(fig)


def _pdf_table(pdf, df: pd.DataFrame, title: str, rows: int = 28, note: str = ""):
    if df.empty:
        df = pd.DataFrame({"": ["No rows"]})
    for i in range(0, len(df), rows):
        fig = plt.figure(figsize=A4L)
        page = f"  (rows {i + 1}–{min(i + rows, len(df))} of {len(df)})" if len(df) > rows else ""
        fig.text(0.04, 0.95, title + page, fontsize=13, weight="bold", color=INK)
        if note:
            fig.text(0.04, 0.925, note, fontsize=8, color=INK2)
        ax = fig.add_axes([0.03, 0.04, 0.94, 0.86])
        ax.axis("off")
        chunk = df.iloc[i:i + rows].astype(str).replace({"nan": "–", "NaT": "–", "None": "–", "<NA>": "–"})
        w = np.array([max([len(str(c))] + [len(str(x)) for x in chunk[c]]) for c in chunk.columns], float) + 2
        t = ax.table(cellText=chunk.values, colLabels=list(chunk.columns), loc="upper center",
                     cellLoc="left", colLoc="left", colWidths=list(w / w.sum()))
        t.auto_set_font_size(False)
        t.set_fontsize(7 if w.sum() < 170 else 6)
        t.scale(1, 1.3)
        for (r, _), cell in t.get_celld().items():
            cell.set_edgecolor(GRID)
            if r == 0:
                cell.set_facecolor(NEUTRAL)
                cell.set_text_props(weight="bold", color=INK)
        pdf.savefig(fig)
        plt.close(fig)


def _pdf_chart(pdf, fn, *args, h=1.0):
    fig, ax = plt.subplots(figsize=(A4L[0], A4L[1] * h))
    fn(ax, *args)
    fig.tight_layout(pad=1.5)
    pdf.savefig(fig)
    plt.close(fig)


def _pdf_examples(pdf, ws: Path, rows: pd.DataFrame, title: str):
    per = 8
    for i in range(0, len(rows), per):
        fig = plt.figure(figsize=A4P)
        fig.text(0.05, 0.965, title, fontsize=13, weight="bold", color=INK)
        for k, r in enumerate(rows.iloc[i:i + per].itertuples()):
            ax = fig.add_axes([0.05 + (k % 2) * 0.47, 0.72 - (k // 2) * 0.235, 0.43, 0.2])
            ax.axis("off")
            img = ws / "job" / "clips" / f"{r.det_id}.png"
            if img.exists():
                ax.imshow(plt.imread(str(img)))
            when = r.datetime.strftime("%Y-%m-%d %H:%M:%S") if pd.notna(r.datetime) else ""
            ax.set_title(f"{r.common} ({r.species_id}) · conf {r.confidence:.2f} · {r.decision}\n{when} · "
                         f"{r.file[-22:]} @ {r.start_s:.0f}s", fontsize=6.5, color=INK2, loc="left")
        pdf.savefig(fig)
        plt.close(fig)


def build_report(ws: Path, s: dict, out: Path) -> Path:
    ws = Path(ws)
    det_all, ver, files = load_det(ws), load_ver(ws), load_files(ws)
    params = read_json(ws / "job" / "params.json", {}) or {}
    manifest = read_json(ws / "manifest.json", []) or []
    R = compute(det_all, ver, files, s)
    d, ev, summ = R["det"], R["ev"], R["summary"]
    dec_all = with_decisions(det_all, ver)
    bad = [m for m in manifest if m["status"] != "ok"]
    dates = d["datetime"].dropna()
    loc = {"guano": "from recorder GUANO metadata", "manual": f"manual {params.get('lat')}, {params.get('lon')}",
           "none": "no location filter"}.get(params.get("loc_mode"), "–")
    lat_lon = sorted({(m.get("lat"), m.get("lon")) for m in manifest if m.get("lat") is not None})
    with PdfPages(out) as pdf:
        _pdf_text_page(pdf, f"{APP_TITLE} — bird call report", [
            ("Recordings", [
                f"Files uploaded: {len(manifest)}   ·   analysed: {int((files['status'] == 'analysed').sum()) if not files.empty else 0}"
                f"   ·   empty / unreadable / failed: {len(bad) + (int((files['status'] == 'error').sum()) if not files.empty else 0)}",
                f"Audio analysed: {R['hours']:.2f} h",
                f"Detection period: {dates.min():%Y-%m-%d %H:%M} → {dates.max():%Y-%m-%d %H:%M}" if len(dates) else "Detection period: –",
                f"Recorder location(s): {', '.join(f'{a:.5f}, {b:.5f}' for a, b in lat_lon) or 'not in metadata'}",
                *[f"⚠ {m['name']}: {m.get('problem', m['status'])}" for m in bad[:25]],
                *([f"⚠ … and {len(bad) - 25} more problem files"] if len(bad) > 25 else []),
            ]),
            ("Results at a glance", [
                f"Species: {d['common'].nunique()}   ·   detections: {len(d)}   ·   call events: {len(ev)}",
                f"Verified by a person: {int(dec_all['decision'].isin(['confirm', 'relabel']).sum())} confirmed/relabelled, "
                f"{int((dec_all['decision'] == 'reject').sum())} rejected, of {len(dec_all)} raw detections",
                *[f"• {r.common} ({r.scientific}, {r.species_id}/{r.sci_code}): {r.call_events} calls, {r.detections} detections, "
                  f"{r.calls_per_hour} calls/h, ~{(0 if pd.isna(r.peak_hz) else r.peak_hz) / 1000:.1f} kHz" for r in summ.head(20).itertuples()],
            ]),
            ("Settings", [
                f"BirdNET GLOBAL 6K V2.4 via birdnetlib · detection floor {params.get('min_conf')} · sensitivity {params.get('sensitivity')}"
                f" · overlap {params.get('overlap')} s · location: {loc}",
                f"Timestamps: {'UTC converted to ' + str(params.get('tz')) if params.get('ts_mode') == 'utc' else 'file times used as local clock time'}",
                (f"Per-species thresholds from the site profile '{load_profile(ws).get('name') or 'unnamed'}': "
                 f"{len(s.get('thresholds') or {})} species, {len(s.get('blocked') or [])} blocked "
                 f"(target {float(s.get('target', 0.9)):.0%} correct, min {s.get('min_n', 5)} checks)"
                 if s.get("per_species", True) and (s.get("thresholds") or s.get("blocked")) else
                 "Per-species thresholds: none (global threshold for all species)"),
                f"Report threshold: confidence ≥ {s.get('thr')} · verified detections always kept: {s.get('override', True)}"
                f" · verified only: {s.get('verified_only', False)} · merge gap {s.get('gap', 0)} s"
                + (f" · species filter: {', '.join(s['species'])}" if s.get("species") else ""),
            ]),
            ("How to read this report", [
                "Detection = one 3-second window in which BirdNET recognised the species at or above the threshold.",
                "Call event = consecutive/overlapping detections of the same species in one recording, merged.",
                "Gap to next = silence between the end of one call event and the start of the next, same species, same recording.",
                "Calls per hour = call events ÷ hours of audio analysed (all files, not only when the bird was present).",
                "Frequency = dominant frequency after subtracting each recording's median background noise; band = 5–95 % of call energy.",
                "IDs are automated. Low-confidence detections should be checked by ear — see the verification summary.",
            ]),
            ("Reproducibility & citation", [
                f"{APP_TITLE} v{APP_VERSION} ({APP_FAMILY}) · licence {APP_LICENCE}",
                *[f"Model file {n} · sha256 {h}" for n, h in model_files() if n.endswith(".tflite")],
                "Input files are identified by SHA-1 in the Files table; full session info is in the CSV bundle.",
                f"Cite as: {CITATION}",
            ]),
        ], subtitle=f"Generated {datetime.now():%Y-%m-%d %H:%M} · {APP_TITLE} v{APP_VERSION} · {APP_FAMILY}")
        show = summ.copy()
        for c in ("first_heard", "last_heard"):
            show[c] = pd.to_datetime(show[c]).dt.strftime("%m-%d %H:%M")
        _pdf_table(pdf, show[["species_id", "sci_code", "common", "scientific", "detections", "call_events",
                              "calls_per_hour", "total_call_s", "median_gap_s", "peak_hz", "low_hz", "high_hz",
                              "first_heard", "last_heard", "max_conf", "confirmed", "rejected"]],
                   "Species summary", note="IDs: BN#### = BirdNET class index; 6-letter code from the scientific name.")
        if not R["rejected"].empty:
            _pdf_table(pdf, R["rejected"], "Rejected by reviewer",
                       note="Rejected calls are excluded from every count; species listed here may be missing above.")
        st = R["sites"].copy()
        for c in ("first_recording", "last_recording"):
            st[c] = pd.to_datetime(st[c]).dt.strftime("%Y-%m-%d %H:%M")
        _pdf_table(pdf, st, "Recording sites",
                   note="Coordinates per file from recorder GPS (GUANO) unless typed in. Darwin Core occurrences: CSV bundle.")
        rel = reliability(det_all, ver, load_profile(ws), float(s.get("target", 0.9)), int(s.get("min_n", 5)))
        if not rel.empty:
            _pdf_table(pdf, rel[["common", "species_id", "checked", "correct", "wrong", "precision",
                                 "learned_threshold", "manual_threshold", "effective_threshold", "status"]],
                       "Per-species reliability (from human checks)",
                       note="precision = share of checked BirdNET calls that a person confirmed; "
                            "learned threshold = lowest confidence reaching the target.")
        _pdf_chart(pdf, plot_species, summ)
        _pdf_chart(pdf, plot_timeline, d, files)
        _pdf_chart(pdf, plot_daily, R["daily"])
        _pdf_table(pdf, R["daily"], "Daily counts (per site)")
        _pdf_chart(pdf, plot_hourly, ev, h=0.7)
        _pdf_chart(pdf, plot_freq, summ)
        _pdf_chart(pdf, plot_gaps, ev, h=0.7)
        _pdf_chart(pdf, plot_conf, det_all, float(s.get("thr", 0.5)), h=0.7)
        evs = ev.copy()
        evs["start"] = evs["start_dt"].dt.strftime("%Y-%m-%d %H:%M:%S")
        evs = evs[["common", "species_id", "start", "duration_s", "windows", "max_conf", "gap_to_next_s", "verified",
                   "site", "file"]]
        _pdf_table(pdf, evs.head(560), "Call timestamps & intervals",
                   note="First 560 call events; full list in the CSV download." if len(evs) > 560 else "")
        if not d.empty:
            best = d.sort_values("confidence", ascending=False).groupby("common").head(2).sort_values(["common", "confidence"])
            _pdf_examples(pdf, ws, best, "Example calls (top 2 per species)")
        v = dec_all[dec_all["decision"] != "pending"]
        if not v.empty:
            _pdf_table(pdf, v[["original_common", "confidence", "decision", "common", "verifier", "file", "start_s"]]
                       .rename(columns={"original_common": "BirdNET said", "common": "final label"}),
                       "Human verification log")
        if not files.empty:
            fs = files.copy()
            fs["start_local"] = fs["start_local"].dt.strftime("%Y-%m-%d %H:%M:%S")
            fs["duration_s"] = fs["duration_s"].round(0)
            _pdf_table(pdf, fs, "Files")
    return Path(out)


# --------------------------------------------------------------------------- Darwin Core (GBIF / ALA)
DWC_TERMS = ["occurrenceID", "basisOfRecord", "occurrenceStatus", "eventID", "eventDate", "year", "month", "day",
             "scientificName", "vernacularName", "taxonRank", "kingdom", "class", "decimalLatitude",
             "decimalLongitude", "geodeticDatum", "locationID", "locality", "samplingProtocol", "recordedBy",
             "identifiedBy", "identificationVerificationStatus", "identificationRemarks", "associatedMedia",
             "occurrenceRemarks", "dynamicProperties", "datasetName"]


def _iso_with_offset(ts, tz: str) -> str:
    if pd.isna(ts):
        return ""
    dt = ts.to_pydatetime().replace(microsecond=0)
    off = ZoneInfo(tz).utcoffset(dt)
    sign, mins = ("+" if off >= timedelta(0) else "-"), int(abs(off.total_seconds()) // 60)
    return f"{dt.isoformat()}{sign}{mins // 60:02d}:{mins % 60:02d}"


def darwin_core(R: dict, params: dict) -> pd.DataFrame:
    """One Darwin Core occurrence per call event (filtered, verified-aware). Rejected detections never appear."""
    ev = R["ev"]
    if ev.empty:
        return pd.DataFrame(columns=DWC_TERMS)
    tz = params.get("tz", DEFAULT_TZ)
    dev = R["det"].drop_duplicates("file").set_index("file")["device"].to_dict() if not R["det"].empty else {}
    rows = []
    for r in ev.itertuples():
        has_xy = pd.notna(r.lat) and pd.notna(r.lon)
        rows.append({
            "occurrenceID": f"urn:brutingcalls:{r.first_det_id}",
            "basisOfRecord": "MachineObservation",
            "occurrenceStatus": "present",
            "eventID": r.file,
            "eventDate": f"{_iso_with_offset(r.start_dt, tz)}/{_iso_with_offset(r.end_dt, tz)}",
            "year": r.start_dt.year if pd.notna(r.start_dt) else "",
            "month": r.start_dt.month if pd.notna(r.start_dt) else "",
            "day": r.start_dt.day if pd.notna(r.start_dt) else "",
            "scientificName": r.scientific, "vernacularName": r.common, "taxonRank": "species",
            "kingdom": "Animalia", "class": "Aves",
            "decimalLatitude": round(float(r.lat), 6) if has_xy else "",
            "decimalLongitude": round(float(r.lon), 6) if has_xy else "",
            "geodeticDatum": "WGS84" if has_xy else "",
            "locationID": r.site, "locality": r.site,
            "samplingProtocol": "Passive acoustic monitoring; automated identification with BirdNET GLOBAL 6K v2.4 "
                                f"(birdnetlib) in {APP_TITLE} v{APP_VERSION}",
            "recordedBy": dev.get(r.file, "") or "autonomous recording unit",
            "identifiedBy": "BirdNET GLOBAL 6K v2.4" + ("; verified by listening" if r.verified else ""),
            "identificationVerificationStatus": "verified by listening" if r.verified else "unverified (automated)",
            "identificationRemarks": f"BirdNET max confidence {r.max_conf:.3f}; {int(r.windows)} x 3-s window(s)",
            "associatedMedia": f"{r.file}#t={r.start_s:.0f},{r.end_s:.0f}",
            "occurrenceRemarks": f"Call event {r.duration_s:.0f} s"
                                 + ("" if pd.isna(r.gap_to_next_s) else f"; next call after {r.gap_to_next_s:.0f} s"),
            "dynamicProperties": json.dumps({"maxConfidence": round(float(r.max_conf), 4), "windows": int(r.windows),
                                             "durationSeconds": float(r.duration_s)}),
            "datasetName": f"{APP_TITLE} export",
        })
    return pd.DataFrame(rows, columns=DWC_TERMS)


def dwc_meta_xml() -> str:
    fields = "\n".join(f'    <field index="{i}" term="http://rs.tdwg.org/dwc/terms/{t}"/>' for i, t in enumerate(DWC_TERMS))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<archive xmlns="http://rs.tdwg.org/dwc/text/" metadata="eml.xml">
  <core encoding="UTF-8" fieldsTerminatedBy="," linesTerminatedBy="\\n" fieldsEnclosedBy='"' ignoreHeaderLines="1"
        rowType="http://rs.tdwg.org/dwc/terms/Occurrence">
    <files><location>occurrence.csv</location></files>
    <id index="0"/>
{fields}
  </core>
</archive>
"""


def dwc_eml_xml(R: dict) -> str:
    e = html.escape
    d = R["det"]["datetime"].dropna()
    start, end = (f"{d.min():%Y-%m-%d}", f"{d.max():%Y-%m-%d}") if len(d) else ("", "")
    sites = R["sites"]
    bbox = (sites["lat"].min(), sites["lat"].max(), sites["lon"].min(), sites["lon"].max()) \
        if not sites.empty and sites["lat"].notna().any() else None
    cov = (f"""      <geographicCoverage><geographicDescription>{e(", ".join(map(str, sites["site"])))}</geographicDescription>
        <boundingCoordinates><westBoundingCoordinate>{bbox[2]}</westBoundingCoordinate><eastBoundingCoordinate>{bbox[3]}</eastBoundingCoordinate>
        <northBoundingCoordinate>{bbox[1]}</northBoundingCoordinate><southBoundingCoordinate>{bbox[0]}</southBoundingCoordinate></boundingCoordinates>
      </geographicCoverage>""" if bbox else "")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<eml:eml xmlns:eml="https://eml.ecoinformatics.org/eml-2.2.0" packageId="brutingcalls-{now_iso()}" system="brutingcalls">
  <dataset>
    <title>{e(APP_TITLE)} acoustic bird occurrences ({start} to {end})</title>
    <creator><individualName><surName>{e(APP_AUTHOR)}</surName></individualName></creator>
    <pubDate>{datetime.now():%Y-%m-%d}</pubDate>
    <abstract><para>Bird occurrences detected in passive acoustic recordings with BirdNET GLOBAL 6K v2.4 and
      summarised by {e(APP_TITLE)} v{APP_VERSION}. One record per call event; low-confidence records may have been
      verified by listening (see identificationVerificationStatus).</para></abstract>
    <intellectualRights><para>Set the data licence before publishing (e.g. CC-BY 4.0). BirdNET model: CC BY-NC-SA 4.0.</para></intellectualRights>
    <coverage>
{cov}
      <temporalCoverage><rangeOfDates><beginDate><calendarDate>{start}</calendarDate></beginDate>
        <endDate><calendarDate>{end}</calendarDate></endDate></rangeOfDates></temporalCoverage>
    </coverage>
  </dataset>
</eml:eml>
"""


def export_zip(ws: Path, R: dict, s: dict, out: Path) -> Path:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("detections_filtered.csv", R["det"].to_csv(index=False))
        z.writestr("detections_all.csv", with_decisions(R["det_all"], R["ver"]).to_csv(index=False))
        z.writestr("species_summary.csv", R["summary"].to_csv(index=False))
        z.writestr("call_events.csv", R["ev"].to_csv(index=False))
        z.writestr("daily_counts.csv", R["daily"].to_csv(index=False))
        z.writestr("files.csv", R["files"].to_csv(index=False))
        z.writestr("sites.csv", R["sites"].to_csv(index=False))
        z.writestr("rejected_species.csv", R["rejected"].to_csv(index=False))
        tgt, mn = float(s.get("target", 0.9)), int(s.get("min_n", 5))
        z.writestr("reliability.csv", reliability(R["det_all"], R["ver"], load_profile(ws), tgt, mn).to_csv(index=False))
        z.writestr("site_profile.json", json.dumps(export_profile(ws, R["det_all"], R["ver"], tgt, mn), indent=1,
                                                   default=str))
        params = read_json(ws / "job" / "params.json", {}) or {}
        z.writestr("darwin_core/occurrence.csv", darwin_core(R, params).to_csv(index=False))
        z.writestr("darwin_core/meta.xml", dwc_meta_xml())
        z.writestr("darwin_core/eml.xml", dwc_eml_xml(R))
        z.writestr("verifications.csv", R["ver"].to_csv(index=False))
        z.writestr("settings.json", json.dumps(dict(app=APP_TITLE, app_version=APP_VERSION, exported_utc=now_iso(),
                                                    report=s, processing=read_json(ws / "job" / "params.json", {})),
                                               indent=2, default=str))
        z.writestr("files_manifest.csv", pd.DataFrame(read_json(ws / "manifest.json", []) or []).to_csv(index=False))
        z.writestr("data_dictionary.csv", pd.DataFrame(DATA_DICTIONARY, columns=["table", "column", "unit", "description"])
                   .to_csv(index=False))
        z.writestr("session_info.txt", session_info_text())
        z.writestr("CITATION.txt", CITATION + "\n")
    return Path(out)


# --------------------------------------------------------------------------- reproducibility
def model_files() -> list[tuple[str, str]]:
    """BirdNET model/label files shipped with birdnetlib, with short SHA-256 for provenance."""
    p = labels_file()
    if not p:
        return []
    out = []
    for f in sorted(p.parent.glob("*")):
        if f.suffix in (".tflite", ".txt"):
            out.append((f.name, hashlib.sha256(f.read_bytes()).hexdigest()[:16]))
    return out


def session_info_text(extra: list[str] | None = None) -> str:
    import platform
    from importlib import metadata
    pkgs = ["shiny", "birdnetlib", "tensorflow", "tflite-runtime", "numpy", "pandas", "matplotlib",
            "soundfile", "librosa", "resampy", "audioread"]
    lines = [f"App: {APP_TITLE} {APP_VERSION} ({APP_FAMILY})",
             f"Run time: {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC",
             f"Author: {APP_AUTHOR} · Licence: {APP_LICENCE}", "",
             "── Python session ──",
             f"Python: {platform.python_version()} ({platform.python_implementation()})",
             f"Platform: {platform.platform()} · {platform.machine()}", "", "Packages:"]
    for name in pkgs:
        with contextlib.suppress(metadata.PackageNotFoundError):
            lines.append(f"  {name:<16} {metadata.version(name)}")
    lines += ["", "BirdNET model files (name, sha256[:16]):"]
    lines += [f"  {n:<52} {h}" for n, h in model_files()] or ["  not found"]
    lines += ["", "Settings:", f"  cache limit {CACHE_LIMIT / 1024**3:.1f} GB · max jobs {MAX_JOBS} · "
              f"orphan TTL {ORPHAN_TTL_S / 3600:.1f} h · default TZ {DEFAULT_TZ}"]
    return "\n".join(lines + (extra or [])) + "\n"


def about_json() -> str:
    return json.dumps(dict(title=APP_TITLE, version=APP_VERSION, family=APP_FAMILY, author=APP_AUTHOR,
                           licence=APP_LICENCE, purpose=APP_PURPOSE, inputs=APP_INPUTS, citation=CITATION,
                           limits=[x.format(gb=CACHE_LIMIT / 1024**3, jobs=MAX_JOBS) for x in APP_LIMITS],
                           dictionary=[dict(table=a, column=b, unit=c, description=d) for a, b, c, d in DATA_DICTIONARY]))


# --------------------------------------------------------------------------- CLI
def cli(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "process":
        run_job(Path(argv[1]))
    elif cmd == "inspect":
        build_manifest(Path(argv[1]))
    elif cmd == "report":
        build_report(Path(argv[1]), read_json(Path(argv[2]), {}), Path(argv[3]))
    elif cmd == "zip":
        ws, s = Path(argv[1]), read_json(Path(argv[2]), {})
        export_zip(ws, compute(load_det(ws), load_ver(ws), load_files(ws), s), s, Path(argv[3]))
    elif cmd == "sessioninfo":
        sys.stdout.write(session_info_text())
    elif cmd == "about":
        print(about_json())
    elif cmd == "reliability":  # <ws> <target> <min_n>  -> JSON records
        ws = Path(argv[1])
        print(reliability(load_det(ws), load_ver(ws), load_profile(ws), float(argv[2]), int(argv[3]))
              .replace({np.nan: None}).to_json(orient="records"))
    elif cmd == "profile-import":  # <ws> <file.json>
        print(json.dumps(import_profile(Path(argv[1]), read_json(Path(argv[2]), {}))))
    elif cmd == "profile-export":  # <ws> <target> <min_n> <out.json> [name]
        ws = Path(argv[1])
        if len(argv) > 5:
            prof = load_profile(ws)
            prof["name"] = argv[5]
            save_profile(ws, prof)
        write_json(Path(argv[4]), export_profile(ws, load_det(ws), load_ver(ws), float(argv[2]), int(argv[3])))
    elif cmd == "profile-set":  # <ws> <scientific> <threshold|block|allow> [value]
        set_rule(Path(argv[1]), argv[2], argv[3], float(argv[4]) if len(argv) > 4 else None)
    elif cmd == "assets":  # theme CSS + page JS, so app.R uses exactly the same look and behaviour
        import importlib.util as _iu
        spec = _iu.spec_from_file_location("bc_ui", APP_FILE)
        mod = _iu.module_from_spec(spec)
        sys.modules["bc_ui"] = mod
        spec.loader.exec_module(mod)
        print(json.dumps(dict(css=mod.CSS, js=mod.JS, call=read_json(WWW_DIR / "king_parrot_call.json", None),
                              demo=len(list(DEMO_DIR.glob("*.wav"))) if DEMO_DIR.exists() else 0)))
    elif cmd == "labels":
        print(json.dumps([f"{c} ({s})" for s, c in LABELS]))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))


# =========================================================================== Shiny for Python UI
from shiny import App, reactive, render, ui  # noqa: E402

CSS = """
/* 70s pastel theme, picked from a King Parrot in a berry tree: tomato, mustard, avocado, teal, rose on cream. */
:root{
 --surf:#fbf3e4;--card:#fffbf3;--ink:#3b2a1e;--ink2:#6e5a48;--muted:#9a8670;--grid:#eadbc4;--neutral:#f3e8d6;
 --about:#e9dcc4;--about-d:#7a5c3e;--upload:#f8c9b4;--upload-d:#b24a26;--process:#f5de9a;--process-d:#8a6408;
 --verify:#d2dfae;--verify-d:#4f6b1f;--results:#b9ddd5;--results-d:#24665b;--report:#e7c3d2;--report-d:#80405e;
 --primary:#24665b;--primary-h:#1b4f46;--good:#4e7d2b;--warn:#a96d00;--bad:#b23a2a;
 --blue:var(--primary);--blue-d:var(--primary-h);--blue-l:#cfe7e1}
body{background:var(--surf);color:var(--ink);font-family:"Nunito",system-ui,-apple-system,"Segoe UI",sans-serif}
.bc-stripe{height:8px;border-radius:0 0 6px 6px;margin:0 -12px;background:linear-gradient(90deg,
 #c4532d 0 20%,#e39a3b 20% 40%,#e9c46a 40% 60%,#8fa858 60% 80%,#2f7469 80% 100%)}
.bc-header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:16px 4px 6px}
.bc-header h1{font-family:"Righteous","Nunito",sans-serif;font-size:2rem;font-weight:400;margin:0;color:var(--ink);letter-spacing:.01em}
.bc-header .sub{color:var(--ink2);font-size:.95rem}
.bc-header .tools{margin-left:auto;display:flex;gap:8px}
.bc-header .tools .btn{border-radius:999px;font-weight:700;font-size:.85rem;padding:4px 14px}
.bc-ver{font-size:.78rem;font-weight:800;background:var(--process);border:1px solid #e3c36a;border-radius:999px;padding:1px 10px;color:var(--process-d)}
.bc-warn{display:flex;gap:12px;align-items:center;flex-wrap:wrap;background:#fdecc8;border:1px solid #edcf86;
 border-radius:14px;padding:10px 14px;margin:8px 0 12px;font-size:.92rem;color:#5c3b00}
.bc-warn b{color:#3d2700}.bc-warn>span:nth-child(2){flex:1;min-width:260px}
.bc-warn a{color:var(--upload-d);font-weight:700}
/* workflow bar: each step owns a colour that its tab shares */
.bc-steps{display:flex;list-style:none;padding:0;margin:0 0 12px;gap:8px;overflow-x:auto}
.bc-step{--c:var(--about);--d:var(--about-d);flex:1;min-width:130px;display:flex;align-items:center;gap:10px;padding:9px 12px;
 cursor:pointer;border-radius:14px;background:var(--card);border:2px solid var(--grid);position:relative;overflow:hidden;transition:transform .12s}
.bc-step:hover{transform:translateY(-1px)}
.bc-step[data-goto=upload]{--c:var(--upload);--d:var(--upload-d)}.bc-step[data-goto=process]{--c:var(--process);--d:var(--process-d)}
.bc-step[data-goto=verify]{--c:var(--verify);--d:var(--verify-d)}.bc-step[data-goto=results]{--c:var(--results);--d:var(--results-d)}
.bc-step[data-goto=report]{--c:var(--report);--d:var(--report-d)}
.bc-step .dot{flex:0 0 32px;height:32px;border-radius:50%;display:grid;place-items:center;font-weight:800;font-size:.9rem;
 background:var(--neutral);color:var(--ink2);border:2px solid var(--grid)}
.bc-step .lbl{font-weight:800;font-size:.95rem;line-height:1.1}.bc-step .st{font-size:.78rem;color:var(--ink2)}
.bc-step.active{background:var(--c);border-color:var(--d)}.bc-step.active .dot{background:var(--d);color:#fff;border-color:var(--d)}
.bc-step.done{border-color:var(--c)}.bc-step.done .dot{background:var(--c);color:var(--d);border-color:var(--d)}
.bc-step.error{border-color:var(--bad)}.bc-step.error .dot{background:#f6d3cc;color:var(--bad);border-color:var(--bad)}
.bc-step .mini{position:absolute;left:0;bottom:0;height:4px;background:var(--d);transition:width .6s}
.bc-running .dot{animation:bcpulse 1.4s infinite}@keyframes bcpulse{50%{box-shadow:0 0 0 7px rgba(138,100,8,.18)}}
/* main tabs = coloured pills matching the steps */
.nav-tabs{border-bottom:none;gap:6px;margin-bottom:10px}
.nav-tabs .nav-link{--c:var(--about);--d:var(--about-d);border:2px solid transparent;border-radius:999px;font-weight:800;
 color:var(--d);background:transparent;padding:6px 16px}
.nav-tabs .nav-link[data-value=upload]{--c:var(--upload);--d:var(--upload-d)}.nav-tabs .nav-link[data-value=process]{--c:var(--process);--d:var(--process-d)}
.nav-tabs .nav-link[data-value=verify]{--c:var(--verify);--d:var(--verify-d)}.nav-tabs .nav-link[data-value=results]{--c:var(--results);--d:var(--results-d)}
.nav-tabs .nav-link:hover{background:var(--c);border-color:transparent;color:var(--ink)}
.nav-tabs .nav-link.active,.nav-tabs .nav-link.active:focus{background:var(--c);border-color:var(--d);color:var(--ink)}
.nav-link:focus-visible{box-shadow:0 0 0 3px rgba(36,102,91,.35);outline:none}
/* results sub-tabs: same pill language, teal family */
.bc-subtabs .nav-underline,.bc-subtabs .nav{gap:4px;flex-wrap:wrap}
.bc-subtabs .nav-link{border-radius:999px;padding:4px 12px;font-weight:700;color:var(--results-d);border:2px solid transparent}
.bc-subtabs .nav-link:hover{background:#e3f1ed}
.bc-subtabs .nav-link.active,.bc-subtabs .nav-underline .nav-link.active{background:var(--results);border-color:var(--results-d);
 color:var(--ink);border-bottom-width:2px}
.bc-subtabs .nav-link[data-value=report]{color:var(--report-d)}.bc-subtabs .nav-link[data-value=report].active{background:var(--report);border-color:var(--report-d)}
/* surfaces & controls */
.card,.bslib-sidebar-layout{border-color:var(--grid)!important;border-radius:16px;background:var(--card)}
.card-header{background:transparent;font-weight:800;color:var(--ink);border-bottom:1px dashed var(--grid)}
.bslib-sidebar-layout>.sidebar{background:#f7ecd8}
.btn-primary{--bs-btn-bg:var(--primary);--bs-btn-border-color:var(--primary);--bs-btn-hover-bg:var(--primary-h);
 --bs-btn-hover-border-color:var(--primary-h);--bs-btn-active-bg:var(--primary-h);border-radius:999px;font-weight:800}
.btn-outline-primary{--bs-btn-color:var(--primary);--bs-btn-border-color:var(--primary);--bs-btn-hover-bg:var(--primary);
 --bs-btn-hover-border-color:var(--primary);border-radius:999px;font-weight:700}
.btn-outline-secondary{border-radius:999px}
.irs--shiny .irs-bar,.irs--shiny .irs-single,.irs--shiny .irs-from,.irs--shiny .irs-to{background:var(--primary);border-color:var(--primary)}
.irs--shiny .irs-handle,.irs--shiny .irs-handle:hover,.irs--shiny .irs-handle.state_hover{background:var(--primary);border-color:var(--primary)}
.irs--shiny .irs-handle>i:first-child{background:var(--primary)}
.form-check-input:checked{background-color:var(--primary);border-color:var(--primary)}
.progress-bar{background-color:var(--primary)}
.bc-progress{height:14px;background:var(--neutral);border-radius:7px;overflow:hidden;margin:8px 0}
.bc-progress>div{height:100%;border-radius:7px;transition:width .6s;
 background:repeating-linear-gradient(45deg,#e9c46a 0 10px,#e3b552 10px 20px)}
.bc-meter{height:8px;background:var(--neutral);border-radius:4px;overflow:hidden;margin:4px 0 2px}
.bc-meter>div{height:100%;background:var(--primary)}
.bc-drop{border:2px dashed #e0b9a6;border-radius:16px;padding:16px 14px 6px;text-align:center;background:#fff6ef;transition:all .15s}
.bc-drop.over{border-color:var(--upload-d);background:var(--upload)}.bc-drop .big{font-size:1.6rem;line-height:1}
.bc-drop .shiny-input-container{margin:8px auto 0;text-align:left}
.bc-note{color:var(--ink2);font-size:.86rem}.bc-help{margin:-6px 0 14px;font-size:.8rem;line-height:1.35}.bc-bad{color:var(--bad)}.bc-good{color:var(--good)}
/* next / back footer */
.bc-next{display:flex;justify-content:space-between;align-items:center;gap:10px;margin:14px 2px 6px}
.bc-next a{border-radius:999px;font-weight:800;padding:7px 18px;text-decoration:none;border:2px solid var(--grid);color:var(--ink2)}
.bc-next a.fwd{background:var(--primary);border-color:var(--primary);color:#fff}.bc-next a.fwd:hover{background:var(--primary-h)}
.bc-next a.back:hover{background:var(--neutral)}
/* processing checklist */
.bc-check{list-style:none;padding:0;margin:10px 0}
.bc-check li{display:flex;align-items:center;gap:10px;padding:6px 10px;border-radius:10px;margin-bottom:4px;color:var(--ink2)}
.bc-check li .ic{width:1.6em;text-align:center;font-size:1.05rem}
.bc-check li.done{color:var(--ink)}.bc-check li.now{background:var(--process);color:var(--ink);font-weight:700}
.bc-check li.err{background:#f6d3cc;color:var(--bad)}
.bc-check li .spin{display:inline-block;animation:bcspin 1.1s linear infinite}@keyframes bcspin{to{transform:rotate(360deg)}}
.bc-check li .ds{margin-left:auto;font-size:.8rem;color:var(--ink2);font-weight:400}
/* hero */
.bc-hero{display:grid;grid-template-columns:minmax(220px,340px) 1fr;gap:22px;align-items:center;background:var(--card);
 border:2px solid var(--grid);border-radius:22px;padding:18px;margin-bottom:14px;position:relative;overflow:hidden}
.bc-hero:before{content:"";position:absolute;right:-60px;top:-60px;width:260px;height:260px;border-radius:50%;
 background:radial-gradient(circle,#f5de9a 0 35%,#f8c9b4 36% 55%,#d2dfae 56% 75%,transparent 76%);opacity:.55}
.bc-hero figure{margin:0}.bc-hero img{width:100%;border-radius:160px 160px 18px 18px;border:6px solid #fff;
 box-shadow:0 6px 0 var(--process),0 6px 18px rgba(59,42,30,.18);aspect-ratio:3/4;object-fit:cover}
.bc-hero figcaption{font-size:.75rem;color:var(--ink2);margin-top:8px;text-align:center}
.bc-hero h2{font-family:"Righteous","Nunito",sans-serif;font-size:2.1rem;line-height:1.1;margin:0 0 6px;color:var(--ink)}
.bc-hero .lead{font-size:1rem;color:var(--ink2);max-width:46ch}
.bc-hero .ctas{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}
.bc-call{display:flex;align-items:center;gap:12px;background:#fff;border:2px solid var(--grid);border-radius:999px;
 padding:8px 16px 8px 8px;margin-top:12px;max-width:560px;position:relative;z-index:1}
.bc-call button{flex:0 0 48px;height:48px;border-radius:50%;border:none;background:var(--upload-d);color:#fff;font-size:1.2rem;
 box-shadow:0 3px 0 #7d3118;cursor:pointer}
.bc-call button:active{transform:translateY(2px);box-shadow:0 1px 0 #7d3118}
.bc-call .body{flex:1;min-width:0}
.bc-call .wave{display:flex;align-items:center;gap:2px;height:38px;cursor:pointer;margin-top:2px}
.bc-call .wave i{flex:1;background:#e7c9a0;border-radius:2px;min-height:3px;transition:background .1s}
.bc-call .wave i.on{background:var(--results-d)}
.bc-call .who{font-weight:800;color:var(--ink);display:block;font-size:.9rem}.bc-call .who small{font-weight:600;color:var(--ink2)}
.bc-credit{font-size:.74rem;color:var(--ink2);margin:4px 0 0 18px}.bc-credit a{color:var(--ink2)}
@media(max-width:720px){.bc-hero{grid-template-columns:1fr}.bc-hero img{max-height:340px}.bc-header .tools{margin-left:0}}
/* cards & tables */
.bc-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.bc-card{background:#fff;border:2px solid var(--grid);border-radius:14px;padding:10px;display:flex;flex-direction:column;gap:6px}
.bc-card img{width:100%;border-radius:8px;background:var(--neutral);min-height:120px}
.bc-card audio{width:100%;height:34px}
.bc-card .hd{display:flex;justify-content:space-between;gap:8px;font-weight:800}
.bc-card .conf{font-variant-numeric:tabular-nums;background:var(--process);border-radius:999px;padding:0 8px;font-weight:800;color:var(--process-d)}
.bc-card .sub,.bc-card .meta{font-size:.78rem;color:var(--ink2)}
.bc-card .act{display:flex;gap:6px;flex-wrap:wrap}.bc-card .act button{flex:1;font-size:.82rem;padding:3px 6px}
.bc-card .rl{display:flex;gap:6px}.bc-card .rl input{flex:1;min-width:0;font-size:.8rem;padding:3px 8px;border:1px solid var(--grid);border-radius:999px}
.bc-card .stt{font-size:.8rem;font-weight:700}
.bc-card.st-confirm{border-color:var(--verify-d);box-shadow:inset 5px 0 0 var(--verify-d)}
.bc-card.st-reject{border-color:var(--bad);box-shadow:inset 5px 0 0 var(--bad);opacity:.75}
.bc-card.st-relabel{border-color:var(--warn);box-shadow:inset 5px 0 0 var(--warn)}
.btn-ok{background:var(--verify);border:1px solid #9fb76d;color:#2f4410;border-radius:999px;font-weight:700}
.btn-no{background:#f6d3cc;border:1px solid #e3a294;color:#7a1f12;border-radius:999px;font-weight:700}
.btn-rl{background:var(--process);border:1px solid #e3c36a;color:#5c3b00;border-radius:999px;font-weight:700}
.bc-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:12px}
.bc-kpi{background:#fff;border:2px solid var(--grid);border-radius:16px;padding:10px 14px}
.bc-kpi:nth-child(1){border-color:var(--upload)}.bc-kpi:nth-child(2){border-color:var(--process)}.bc-kpi:nth-child(3){border-color:var(--verify)}
.bc-kpi:nth-child(4){border-color:var(--results)}.bc-kpi:nth-child(5){border-color:var(--report)}
.bc-kpi .v{font-size:1.6rem;font-weight:900;font-variant-numeric:tabular-nums}.bc-kpi .k{font-size:.8rem;color:var(--ink2)}
.bc-cite{border-left:4px solid var(--process-d);padding:6px 12px;background:#fff;font-size:.9rem;border-radius:0 10px 10px 0}
.bc-dict{max-height:420px;overflow:auto}.bc-dict td{font-size:.84rem}.card h6{margin-top:.9rem;font-weight:800}
.bc-bye{max-width:560px;margin:12vh auto;text-align:center;background:var(--card);border:2px solid var(--grid);border-radius:22px;padding:30px}
.bc-bye h2{font-family:"Righteous","Nunito",sans-serif}
"""

JS = """
(function(){
  let quota=null, dirty=false, origTitle=document.title, flashTimer=null;
  Shiny.addCustomMessageHandler('bc-quota', m => { quota = m; });
  Shiny.addCustomMessageHandler('bc-dirty', m => { dirty = !!m.dirty; });
  Shiny.addCustomMessageHandler('bc-card', m => {
    const c=document.getElementById('card-'+m.id); if(!c) return;
    c.classList.remove('st-pending','st-confirm','st-reject','st-relabel'); c.classList.add('st-'+m.decision);
    const s=c.querySelector('.stt'); if(s) s.textContent=m.text;
  });
  Shiny.addCustomMessageHandler('bc-notify', m => {
    try{ if('Notification' in window && Notification.permission==='granted') new Notification(m.title,{body:m.body}); }catch(e){}
    try{ const a=new (window.AudioContext||window.webkitAudioContext)(), o=a.createOscillator(), g=a.createGain();
      o.frequency.value=880; g.gain.value=0.05; o.connect(g); g.connect(a.destination); o.start(); o.stop(a.currentTime+0.18);}catch(e){}
    if(document.hidden){ clearInterval(flashTimer); let on=false;
      flashTimer=setInterval(()=>{document.title=(on=!on)?('✔ '+m.title):origTitle;},900);
      document.addEventListener('visibilitychange',()=>{clearInterval(flashTimer);document.title=origTitle;},{once:true}); }
  });
  function askNotify(){ if('Notification' in window && Notification.permission==='default') Notification.requestPermission(); }
  document.addEventListener('click', e => {
    const g=e.target.closest('[data-goto]'); if(g) Shiny.setInputValue('goto', g.dataset.goto, {priority:'event'});
    if(e.target.closest('#run')) askNotify();
  });
  // Block uploads that would overflow the shared cache before any bytes are sent.
  document.addEventListener('change', e => {
    const t=e.target; if(!t || t.id!=='upload' || !t.files || !quota) return;
    let total=0; for(const f of t.files) total+=f.size;
    if(total>quota.remaining){ e.stopImmediatePropagation(); t.value='';
      alert('These files total '+(total/1073741824).toFixed(2)+' GB but only '+(quota.remaining/1073741824).toFixed(2)+
            ' GB of the '+(quota.limit/1073741824).toFixed(0)+' GB temporary cache is free. Upload fewer files at a time.'); }
  }, true);
  // Drag & drop anywhere on the upload card (also works where a native file dialog is unavailable).
  const EXT=/\\.(wav|flac|ogg)$/i;
  window.addEventListener('dragover', e => { if(e.dataTransfer && [...e.dataTransfer.types].includes('Files')) e.preventDefault();
    const z=e.target.closest && e.target.closest('.bc-drop'); document.querySelectorAll('.bc-drop').forEach(d=>d.classList.toggle('over', d===z)); });
  window.addEventListener('dragleave', e => { if(!e.relatedTarget) document.querySelectorAll('.bc-drop').forEach(d=>d.classList.remove('over')); });
  window.addEventListener('drop', e => {
    if(!e.dataTransfer || !e.dataTransfer.files.length) return;
    e.preventDefault(); document.querySelectorAll('.bc-drop').forEach(d=>d.classList.remove('over'));
    if(!e.target.closest || !e.target.closest('.bc-drop')) return;
    const dt=new DataTransfer(); let skipped=0;
    for(const f of e.dataTransfer.files){ if(EXT.test(f.name)) dt.items.add(f); else skipped++; }
    if(!dt.files.length){ alert('No WAV, FLAC or OGG files in what you dropped.'); return; }
    if(skipped) alert(skipped+' non-audio file(s) ignored.');
    const inp=document.getElementById('upload'); inp.files=dt.files; inp.dispatchEvent(new Event('change',{bubbles:true}));
  });
  window.addEventListener('beforeunload', e => { if(dirty){ e.preventDefault(); e.returnValue=''; } });
  window.bcVerify = function(id, decision){
    let label='';
    if(decision==='relabel'){ const i=document.getElementById('rl-'+id); label=i?i.value.trim():'';
      if(!label){ alert('Type or pick a species name first.'); return; } }
    const who=(document.getElementById('verifier')||{}).value||'';
    Shiny.setInputValue('verify_action',{id:id,decision:decision,label:label,verifier:who,nonce:Date.now()},{priority:'event'});
  };
  $(document).on('shiny:connected', () => {
    try{ const v=localStorage.getItem('bc-verifier'); if(v) $('#verifier').val(v).trigger('change'); }catch(e){}
  });
  // King Parrot call bar: play/pause, progress across the waveform, click to seek.
  document.addEventListener('click', e => {
    const bar=e.target.closest('.bc-call'); if(!bar) return;
    const au=bar.querySelector('audio'), bars=[...bar.querySelectorAll('.wave i')], btn=bar.querySelector('button');
    if(!au.dataset.bound){ au.dataset.bound=1;
      au.addEventListener('timeupdate',()=>{ const k=Math.round(bars.length*au.currentTime/(au.duration||1));
        bars.forEach((b,i)=>b.classList.toggle('on',i<k)); });
      au.addEventListener('play',()=>{btn.textContent='❚❚';btn.setAttribute('aria-label','Pause');});
      au.addEventListener('pause',()=>{btn.textContent='▶';btn.setAttribute('aria-label','Play');});
      au.addEventListener('ended',()=>bars.forEach(b=>b.classList.remove('on'))); }
    const w=e.target.closest('.wave');
    if(w){ const r=w.getBoundingClientRect(); au.currentTime=(au.duration||0)*(e.clientX-r.left)/r.width; au.play(); return; }
    if(e.target.closest('button')){ au.paused?au.play():au.pause(); }
  });
  // Start over = reload: this session (and its data) ends, a fresh one starts.
  window.bcRestart = function(){
    if(confirm('Start over? This deletes the recordings, results and checks in this session.\\n\\nDownload your PDF report, CSV bundle and site profile first if you need them.')){
      dirty=false; location.reload(); } };
  Shiny.addCustomMessageHandler('bc-ended', m => {
    dirty=false;
    document.body.innerHTML='<div class="bc-stripe"></div><div class="bc-bye"><div style="font-size:2.6rem">🦜</div>'+
      '<h2>'+(m.stopped?'App stopped':'Session ended')+'</h2><p>'+m.text+'</p>'+
      (m.stopped?'':'<p><a class="btn btn-primary" href="'+location.pathname+'">Start a new session</a></p>')+'</div>';
  });
  $(document).on('change', '#verifier', function(){ try{ localStorage.setItem('bc-verifier', this.value); }catch(e){} });
})();
"""

TZ_CHOICES = sorted(available_timezones()) if available_timezones() else [DEFAULT_TZ]
LABEL_DATALIST = ui.HTML('<datalist id="bc-labels">' + "".join(
    f'<option value="{html.escape(f"{c} ({s})")}">' for s, c in LABELS) + "</datalist>")


def kpi(value, label):
    return ui.div(ui.div(value, class_="v"), ui.div(label, class_="k"), class_="bc-kpi")


def nav_footer(back: tuple | None = None, fwd: tuple | None = None):
    """Back / Next buttons that move along the workflow (handled by the data-goto click listener)."""
    b = f'<a href="#" class="back" data-goto="{back[0]}">‹ Back: {back[1]}</a>' if back else "<span></span>"
    f = f'<a href="#" class="fwd" data-goto="{fwd[0]}">Next: {fwd[1]} ›</a>' if fwd else "<span></span>"
    return ui.HTML(f'<div class="bc-next">{b}{f}</div>')


def hero():
    call = read_json(WWW_DIR / "king_parrot_call.json", None)
    bars = "".join(f'<i style="height:{max(8, int(p * 100))}%"></i>' for p in (call or {}).get("peaks", []))
    call_html = (f"""<div class="bc-call" role="group" aria-label="Australian King-Parrot call">
<button type="button" aria-label="Play">▶</button>
<div class="body"><span class="who">🦜 Australian King-Parrot <small>· {call['duration']:.0f} s · tap to play</small></span>
<div class="wave" title="Click to jump">{bars}</div></div>
<audio preload="metadata" src="static/king_parrot_call.m4a"></audio></div>
<div class="bc-credit">Call: <a href="{call['url']}" target="_blank" rel="noopener">{html.escape(call['credit'])}</a></div>""" if call else "")
    photo = ('<figure><img src="static/king_parrot.jpg" alt="Male Australian King-Parrot feeding in a berry tree">'
             f'<figcaption>Male Australian King-Parrot · photo © {APP_AUTHOR}</figcaption></figure>'
             if (WWW_DIR / "king_parrot.jpg").exists() else "<span></span>")
    demo_n = len(list(DEMO_DIR.glob("*.wav"))) if DEMO_DIR.exists() else 0
    return ui.div(
        ui.HTML(photo),
        ui.div(
            ui.h2("Hear who's calling."),
            ui.p("Drop in your recorder's files and BirdNET lists every bird it hears: when, how often, how long and "
                 "at what pitch. Then check the uncertain calls by ear, and take home a report.", class_="lead"),
            ui.HTML(call_html),
            ui.div(
                ui.input_action_button("start", "Upload my recordings ›", class_="btn-primary"),
                ui.input_action_button("demo", f"🦜 Try with demo data ({demo_n} recordings)",
                                       class_="btn-outline-primary") if demo_n else None,
                class_="ctas"),
        ),
        class_="bc-hero",
    )


def about_panel():
    limits = [x.format(gb=CACHE_LIMIT / 1024**3, jobs=MAX_JOBS) for x in APP_LIMITS]
    ul = lambda xs: ui.tags.ul(*[ui.tags.li(x) for x in xs])  # noqa: E731
    rows = "".join(f"<tr><td>{html.escape(t)}</td><td><code>{html.escape(c)}</code></td><td>{html.escape(u)}</td>"
                   f"<td>{html.escape(d)}</td></tr>" for t, c, u, d in DATA_DICTIONARY)
    birdnetlib_v = ""
    with contextlib.suppress(Exception):
        from importlib import metadata
        birdnetlib_v = " " + metadata.version("birdnetlib")
    return ui.nav_panel(
        "About",
        hero(),
        ui.layout_columns(
            ui.card(
                ui.card_header("What this app does"),
                ui.p(APP_PURPOSE),
                ui.h6("Workflow"),
                ui.tags.ol(ui.tags.li(ui.tags.b("Upload"), " recordings (many at once)."),
                           ui.tags.li(ui.tags.b("Process"), " with BirdNET in the background."),
                           ui.tags.li(ui.tags.b("Verify"), " low-confidence calls by ear: confirm, reject or relabel."),
                           ui.tags.li(ui.tags.b("Results"), ": species, IDs, counts per day, call times, durations, "
                                      "intervals and pitch, with a custom confidence threshold."),
                           ui.tags.li(ui.tags.b("Report"), ": download the PDF and CSV bundle.")),
                ui.h6("What you need"), ul(APP_INPUTS),
                ui.h6("Limits and runtime"), ul(limits),
            ),
            ui.card(
                ui.card_header("Data, citation and reproducibility"),
                ui.h6("Data provenance"),
                ul([f"Species model: BirdNET GLOBAL 6K v2.4 via birdnetlib{birdnetlib_v} — Kahl, Wood, Eibl & Klinck (2021) "
                    "BirdNET: a deep learning solution for avian diversity monitoring. Ecological Informatics 61, 101236.",
                    "Expected-species filter: BirdNET location/week meta-model, using the recorder's GPS and date.",
                    "Start time and location: GUANO metadata written by the recorder, else the file name.",
                    "Every input file is fingerprinted (SHA-1) and listed in the report and CSV bundle."]),
                ui.h6("How to cite"),
                ui.tags.blockquote(CITATION, class_="bc-cite"),
                ui.p("Machine-readable citation: CITATION.cff in the code repository. A DOI will be added after the first "
                     "archived release.", class_="bc-note"),
                ui.h6("Licence"), ui.p(APP_LICENCE),
                ui.h6("Reproducibility"),
                ui.p("The session info lists the app version, Python and package versions, and BirdNET model file "
                     "checksums. The same file is included in the CSV bundle.", class_="bc-note"),
                ui.p("Best downloaded after processing, when it also records your files and settings. "
                     "It's on the Report tab too, and inside the CSV bundle.", class_="bc-note"),
                ui.download_button("dl_session", "⬇ Download session info", class_="btn-outline-primary"),
                ui.h6("Demo data and pictures"),
                ui.p(f"Photo © {APP_AUTHOR}. King-Parrot call: Petra Holland, iNaturalist 324241020, CC BY 4.0. "
                     "Demo recordings: iNaturalist observations 324241020 (CC BY 4.0, Petra Holland), 199447984 and "
                     "199347288 (CC0, Rosalie Lawrence) — resampled, coordinates rounded to ~1 km.", class_="bc-note"),
            ),
            col_widths=[6, 6],
        ),
        ui.card(ui.card_header("Data dictionary — columns in the CSV bundle"),
                ui.HTML(f'<div class="bc-dict"><table class="table table-sm"><thead><tr><th>Table</th><th>Column</th>'
                        f'<th>Unit</th><th>Meaning</th></tr></thead><tbody>{rows}</tbody></table></div>')),
        nav_footer(None, ("upload", "Upload")),
        value="about",
    )


app_ui = ui.page_fluid(
    ui.head_content(
        ui.tags.link(rel="preconnect", href="https://fonts.googleapis.com"),
        ui.tags.link(rel="stylesheet", href="https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800;900"
                                            "&family=Righteous&display=swap"),
        ui.tags.style(CSS), ui.tags.title(APP_TITLE)),
    ui.div(class_="bc-stripe"),
    ui.div(ui.h1(APP_TITLE), ui.span(f"v{APP_VERSION}", class_="bc-ver"),
           ui.span(f"{APP_FAMILY} · BirdNET call detection · human verification · reporting", class_="sub"),
           ui.div(ui.tags.button("↺ Start over", type="button", class_="btn btn-outline-secondary",
                                 onclick="bcRestart()", title="Delete this session's data and start fresh"),
                  ui.input_action_button("end_session", "⏻ End session", class_="btn-outline-secondary",
                                         title="Delete your data now and close the session"),
                  class_="tools"),
           class_="bc-header"),
    ui.div(
        ui.span("⚠"),
        ui.span(ui.tags.b("Temporary workspace: "),
                "uploads, results and your verifications are deleted when this session ends "
                "(closing or refreshing the tab, or losing connection). ",
                ui.tags.b("Download the PDF report"), " (charts, tables, spectrograms), the CSV bundle and your site "
                "profile before you leave."),
        ui.tags.a("Go to report ›", href="#", class_="bc-link", **{"data-goto": "report"}),
        class_="bc-warn",
    ),
    ui.output_ui("steps"),
    ui.navset_tab(
        about_panel(),
        ui.nav_panel(
            "1 · Upload",
            ui.layout_columns(
                ui.card(
                    ui.card_header("Upload recordings"),
                    ui.div(
                        ui.div("⤓", class_="big"),
                        ui.tags.b("Drag & drop recordings here"),
                        ui.div("or", class_="bc-note"),
                        ui.input_file("upload", None, multiple=True, accept=list(AUDIO_EXT), width="100%",
                                      button_label="Choose files…", placeholder="WAV · FLAC · OGG"),
                        class_="bc-drop",
                    ),
                    ui.p("Tip: select all files of a deployment at once. Recorder metadata (GUANO time and GPS) "
                         "is read automatically.", class_="bc-note"),
                    ui.input_file("profile_up", "Site profile (optional)", accept=[".json"], width="100%",
                                  button_label="Load profile…", placeholder="site_profile.json from a past session"),
                    ui.output_ui("profile_status"),
                    ui.output_ui("cache_meter"),
                    ui.div(ui.input_action_button("demo2", "🦜 Load demo data", class_="btn-outline-primary btn-sm"),
                           ui.input_action_button("clear_ws", "Clear workspace", class_="btn-outline-secondary btn-sm"),
                           style="display:flex;gap:8px;flex-wrap:wrap"),
                ),
                ui.card(ui.card_header("Files in this workspace"), ui.output_ui("files_note"),
                        ui.output_data_frame("files_table")),
                col_widths=[4, 8],
            ),
            nav_footer(("about", "About"), ("process", "Process")),
            value="upload",
        ),
        ui.nav_panel(
            "2 · Process",
            ui.layout_columns(
                ui.card(
                    ui.card_header("BirdNET settings"),
                    ui.input_radio_buttons("loc_mode", "Location (limits species to those expected there that week)",
                                           {"guano": "From each file's recorder GPS (typed values fill gaps)",
                                            "manual": "Use the typed coordinates for every file",
                                            "none": "No location filter (all 6,522 species)"}),
                    ui.layout_columns(ui.input_numeric("lat", "Latitude", None, step=0.0001),
                                      ui.input_numeric("lon", "Longitude", None, step=0.0001)),
                    ui.input_radio_buttons("ts_mode", "Recording start times are…",
                                           {"local": "Local clock time (use as-is)",
                                            "utc": "UTC — convert to time zone below"}),
                    ui.output_ui("ts_hint"),
                    ui.input_selectize("tz", "Time zone", TZ_CHOICES, selected=DEFAULT_TZ),
                    ui.input_slider("min_conf", "Detection floor (lowest confidence kept for review)", 0.05, 0.9, 0.1, step=0.05),
                    ui.p("BirdNET ignores anything scored below this. Lower = more candidate calls to review (and more false alarms). You can still raise the threshold in Results without re-running.", class_="bc-note bc-help"),
                    ui.input_slider("sensitivity", "Sensitivity", 0.5, 1.5, 1.0, step=0.05),
                    ui.p("How readily BirdNET reports a call. Higher finds fainter or distant calls but adds false alarms; 1.0 is BirdNET's default.", class_="bc-note bc-help"),
                    ui.input_slider("overlap", "Window overlap (s)", 0.0, 2.5, 0.0, step=0.5),
                    ui.p("BirdNET listens in 3-second windows. Overlap (e.g. 1.5 s) lets windows overlap so calls cut by a window edge aren't missed. It's slower and gives more detections per call.", class_="bc-note bc-help"),
                ),
                ui.card(
                    ui.card_header("Run"),
                    ui.div(ui.input_action_button("run", "▶ Run BirdNET", class_="btn-primary"),
                           ui.input_action_button("cancel", "Cancel", class_="btn-outline-secondary"),
                           style="display:flex;gap:8px"),
                    ui.output_ui("job_status"),
                    ui.output_ui("job_check"),
                    ui.output_data_frame("job_log"),
                ),
                col_widths=[5, 7],
            ),
            nav_footer(("upload", "Upload"), ("verify", "Verify")),
            value="process",
        ),
        ui.nav_panel(
            "3 · Verify",
            ui.card(
                ui.card_header("Listen to low-confidence calls and confirm, reject or relabel them"),
                ui.layout_columns(
                    ui.input_text("verifier", "Your name / initials", placeholder="e.g. AR"),
                    ui.input_slider("rev_band", "Confidence band to review", 0.0, 1.0, (0.1, 0.5), step=0.05),
                    ui.input_numeric("rev_n", "Clips per species", 6, min=1, max=50),
                    ui.input_select("rev_order", "Pick clips",
                                    {"spread": "Spread across band", "lowest": "Lowest first",
                                     "random": "Random", "highest": "Highest first"}),
                    ui.input_select("rev_status", "Show", {"all": "All in sample", "pending": "Unverified only"}),
                    col_widths=[2, 3, 2, 3, 2],
                ),
                ui.div(ui.input_selectize("rev_species", "Species", [], multiple=True,
                                          options={"placeholder": "All species"}),
                       ui.input_action_button("reshuffle", "↻ New random sample", class_="btn-sm btn-outline-secondary"),
                       style="display:flex;gap:12px;align-items:end;flex-wrap:wrap"),
                ui.output_ui("rev_progress"),
                ui.output_ui("rev_cards"),
                LABEL_DATALIST,
            ),
            nav_footer(("process", "Process"), ("results", "Results")),
            value="verify",
        ),
        ui.nav_panel(
            "4 · Results",
            ui.layout_sidebar(
                ui.sidebar(
                    ui.input_slider("thr", "Confidence threshold", 0.05, 1.0, 0.5, step=0.01),
                    ui.input_switch("override", "Always keep verified calls", True),
                    ui.input_switch("verified_only", "Verified calls only", False),
                    ui.input_switch("per_species", "Per-species thresholds (site profile)", True),
                    ui.input_numeric("gap", "Merge detections into one call if gap ≤ (s)", 0, min=0, max=30, step=0.5),
                    ui.input_selectize("species_sel", "Species", [], multiple=True, options={"placeholder": "All species"}),
                    ui.hr(),
                    ui.download_button("dl_pdf", "⬇ PDF report", class_="btn-primary w-100"),
                    ui.download_button("dl_zip", "⬇ CSV bundle (.zip)", class_="btn-outline-primary w-100 mt-2"),
                    width=290, open="always",
                ),
                ui.output_ui("kpis"),
                ui.navset_card_underline(
                    ui.nav_panel("Overview", ui.output_plot("p_species", height="420px"),
                                 ui.layout_columns(ui.output_plot("p_hourly", height="280px"),
                                                   ui.output_plot("p_conf", height="280px"))),
                    ui.nav_panel("Species & IDs", ui.output_data_frame("t_species"),
                                 ui.h6("Rejected by reviewer", style="margin-top:14px"),
                                 ui.p("Species whose calls you rejected or relabelled. Rejected calls are left out of "
                                      "all results; a species that disappears entirely still shows here.",
                                      class_="bc-note"),
                                 ui.output_data_frame("t_rejected")),
                    ui.nav_panel(
                        "Reliability",
                        ui.p("How often BirdNET was right for each species, from your Confirm / Reject / Relabel "
                             "checks. A learned threshold is the lowest confidence that reaches the target. It is "
                             "applied when 'Per-species thresholds' is on; other species use the global threshold.",
                             class_="bc-note"),
                        ui.layout_columns(
                            ui.input_slider("target", "Target: share correct", 0.7, 0.99, 0.9, step=0.01),
                            ui.input_numeric("min_n", "Minimum checks per species", 5, min=2, max=50),
                            ui.input_text("profile_name", "Profile name", placeholder="e.g. Balcony 2026"),
                            col_widths=[4, 4, 4]),
                        ui.output_data_frame("t_rel"),
                        ui.div(ui.input_selectize("rule_sp", "Species", [], width="280px"),
                               ui.input_numeric("rule_thr", "Threshold", 0.5, min=0.05, max=1, step=0.05, width="120px"),
                               ui.input_action_button("rule_set", "Set threshold", class_="btn-sm btn-outline-primary"),
                               ui.input_action_button("rule_block", "Block species", class_="btn-sm btn-outline-danger"),
                               ui.input_action_button("rule_clear", "Use learned / clear", class_="btn-sm btn-outline-secondary"),
                               style="display:flex;gap:10px;align-items:end;flex-wrap:wrap"),
                        ui.download_button("dl_profile", "⬇ Download site profile", class_="btn-primary mt-2"),
                    ),
                    ui.nav_panel("Sites", ui.p("One row per recording site (from each file's GPS). Darwin Core "
                                               "occurrences for GBIF / ALA are in the CSV bundle under darwin_core/.",
                                               class_="bc-note"), ui.output_data_frame("t_sites")),
                    ui.nav_panel("Timeline", ui.output_plot("p_timeline", height="480px")),
                    ui.nav_panel("Daily counts", ui.output_plot("p_daily", height="380px"),
                                 ui.output_data_frame("t_daily")),
                    ui.nav_panel("Call timing", ui.output_plot("p_gaps", height="280px"),
                                 ui.p("Select a row to listen to that call.", class_="bc-note"),
                                 ui.output_data_frame("t_events"), ui.output_ui("event_player")),
                    ui.nav_panel("Frequencies", ui.output_plot("p_freq", height="420px"),
                                 ui.p("Frequency is measured on each detection after subtracting the recording's median "
                                      "background noise, so constant hiss does not dominate.", class_="bc-note")),
                    ui.nav_panel("Examples", ui.output_ui("examples")),
                    ui.nav_panel("Detections", ui.p("Select a row to listen.", class_="bc-note"),
                                 ui.output_data_frame("t_det"), ui.output_ui("det_player")),
                    ui.nav_panel("Report", ui.output_ui("report_panel"), value="report"),
                    id="res_tabs",
                ),
                nav_footer(("verify", "Verify"), None),
                class_="bc-subtabs",
            ),
            value="results",
        ),
        id="tabs",
    ),
    ui.tags.script(JS),
)


def card_html(r, sid: str, text: str | None = None) -> str:
    base = f"ws/{sid}/job/clips/{r.det_id}"
    e = html.escape
    when = r.datetime.strftime("%Y-%m-%d %H:%M:%S") if pd.notna(r.datetime) else "time unknown"
    status = text or {"pending": "Unverified", "confirm": "✓ Confirmed", "reject": "✗ Rejected",
                      "relabel": f"↺ Relabelled → {r.common}"}.get(r.decision, r.decision)
    return f"""<div class="bc-card st-{e(r.decision)}" id="card-{r.det_id}">
<div class="hd"><span>{e(r.original_common)}</span><span class="conf">{r.confidence:.2f}</span></div>
<div class="sub"><i>{e(str(r.scientific))}</i> · {e(str(r.species_id))} · {e(str(r.sci_code))}</div>
<img src="{base}.png" loading="lazy" alt="Waveform and spectrogram of the clip">
<audio controls preload="none" src="{base}.wav"></audio>
<div class="meta">{when} · {e(r.file)} @ {r.start_s:.0f}–{r.end_s:.0f} s · peak {'' if pd.isna(r.peak_hz) else int(r.peak_hz)} Hz</div>
<div class="act"><button class="btn-ok" onclick="bcVerify('{r.det_id}','confirm')">✓ Confirm</button>
<button class="btn-no" onclick="bcVerify('{r.det_id}','reject')">✗ Reject</button>
<button class="btn btn-sm btn-light" onclick="bcVerify('{r.det_id}','clear')" title="Undo">↶</button></div>
<div class="rl"><input list="bc-labels" id="rl-{r.det_id}" placeholder="Relabel: type a species…">
<button class="btn-rl" onclick="bcVerify('{r.det_id}','relabel')">Relabel</button></div>
<div class="stt">{e(status)}</div></div>"""


def player_html(r, sid: str) -> str:
    base = f"ws/{sid}/job/clips/{r['det_id']}"
    return (f'<div class="bc-card" style="max-width:520px"><b>{html.escape(str(r["common"]))}</b>'
            f'<img src="{base}.png"><audio controls autoplay src="{base}.wav"></audio></div>')


def server(input, output, session):
    sid = uuid.uuid4().hex
    ws = CACHE_ROOT / sid
    (ws / "audio").mkdir(parents=True, exist_ok=True)
    touch_alive(ws)
    sweep_cache(keep=sid)
    proc: dict = {"p": None}
    manifest = reactive.value([])
    ver_bump = reactive.value(0)
    downloaded = reactive.value(False)
    last_state = {"s": None, "beat": 0.0}
    rev_seed = reactive.value(1)
    profile_bump = reactive.value(0)
    choice_cache: dict = {}

    def cleanup():
        p = proc["p"]
        with contextlib.suppress(Exception):
            (ws / "job").mkdir(exist_ok=True)
            (ws / "job" / "cancel").touch()
            if p and p.poll() is None:
                p.terminate()
                p.wait(timeout=5)
        shutil.rmtree(ws, ignore_errors=True)

    session.on_ended(cleanup)

    # ----- start-up: warning modal + quota
    @reactive.effect
    async def _start():
        ui.modal_show(ui.modal(
            ui.p("This app keeps your recordings and results only for this browser session."),
            ui.tags.ul(
                ui.tags.li("Everything is deleted when you close or refresh this tab, or the connection drops."),
                ui.tags.li("Before leaving, open ", ui.tags.b("4 · Results → Report"),
                           " and download the PDF report, the CSV bundle and your site profile (to reuse your "
                           "checks next time)."),
                ui.tags.li(f"The server shares a {CACHE_LIMIT / 1024**3:.0f} GB temporary cache between all users."),
            ),
            title="Your data is temporary", easy_close=False, footer=ui.modal_button("I understand"),
        ))
        await send_quota()

    async def send_quota():
        used = cache_usage()
        await session.send_custom_message("bc-quota", {"remaining": max(CACHE_LIMIT - used, 0), "limit": CACHE_LIMIT})

    # ----- upload
    @reactive.effect
    @reactive.event(input.upload)
    async def _upload():
        items = input.upload() or []
        incoming = sum(int(i["size"]) for i in items)
        sweep_cache(keep=sid)
        if cache_usage() + incoming > CACHE_LIMIT:
            ui.notification_show(f"Not enough space in the {CACHE_LIMIT / 1024**3:.0f} GB cache for "
                                 f"{fmt_bytes(incoming)}. Try fewer files.", type="error", duration=10)
            return
        skipped = []
        with ui.Progress(min=0, max=len(items) + 1) as prog:
            prog.set(message="Saving uploads…")
            for k, it in enumerate(items):
                name = os.path.basename(it["name"])
                if not name.lower().endswith(AUDIO_EXT):
                    skipped.append(name)
                    continue
                shutil.move(it["datapath"], ws / "audio" / name)
                prog.set(k + 1, detail=name)
            prog.set(message="Reading recorder metadata…")
            m = await asyncio_to_thread(build_manifest, ws)
        manifest.set(m)
        first = next((x for x in m if x.get("lat") is not None), None)
        if first and input.lat() is None:
            ui.update_numeric("lat", value=first["lat"])
            ui.update_numeric("lon", value=first["lon"])
        bad = [x for x in m if x["status"] != "ok"]
        msg = f"Uploaded {len(items) - len(skipped)} file(s), {fmt_bytes(incoming)}."
        if bad:
            msg += f" ⚠ {len(bad)} empty/unreadable file(s) will be skipped."
        if skipped:
            msg += f" Ignored non-audio: {', '.join(skipped[:5])}"
        ui.notification_show(msg, type="warning" if bad else "message", duration=8)
        await session.send_custom_message("bc-notify", {"title": "Upload complete", "body": msg})
        await send_quota()

    @reactive.effect
    @reactive.event(input.clear_ws)
    async def _clear():
        if proc["p"] and proc["p"].poll() is None:
            ui.notification_show("Cancel the running job first.", type="warning")
            return
        for sub in ("audio", "job"):
            shutil.rmtree(ws / sub, ignore_errors=True)
        for f in ("manifest.json", "verifications.csv", "report.pdf", "results.zip"):
            with contextlib.suppress(OSError):
                (ws / f).unlink()
        (ws / "audio").mkdir()
        manifest.set([])
        ver_bump.set(ver_bump() + 1)
        ui.notification_show("Workspace cleared.")
        await send_quota()

    @render.ui
    def ts_hint():
        m = manifest()
        if not any(x.get("start_is_utc") for x in m):
            return None
        ex = next(x for x in m if x.get("start_is_utc"))
        return ui.p(f"ℹ The recorder labels its times as UTC (e.g. {ex['start'].replace('T', ' ')} Z). Many recorders "
                    "write local clock time with a 'Z' anyway — if the file names match your local time, keep "
                    "'Local clock time'.", class_="bc-note")

    @render.ui
    def cache_meter():
        reactive.invalidate_later(15)
        manifest()
        used, mine = cache_usage(), dir_size(ws)
        pct = min(100, 100 * used / CACHE_LIMIT)
        return ui.div(
            ui.div(ui.tags.b("Temporary cache"), f" — this session {fmt_bytes(mine)} · all users "
                   f"{fmt_bytes(used)} of {fmt_bytes(CACHE_LIMIT)}", class_="bc-note"),
            ui.div(ui.div(style=f"width:{pct:.1f}%"), class_="bc-meter"))

    @render.ui
    def files_note():
        m = manifest()
        if not m:
            return ui.p("No files yet.", class_="bc-note")
        ok = [x for x in m if x["status"] == "ok"]
        bad = [x for x in m if x["status"] != "ok"]
        hrs = sum(x["duration_s"] or 0 for x in ok) / 3600
        out = [ui.p(f"{len(m)} files · {len(ok)} usable · {hrs:.2f} h of audio · "
                    f"{fmt_bytes(sum(x['size'] for x in m))}", class_="bc-note")]
        if bad:
            out.append(ui.p(f"⚠ {len(bad)} file(s) cannot be analysed (empty or unreadable). Check the recorder's "
                            "card and battery — this often means the device stopped writing.", class_="bc-bad"))
        return ui.TagList(*out)

    @render.data_frame
    def files_table():
        m = manifest()
        if not m:
            return pd.DataFrame()
        df = pd.DataFrame(m)
        df["size"] = df["size"].map(fmt_bytes)
        df["start"] = df["start"].str.replace("T", " ") + np.where(df["start_is_utc"], " UTC", "")
        return render.DataGrid(df[["name", "status", "size", "duration_s", "samplerate", "start", "start_source",
                                   "lat", "lon", "note", "problem"]], filters=True, height="430px", width="100%")

    # ----- processing
    def job_state():
        p = proc["p"]
        return (mtime(ws / "job" / "status.json"), None if p is None else p.poll())

    @reactive.poll(job_state, 0.75)
    def status():
        now = time.time()
        if now - last_state["beat"] > 30:
            touch_alive(ws)
            last_state["beat"] = now
        st = read_json(ws / "job" / "status.json", None)
        p = proc["p"]
        if st and p is not None and p.poll() not in (None, 0) and st.get("state") in ("queued", "running"):
            log = (ws / "job" / "worker.log").read_text()[-600:] if (ws / "job" / "worker.log").exists() else ""
            st.update(state="error", message=f"The worker stopped unexpectedly. {log}")
        return st

    @reactive.effect
    @reactive.event(input.run)
    def _run():
        if proc["p"] and proc["p"].poll() is None:
            ui.notification_show("Already running.", type="warning")
            return
        m = manifest()
        if not [x for x in m if x["status"] == "ok"]:
            ui.notification_show("Upload at least one usable recording first.", type="error")
            ui.update_navset("tabs", selected="upload")
            return
        if input.loc_mode() == "manual" and (input.lat() is None or input.lon() is None):
            ui.notification_show("Enter latitude and longitude, or pick another location option.", type="error")
            return
        job = ws / "job"
        shutil.rmtree(job, ignore_errors=True)
        job.mkdir()
        write_json(job / "params.json", dict(
            loc_mode=input.loc_mode(), lat=input.lat(), lon=input.lon(), ts_mode=input.ts_mode(), tz=input.tz(),
            min_conf=input.min_conf(), sensitivity=input.sensitivity(), overlap=input.overlap(), started=now_iso()))
        log = open(job / "worker.log", "w")
        proc["p"] = subprocess.Popen([sys.executable, str(APP_FILE), "process", str(ws)], stdout=log,
                                     stderr=subprocess.STDOUT, cwd=str(APP_DIR))
        downloaded.set(False)
        ui.update_slider("thr", min=input.min_conf(), value=max(input.min_conf(), input.thr()))
        ui.update_slider("rev_band", value=(input.min_conf(), max(input.min_conf() + 0.05, 0.5)))

    @reactive.effect
    @reactive.event(input.cancel)
    def _cancel():
        if proc["p"] and proc["p"].poll() is None:
            (ws / "job" / "cancel").touch()
            ui.notification_show("Cancelling after the current file…")

    @reactive.effect
    async def _state_change():
        st = status()
        s = st.get("state") if st else None
        if s == last_state["s"]:
            return
        prev, last_state["s"] = last_state["s"], s
        if prev is None and s in ("done", "error", "cancelled"):
            return
        if s == "done":
            ui.notification_show(st["message"] + " Next: verify low-confidence calls.", type="message", duration=12)
            await session.send_custom_message("bc-notify", {"title": "Processing complete", "body": st["message"]})
        elif s == "error":
            ui.notification_show(st["message"], type="error", duration=None)
            await session.send_custom_message("bc-notify", {"title": "Processing failed", "body": st["message"][:200]})
        await send_quota()

    @render.ui
    def job_status():
        st = status()
        if not st:
            return ui.p("Not started. Settings on the left; the model runs in the background so you can keep working.",
                        class_="bc-note")
        pct = float(st.get("pct") or 0)
        eta = ""
        if st["state"] == "running" and st.get("done"):
            per = sum(x["seconds"] for x in st["log"]) / max(len(st["log"]), 1)
            eta = f" · about {fmt_secs(per * (st['n_files'] - st['done']))} left"
        cls = {"error": "bc-bad", "done": "bc-good"}.get(st["state"], "")
        return ui.div(
            ui.div(ui.div(style=f"width:{pct:.1f}%"), class_="bc-progress"),
            ui.p(ui.tags.b(st["state"].upper()), f" · {st.get('done', 0)}/{st.get('n_files', 0)} files · "
                 f"{st.get('n_detections', 0)} detections{eta}", class_=cls),
            ui.p(st.get("message", ""), class_="bc-note"))

    @render.ui
    def job_check():
        st = status()
        if not st:
            return None
        stage, state = st.get("stage", "queued"), st.get("state")
        order = ["queued", "loading", "analysing", "finishing", "done"]
        cur = order.index(stage) if stage in order else 0
        elapsed = ""
        with contextlib.suppress(Exception):
            t0 = datetime.fromisoformat(st["started"].replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(st["finished"].replace("Z", "+00:00")) if st.get("finished") else datetime.now(timezone.utc)
            elapsed = fmt_secs((t1 - t0).total_seconds())
        steps = [  # (stage index that completes it, emoji, label, detail)
            (0, "📂", "Recordings checked", f"{st.get('n_files', 0)} usable · {st.get('n_skipped', 0)} skipped"),
            (1, "🧠", "BirdNET model loaded", "GLOBAL 6K v2.4"),
            (2, "🎧", "Listening to recordings", f"{st.get('done', 0)}/{st.get('n_files', 0)} files"),
            (2, "✂️", "Clips & spectrograms cut", f"{st.get('n_detections', 0)} detections"),
            (3, "📊", "Results saved", ""),
            (4, "🔔", "Done — you've been notified" if state == "done" else "Done", elapsed),
        ]
        lis = []
        for idx, emo, label, detail in steps:
            if state == "error" and idx >= cur:
                cls, ic = ("err", "❌") if idx == cur else ("", "⬜")
            elif state == "cancelled" and idx >= cur:
                cls, ic = ("err", "⏹️") if idx == cur else ("", "⬜")
            elif state == "queued" and idx == 0:
                cls, ic, label, detail = "now", '<span class="spin">⏳</span>', "Waiting for a free slot", "other users are processing"
            elif idx < cur or stage == "done":
                cls, ic = "done", "✅"
            elif idx == cur:
                cls, ic = "now", f'<span class="spin">🔄</span>'
            else:
                cls, ic = "", "⬜"
            lis.append(f'<li class="{cls}"><span class="ic">{ic}</span><span>{emo} {html.escape(label)}</span>'
                       f'<span class="ds">{html.escape(detail)}</span></li>')
        return ui.HTML(f'<ul class="bc-check" aria-live="polite">{"".join(lis)}</ul>')

    # ----- demo data
    def load_demo():
        files_ = sorted(DEMO_DIR.glob("*.wav")) if DEMO_DIR.exists() else []
        if not files_:
            ui.notification_show("No demo recordings are bundled with this installation.", type="warning")
            return
        if proc["p"] and proc["p"].poll() is None:
            ui.notification_show("Wait for the current run to finish (or cancel it) before loading demo data.", type="warning")
            return
        for f in files_:
            shutil.copy(f, ws / "audio" / f.name)
        manifest.set(build_manifest(ws))
        ui.update_navset("tabs", selected="upload")
        ui.notification_show(f"🦜 Loaded {len(files_)} demo recordings (King-Parrot and friends). Next: Process → Run.",
                             type="message", duration=8)

    @reactive.effect
    @reactive.event(input.demo)
    def _demo():
        load_demo()

    @reactive.effect
    @reactive.event(input.demo2)
    def _demo2():
        load_demo()

    # ----- end session / stop app
    @reactive.effect
    @reactive.event(input.end_session)
    def _end_session():
        ui.modal_show(ui.modal(
            ui.p("This deletes your uploads, results and checks from the server now."),
            ui.p(ui.tags.b("Downloaded your PDF report, CSV bundle and site profile? "), "They're on ",
                 ui.tags.b("4 · Results → Report"), "." if downloaded() else " — not downloaded yet in this session.",
                 class_="bc-good" if downloaded() else "bc-bad"),
            title="End this session?", easy_close=True,
            footer=ui.div(
                ui.modal_button("Keep working"),
                ui.input_action_button("end_confirm", "End session & delete data", class_="btn-danger"),
                ui.input_action_button("stop_app", "Stop the app", class_="btn-outline-danger",
                                       title="Local use: shuts the server down") if ALLOW_STOP else None,
                style="display:flex;gap:8px")))

    async def _finish(stopped: bool):
        ui.modal_remove()
        cleanup()
        txt = ("The server has been shut down. Close this tab; start it again from your terminal." if stopped else
               "Your recordings, results and checks were deleted from the server.")
        await session.send_custom_message("bc-ended", {"stopped": stopped, "text": txt})

    @reactive.effect
    @reactive.event(input.end_confirm)
    async def _end_confirm():
        await _finish(False)
        await session.close()

    @reactive.effect
    @reactive.event(input.stop_app)
    async def _stop_app():
        if not ALLOW_STOP:
            return
        await _finish(True)
        import asyncio
        import signal
        asyncio.get_running_loop().call_later(0.8, os.kill, os.getpid(), signal.SIGINT)

    @render.data_frame
    def job_log():
        st = status()
        if not st or not st.get("log"):
            return pd.DataFrame()
        return render.DataGrid(pd.DataFrame(st["log"]), height="320px", width="100%")

    # ----- data
    @reactive.poll(lambda: (mtime(ws / "job" / "detections.csv"), mtime(ws / "job" / "files.csv")), 1.0)
    def det_all():
        return load_det(ws)

    @reactive.calc
    def files():
        det_all()
        return load_files(ws)

    @reactive.calc
    def ver():
        ver_bump()
        return load_ver(ws)

    @reactive.effect
    def _species_choices():
        d = with_decisions(det_all(), ver())
        sp = sorted(d["common"].dropna().unique().tolist())
        orig = sorted(d["original_common"].dropna().unique().tolist())
        with reactive.isolate():
            keep_sp, keep_rev = list(input.species_sel() or []), list(input.rev_species() or [])
        # Only push updates when the lists change: re-sending a selectize re-triggers the review grid,
        # which would redraw every card (stopping playback) after each verification.
        if sp != choice_cache.get("sp"):
            choice_cache["sp"] = sp
            ui.update_selectize("species_sel", choices=sp, selected=[x for x in keep_sp if x in sp])
        if orig != choice_cache.get("orig"):
            choice_cache["orig"] = orig
            ui.update_selectize("rev_species", choices=orig, selected=[x for x in keep_rev if x in orig])

    @reactive.effect
    async def _dirty():
        await session.send_custom_message("bc-dirty", {"dirty": (not det_all().empty) and not downloaded()})

    # ----- verification
    @reactive.effect
    @reactive.event(input.reshuffle)
    def _reshuffle():
        rev_seed.set(rev_seed() + 1)
        ui.update_select("rev_order", selected="random")

    @reactive.calc
    def pool():
        lo, hi = input.rev_band()
        return review_pool(det_all(), ver(), lo, hi)

    @render.ui
    def rev_progress():
        p = pool()
        if det_all().empty:
            return ui.p("Run processing first.", class_="bc-note")
        done = int((p["decision"] != "pending").sum())
        pct = 100 * done / max(len(p), 1)
        return ui.div(ui.p(f"{done} of {len(p)} detections in this band verified "
                           f"({p['original_common'].nunique()} species). Clips are volume-normalised for listening; "
                           "headphones recommended.", class_="bc-note"),
                      ui.div(ui.div(style=f"width:{pct:.1f}%;background:var(--good)"), class_="bc-meter"))

    @render.ui
    def rev_cards():
        rev_seed()
        with reactive.isolate():
            p = pool()
        lo, hi = input.rev_band()
        input.rev_n(), input.rev_order(), input.rev_status(), input.rev_species()
        det_all()
        s = review_sample(p, int(input.rev_n() or 6), input.rev_order(), rev_seed(),
                          list(input.rev_species() or []), input.rev_status())
        if s.empty:
            return ui.p(f"No detections between {lo:.2f} and {hi:.2f}.", class_="bc-note")
        cap = 150
        note = ui.p(f"Showing first {cap} of {len(s)} clips — narrow by species.", class_="bc-note") if len(s) > cap else None
        return ui.TagList(note, ui.HTML('<div class="bc-grid">' + "".join(card_html(r, sid) for r in s.head(cap).itertuples())
                                        + "</div>"))

    @reactive.effect
    @reactive.event(input.verify_action)
    async def _verify():
        a = input.verify_action()
        d = det_all()
        row = d[d["det_id"] == a["id"]]
        if row.empty:
            return
        r = row.iloc[0]
        new_sci = new_common = ""
        if a["decision"] == "relabel":
            parsed = parse_label_choice(a["label"])
            if not parsed:
                ui.notification_show(f"'{a['label']}' is not a BirdNET species. Pick one from the list.", type="error")
                return
            new_sci, new_common = parsed
        append_ver(ws, ver_row(r, a["decision"], new_sci, new_common, a.get("verifier", "")))
        text = {"confirm": "✓ Confirmed", "reject": "✗ Rejected", "relabel": f"↺ Relabelled → {new_common}",
                "clear": "Unverified"}[a["decision"]]
        await session.send_custom_message("bc-card", {"id": a["id"], "decision":
                                                      "pending" if a["decision"] == "clear" else a["decision"], "text": text})
        ver_bump.set(ver_bump() + 1)
        downloaded.set(False)

    # ----- site profile
    @reactive.calc
    def profile():
        profile_bump()
        return load_profile(ws)

    @reactive.effect
    @reactive.event(input.profile_up)
    def _profile_up():
        f = (input.profile_up() or [None])[0]
        if not f:
            return
        try:
            info = import_profile(ws, json.loads(Path(f["datapath"]).read_text()))
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"Could not load the profile: {e}", type="error", duration=10)
            return
        profile_bump.set(profile_bump() + 1)
        ver_bump.set(ver_bump() + 1)
        ui.update_text("profile_name", value=info["name"])
        ui.notification_show(f"Loaded site profile '{info['name'] or 'unnamed'}': {info['rules']} species rule(s), "
                             f"{info['verifications']} past check(s).", type="message", duration=8)

    @render.ui
    def profile_status():
        pr = profile()
        n_rules, n_ver = len(pr["species"]), len(ver())
        if not pr["name"] and not n_rules and not n_ver:
            return ui.p("No profile loaded. After verifying, download one from Results → Reliability to reuse your "
                        "checks and thresholds next time.", class_="bc-note")
        return ui.p(f"✓ Profile '{pr['name'] or 'unnamed'}': {n_rules} species rule(s), {n_ver} check(s) in use.",
                    class_="bc-good")

    @reactive.calc
    def rel():
        return reliability(det_all(), ver(), profile(), float(input.target()), int(input.min_n() or 5))

    @render.data_frame
    def t_rel():
        return render.DataGrid(rel().drop(columns=["scientific"]), filters=True, height="320px", width="100%")

    @reactive.effect
    def _rule_choices():
        d, pr = det_all(), profile()
        sp = {}
        if not d.empty:
            sp.update(dict(zip(d["scientific"], d["common"])))
        sp.update({k: v.get("common", k) for k, v in pr["species"].items()})
        with reactive.isolate():
            cur = input.rule_sp()
        choices = {k: f"{v} ({k})" for k, v in sorted(sp.items(), key=lambda kv: kv[1])}
        if choices != choice_cache.get("rule"):
            choice_cache["rule"] = choices
            ui.update_selectize("rule_sp", choices=choices, selected=cur if cur in choices else None)

    def _rule(action, value=None):
        sp = input.rule_sp()
        if not sp:
            ui.notification_show("Pick a species first.", type="warning")
            return
        set_rule(ws, sp, action, value)
        profile_bump.set(profile_bump() + 1)
        downloaded.set(False)

    @reactive.effect
    @reactive.event(input.rule_set)
    def _rule_set():
        _rule("threshold", float(input.rule_thr() or 0.5))

    @reactive.effect
    @reactive.event(input.rule_block)
    def _rule_block():
        _rule("block")

    @reactive.effect
    @reactive.event(input.rule_clear)
    def _rule_clear():
        _rule("allow")

    def make_profile():
        pr = load_profile(ws)
        pr["name"] = input.profile_name() or pr["name"]
        save_profile(ws, pr)
        out = ws / "site_profile.json"
        write_json(out, export_profile(ws, det_all(), ver(), float(input.target()), int(input.min_n() or 5)))
        return str(out)

    profile_fname = lambda: f"site_profile_{re.sub(r'[^A-Za-z0-9]+', '_', input.profile_name() or 'site')}.json"  # noqa: E731

    @render.download_button(filename=profile_fname)
    def dl_profile():
        return make_profile()

    @render.download_button(filename=profile_fname)
    def dl_profile2():
        return make_profile()

    # ----- results
    @reactive.calc
    def settings():
        th, blocked = species_rules(rel())
        return dict(thr=input.thr(), override=input.override(), verified_only=input.verified_only(),
                    gap=float(input.gap() or 0), species=list(input.species_sel() or []),
                    per_species=input.per_species(), thresholds=th, blocked=blocked,
                    target=float(input.target()), min_n=int(input.min_n() or 5))

    @reactive.calc
    def R():
        return compute(det_all(), ver(), files(), settings())

    @render.ui
    def kpis():
        r = R()
        dec = with_decisions(r["det_all"], r["ver"])
        return ui.div(
            kpi(f"{int((r['files']['status'] == 'analysed').sum()) if not r['files'].empty else 0}",
                f"files analysed · {r['hours']:.1f} h audio"),
            kpi(r["det"]["common"].nunique(), "species"),
            kpi(len(r["det"]), "detections (3-s windows)"),
            kpi(len(r["ev"]), "call events"),
            kpi(int((dec["decision"] != "pending").sum()), f"verified of {len(dec)} raw"),
            class_="bc-kpis")

    def fig1(fn, *args):
        fig, ax = plt.subplots()
        fn(ax, *args)
        fig.tight_layout()
        return fig

    @render.plot
    def p_species():
        return fig1(plot_species, R()["summary"])

    @render.plot
    def p_hourly():
        return fig1(plot_hourly, R()["ev"])

    @render.plot
    def p_conf():
        return fig1(plot_conf, with_decisions(det_all(), ver()).query("decision != 'reject'"), input.thr())

    @render.plot
    def p_timeline():
        return fig1(plot_timeline, R()["det"], files())

    @render.plot
    def p_daily():
        return fig1(plot_daily, R()["daily"])

    @render.plot
    def p_freq():
        return fig1(plot_freq, R()["summary"])

    @render.plot
    def p_gaps():
        return fig1(plot_gaps, R()["ev"])

    @render.data_frame
    def t_species():
        s = R()["summary"].copy()
        for c in ("first_heard", "last_heard"):
            s[c] = pd.to_datetime(s[c]).dt.strftime("%Y-%m-%d %H:%M:%S")
        return render.DataGrid(s, filters=True, height="520px", width="100%")

    @render.data_frame
    def t_rejected():
        return render.DataGrid(R()["rejected"], height="200px", width="100%")

    @render.data_frame
    def t_sites():
        st = R()["sites"].copy()
        for c in ("first_recording", "last_recording"):
            st[c] = pd.to_datetime(st[c]).dt.strftime("%Y-%m-%d %H:%M")
        return render.DataGrid(st, filters=True, height="300px", width="100%")

    @render.data_frame
    def t_daily():
        return render.DataGrid(R()["daily"], filters=True, height="300px", width="100%")

    def events_view():
        ev = R()["ev"].copy()
        ev["start"] = ev["start_dt"].dt.strftime("%Y-%m-%d %H:%M:%S")
        ev["end"] = ev["end_dt"].dt.strftime("%H:%M:%S")
        return ev

    @render.data_frame
    def t_events():
        ev = events_view()
        return render.DataGrid(ev[["common", "species_id", "start", "end", "duration_s", "windows", "max_conf",
                                   "gap_to_next_s", "verified", "site", "file", "start_s"]], filters=True, selection_mode="row",
                               height="420px", width="100%")

    @render.ui
    def event_player():
        sel = t_events.cell_selection()["rows"]
        if not sel:
            return None
        ev = events_view().iloc[sel[0]]
        d = R()["det"]
        hit = d[(d["file"] == ev["file"]) & (d["common"] == ev["common"]) & (d["start_s"] >= ev["start_s"])]
        return ui.HTML(player_html(hit.sort_values("start_s").iloc[0], sid)) if not hit.empty else None

    def det_view():
        d = R()["det"].copy()
        d["datetime"] = d["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")
        return d

    @render.data_frame
    def t_det():
        return render.DataGrid(det_view()[["datetime", "common", "scientific", "species_id", "sci_code", "confidence",
                                           "decision", "peak_hz", "low_hz", "high_hz", "snr_db", "site", "lat", "lon",
                                           "file", "start_s", "det_id"]], filters=True, selection_mode="row", height="460px", width="100%")

    @render.ui
    def det_player():
        sel = t_det.cell_selection()["rows"]
        return ui.HTML(player_html(det_view().iloc[sel[0]], sid)) if sel else None

    @render.ui
    def examples():
        d = R()["det"]
        if d.empty:
            return ui.p("No detections match the current filters.", class_="bc-note")
        best = d.sort_values("confidence", ascending=False).groupby("common").head(3).sort_values(["common", "confidence"],
                                                                                                    ascending=[True, False])
        return ui.HTML('<div class="bc-grid">' + "".join(card_html(r, sid) for r in best.itertuples()) + "</div>")

    @render.ui
    def report_panel():
        r = R()
        return ui.div(
            ui.h4("Download before you leave"),
            ui.p("The PDF uses the filters currently set in the sidebar. It includes: summary and settings, "
                 "species table with IDs, calls per species, timeline, daily counts, hourly activity, frequency ranges, "
                 "time between calls, confidence distribution, call timestamps, example spectrograms, "
                 "your verification log and the file list."),
            ui.p(f"Current selection: {r['det']['common'].nunique()} species, {len(r['det'])} detections, "
                 f"{len(r['ev'])} call events.", class_="bc-note"),
            ui.div(ui.download_button("dl_pdf2", "⬇ Download PDF report", class_="btn-primary"),
                   ui.download_button("dl_zip2", "⬇ Download CSV bundle", class_="btn-outline-primary"),
                   ui.download_button("dl_profile2", "⬇ Download site profile", class_="btn-outline-primary"),
                   ui.download_button("dl_session2", "⬇ Session info", class_="btn-outline-secondary"),
                   style="display:flex;gap:10px;flex-wrap:wrap"),
            ui.p("The site profile is a small JSON file holding your checks and per-species thresholds. "
                 "Load it on the Upload tab next time so the app starts from what you already verified.",
                 class_="bc-note", style="margin-top:8px"),
            ui.p("✓ Downloaded in this session." if downloaded() else
                 "Not downloaded yet — your data will be lost when this session ends.",
                 class_="bc-good" if downloaded() else "bc-bad", style="margin-top:10px"))

    def make_pdf():
        with ui.Progress() as p:
            p.set(message="Building PDF report…", detail="charts, tables and spectrograms")
            out = build_report(ws, settings(), ws / "report.pdf")
        downloaded.set(True)
        return str(out)

    def make_zip():
        out = export_zip(ws, R(), settings(), ws / "results.zip")
        return str(out)

    stamp = lambda: datetime.now().strftime("%Y%m%d_%H%M")  # noqa: E731

    @render.download_button(filename=lambda: f"bruting_calls_report_{stamp()}.pdf")
    def dl_pdf():
        return make_pdf()

    @render.download_button(filename=lambda: f"bruting_calls_report_{stamp()}.pdf")
    def dl_pdf2():
        return make_pdf()

    @render.download_button(filename=lambda: f"bruting_calls_results_{stamp()}.zip")
    def dl_zip():
        return make_zip()

    @render.download_button(filename=lambda: f"bruting_calls_results_{stamp()}.zip")
    def dl_zip2():
        return make_zip()

    @reactive.effect
    @reactive.event(input.start)
    def _start_btn():
        ui.update_navset("tabs", selected="upload")

    @render.download_button(filename=lambda: f"session_info_{APP_TITLE.replace(' ', '_')}_v{APP_VERSION}_"
                                             f"{datetime.now():%Y%m%d_%H%M%S}.txt")
    def dl_session():
        return make_session_info()

    @render.download_button(filename=lambda: f"session_info_{APP_TITLE.replace(' ', '_')}_v{APP_VERSION}_"
                                             f"{datetime.now():%Y%m%d_%H%M%S}.txt")
    def dl_session2():
        return make_session_info()

    def make_session_info():
        m = manifest()
        extra = ["", "── This session ──", f"Workspace files: {len(m)} ({sum(x['size'] for x in m) / 1024**2:.1f} MB)",
                 *[f"  {x['name']}  sha1 {x.get('sha1') or '–'}  {x['status']}" for x in m],
                 f"Processing parameters: {json.dumps(read_json(ws / 'job' / 'params.json', {}) or {})}",
                 f"Report settings: {json.dumps(settings())}"]
        out = ws / "session_info.txt"
        out.write_text(session_info_text(extra))
        return str(out)

    # ----- workflow bar
    @reactive.effect
    @reactive.event(input.goto)
    def _goto():
        g = input.goto()
        if g == "report":
            ui.update_navset("tabs", selected="results")
            ui.update_navset("res_tabs", selected="report")
        else:
            ui.update_navset("tabs", selected=g)

    @render.ui
    def steps():
        m, st = manifest(), status()
        ok = [x for x in m if x["status"] == "ok"]
        s = st.get("state") if st else None
        d = det_all()
        p = pool() if not d.empty else pd.DataFrame(columns=["decision"])
        n_ver = int((p["decision"] != "pending").sum())
        items = [
            ("upload", "Upload", "done" if ok else "active",
             f"{len(ok)} usable file(s)" if m else "add recordings", None),
            ("process", "Process",
             {"done": "done", "error": "error", "running": "active", "queued": "active"}.get(s, "active" if ok else ""),
             {"running": f"{st['pct']:.0f}% · {st['done']}/{st['n_files']}" if st else "", "queued": "queued…",
              "done": f"{st['n_detections']} detections" if st else "", "error": "failed",
              "cancelled": "cancelled"}.get(s, "ready" if ok else "waiting"),
             st["pct"] if s == "running" else None),
            ("verify", "Verify", "done" if len(p) and n_ver == len(p) else ("active" if s == "done" else ""),
             f"{n_ver}/{len(p)} checked" if len(p) else "low-confidence clips", None),
            ("results", "Results", "done" if s == "done" and not d.empty else ("active" if s == "done" else ""),
             f"{d['common'].nunique()} species (raw)" if not d.empty else "charts & tables", None),
            ("report", "Report", "done" if downloaded() else ("active" if s == "done" else ""),
             "downloaded ✓" if downloaded() else "download PDF", None),
        ]
        lis = []
        for i, (key, lbl, state, sub, pct) in enumerate(items, 1):
            mark = "✓" if state == "done" else ("!" if state == "error" else str(i))
            bar = f'<div class="mini" style="width:{pct:.0f}%"></div>' if pct is not None else ""
            run = " bc-running" if key == "process" and s in ("running", "queued") else ""
            lis.append(f'<li class="bc-step {state}{run}" data-goto="{key}" title="Go to {lbl}"><span class="dot">{mark}</span>'
                       f'<span><div class="lbl">{lbl}</div><div class="st">{html.escape(sub)}</div></span>{bar}</li>')
        return ui.HTML(f'<ol class="bc-steps">{"".join(lis)}</ol>')


async def asyncio_to_thread(fn, *a):
    import asyncio
    return await asyncio.to_thread(fn, *a)


WWW_DIR.mkdir(exist_ok=True)
app = App(app_ui, server, static_assets={"/ws": str(CACHE_ROOT), "/static": str(WWW_DIR)})
