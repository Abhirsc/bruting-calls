---
title: Bruting Calls
emoji: 🦜
colorFrom: yellow
colorTo: green
sdk: docker
app_port: 8000
pinned: false
license: mit
short_description: BirdNET bird-call detection, verification & reports
---

# Bruting Calls

**v0.4.0** · part of **Demeter Insights** · by Abhimanyu Raj Singh · MIT licence

A Shiny app that finds bird calls in field recordings (built for Smith Robotics *Demeter* recorders) with
[BirdNET](https://github.com/kahst/BirdNET-Analyzer). It lets a person listen to and verify uncertain detections,
and produces a PDF report with charts, tables and spectrograms.

It comes in two front-ends that give identical results:

| File | Front-end | Notes |
|---|---|---|
| `app.py` | **Shiny for Python** | Single script: UI, server, BirdNET worker, statistics and PDF report. |
| `app.R`  | **Shiny for R** | Single script UI/server; calls `app.py` (CLI mode) for BirdNET, metadata and the PDF. |

Both open on an **About** tab. It covers:
- what the app does, the inputs it needs, and its limits and runtime
- data provenance, how to cite it, and the licence
- a **data dictionary** for every exported column
- a **Download session info** button (app, R/Python and package versions, BirdNET model checksums)

## Requirements

| | |
|---|---|
| Input | WAV (16-bit PCM), FLAC or OGG; mono or stereo; any sample rate. GUANO metadata recommended (start time, GPS). |
| Typical size | 5-minute 48 kHz files, ~28 MB each |
| Limits | Temporary storage shared by all users (default 20 GB, set with `BRUTING_CACHE_GB`); data kept only for the browser session |
| Storage guide | ≈ 32 MB per 5-min 48 kHz file (28.8 MB audio + ~3 MB clips). 20 files a night ≈ 0.65 GB, so 20 GB ≈ 4 weeks of nights (≈ 0.65 GB each). A continuous 24 h day ≈ 9 GB |
| Runtime | ~4–6 s per 5-minute file on a recent laptop CPU; 2 jobs at once by default |
| Software | Python ≥ 3.10 (`requirements.txt`, exact versions in `requirements-lock.txt`); for `app.R` also R ≥ 4.3 (`renv.lock`) |

## Workflow

A live steps bar at the top shows progress: **Upload → Process → Verify → Results → Report**. Click any step to jump to it.

1. **Upload** — select many WAV/FLAC/OGG files at once. The page shows an upload progress bar. GUANO recorder metadata (start time, GPS, device, note) is read automatically. Empty (0-byte) or unreadable files are flagged.
2. **Process** — BirdNET (GLOBAL 6K v2.4) runs in a background worker process, so the page stays responsive. You see a progress bar, the current file, an estimate of time left and a per-file log. When it finishes you get an in-app toast, a browser notification, a short beep and a flashing tab title.
3. **Verify** — sample low-confidence detections (choose the confidence band, clips per species, and how clips are picked). Each card shows a waveform and spectrogram with a **play button**. Mark each one **Confirm / Reject / Relabel** (the species list autocompletes) or undo it.
4. **Results** — set a custom **confidence threshold** and choose whether verified calls are always kept or only verified calls are shown. Tabs:
   - **Species & IDs**: BirdNET class index (`BN6140`), 6-letter code (`TRIMOL`), common and scientific names.
   - **Counts**: detections and call events, in total and **per day**.
   - **Calls per hour**, time of first and last call.
   - **Call timestamps and duration**; **time between calls** (silence to the next call of the same species).
   - **Call frequency (pitch)**: dominant frequency in kHz and the 5–95 % energy band.
   - **Charts**: timeline, hourly activity, confidence histogram and example spectrograms. Click any table row to hear that call.
5. **Report** — download the **PDF report** and a **CSV bundle** (all tables + settings + verification log).

> ⚠ **Data is temporary.** Each browser session gets its own workspace, deleted when the session ends (tab closed or refreshed, or the connection lost).
> A banner, a start-up dialog and a "leave page?" prompt remind users to download the PDF report.

### Site profile: the app learns from your checks

Each Confirm / Reject / Relabel tells the app whether BirdNET was right. On **Results → Reliability**, the app shows per species:
- how many calls you checked, and what share BirdNET got right (precision)
- the **learned threshold**: the lowest confidence that reaches your target, by default 90 % correct from at least 5 checks

You can also set a threshold by hand or **block** a species.

**Download the site profile** (`site_profile.json`). Next session, load it on the Upload tab: your past checks are applied again (same recording + time + species), and the per-species thresholds are used straight away.
Nothing is stored on the server; the profile is a small file you keep. This is calibration, not retraining: BirdNET itself stays the same, but the app reports with thresholds tuned to your site.

### Try it without your own data

Click **🦜 Try with demo data** on the About tab. It loads three short iNaturalist recordings from NSW with recorder-style GUANO metadata:
- a clean Australian King-Parrot
- two busy dawn/dusk choruses with Whipbird, Bowerbird, Bell Miner and others

Sources and licences are in [`demo/ATTRIBUTION.md`](demo/ATTRIBUTION.md).

### Multiple sites and Darwin Core

- **Each file keeps its own location** from the recorder's GPS (GUANO `Loc Position`), so one upload can mix sites.
  Typed coordinates fill in files without GPS, or override every file if you pick that option.
- Every detection and call event carries `site`, `lat`, `lon`, `location_source` and `device`. Sites are listed on the **Sites** tab and in `sites.csv`.
- `darwin_core/` in the CSV bundle is a Darwin Core Archive (`occurrence.csv` + `meta.xml` + `eml.xml`). It has one `MachineObservation` per call event, ready for the GBIF IPT or ALA.
  Before publishing, set the data licence in `eml.xml`. Rejected detections are never exported.

### Definitions used

- **Detection** — one 3-second BirdNET window at or above the threshold.
- **Call event** — consecutive or overlapping detections of one species in one recording, merged. The merge gap is adjustable.
- **Gap to next** — the silence between one call event and the next of the same species in the same recording.
- **Calls per hour** — call events ÷ hours of audio analysed.
- **Frequency** — the per-detection spectrum minus the recording's median background noise, so steady hiss doesn't dominate.
  - **Peak** is the loudest remaining frequency.
  - **Band** is where 5–95 % of the remaining energy lies.

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # or requirements-lock.txt for the exact tested versions
BRUTING_ALLOW_STOP=1 shiny run app.py   # → http://127.0.0.1:8000 (Stop button enabled for local use)
```

R version (uses the same Python environment for BirdNET):

```bash
Rscript -e 'renv::restore()'      # exact versions from renv.lock (or: Rscript install.R)
BRUTING_PYTHON=.venv/bin/python Rscript -e "shiny::runApp('app.R')"
```

## UI tests (Playwright)

```bash
pip install -r requirements-dev.txt && python -m playwright install chromium webkit
pytest tests/ --browser chromium --browser webkit
```

The tests cover the workflow end to end: warning dialog → upload (file picker and drag & drop) → BirdNET run → confirm / reject / relabel → threshold → PDF and ZIP downloads. They also check the mobile layout. Set `BRUTING_TEST_AUDIO` to a folder containing the test recordings.

## Deploy on a shared server

> The block between `---` lines at the top of this README is configuration for Hugging Face Spaces. GitHub shows it as a small table; leave it in place.

### Docker (any host)

```bash
docker build -t bruting-calls .                                # Python app (default)
docker run -p 8000:8000 bruting-calls
docker build --build-arg WITH_R=true -t bruting-calls-r .      # with R + app.R
docker run -p 8000:8000 -e APP=r bruting-calls-r
```

### Hugging Face Space (Docker)

1. Create a Space: **SDK = Docker**, **Private** recommended (the app has no login). Docker Spaces need HF PRO.
2. Upload this folder with the `hf` CLI. A plain `git push` is rejected because the repo contains binary files (WAV, JPG, M4A); `hf upload` stores them correctly for you:
   ```bash
   hf auth login
   hf upload <user>/bruting-calls . . --repo-type=space --exclude=".git/*" --exclude="cache/*" --exclude="__pycache__/*" --exclude=".venv/*"
   ```
   Re-run the same `hf upload` after each new release.
3. It builds from the `Dockerfile` and serves on port 8000 (`app_port` above). CPU Basic gives 2 vCPU, 16 GB RAM and 50 GB of non-persistent disk, which fits the 20 GB cache.
4. Optional **Settings → Variables**: `BRUTING_CACHE_GB`, `BRUTING_MAX_JOBS`. Never set `BRUTING_ALLOW_STOP`.

### Posit Connect Cloud

1. connect.posit.cloud → **Publish** → **Shiny** → this GitHub repo, branch `main`, primary file **`app.py`**. It installs `requirements.txt`.
2. **Advanced settings → Variables**: `BRUTING_CACHE=/tmp/bruting-cache`, `BRUTING_CACHE_GB=15`, `BRUTING_MAX_JOBS=1`.
   The free plan has 1 CPU, 4 GB RAM and 24 GiB disk *including the software*, so 20 GB of cache and two TensorFlow jobs don't fit.
3. Free-plan content is public. Republishing from GitHub picks up new commits.

Settings (environment variables):

| Variable | Default | Meaning |
|---|---|---|
| `BRUTING_CACHE` | `./cache` | Temporary workspace root (one sub-folder per session) |
| `BRUTING_CACHE_GB` | `20` | Total cache limit shared by all users. Uploads that would exceed it are blocked in the browser before sending. |
| `BRUTING_MAX_JOBS` | `2` | BirdNET jobs that may run at once. Extra jobs wait in a queue. |
| `BRUTING_ORPHAN_HOURS` | `2` | Workspaces with no heartbeat for this long are deleted, e.g. after a server crash. |
| `BRUTING_TZ` | `Australia/Sydney` | Default time zone |
| `BRUTING_PYTHON` | `python3` | Python interpreter used by `app.R` |
| `BRUTING_ALLOW_STOP` | unset | `1` shows a **Stop the app** button (local use only — never on a shared server) |

**Security:** there is no login. Put it behind your organisation's authentication (a reverse proxy, Posit Connect or shinyapps.io auth) if recordings are sensitive.

## CLI (used by `app.R`, also handy for batch jobs)

```bash
python app.py inspect <workspace>                         # read metadata → manifest.json
python app.py process <workspace>                         # run BirdNET (reads job/params.json)
python app.py report  <workspace> settings.json out.pdf   # build the PDF report
python app.py zip     <workspace> settings.json out.zip   # CSV bundle
```

A workspace holds `audio/` with the recordings, `manifest.json`, and `job/params.json` with the processing settings.

## Repository layout

```
app.py                 # Shiny for Python app + BirdNET worker + report (single script)
app.R                  # Shiny for R app (single script; uses app.py for BirdNET)
DESCRIPTION            # R project metadata: name, version, author, licence, R version
renv.lock              # pinned R package versions
requirements.txt       # Python dependencies (ranges)
requirements-lock.txt  # exact Python versions used for the release
requirements-dev.txt   # test tooling (pytest, Playwright)
README.md  LICENSE  CITATION.cff  CHANGELOG.md
Dockerfile  install.R  tests/
www/                   # About-page photo and King Parrot call (+ waveform JSON)
demo/                  # bundled demo recordings + ATTRIBUTION.md
```

## FAIR

| Principle | How this app meets it |
|---|---|
| Findable | Clear name and README; tagged releases (`v0.1.0`); archive each release on Zenodo for a DOI (then add it to `CITATION.cff`) |
| Accessible | MIT licence; all dependencies listed; run instructions above; Docker image |
| Interoperable | Open formats only: WAV/FLAC/OGG in; CSV, JSON, TXT and PDF out; every column documented with units (`data_dictionary.csv` in the bundle and on the About tab) |
| Reusable | `requirements-lock.txt` + `renv.lock`; `CITATION.cff`; `CHANGELOG.md`; session-info export. Provenance comes with every output: input SHA-1s, settings, app version and model checksums |

## Versioning

- `APP_VERSION` in `app.py` is the single source of truth. `app.R` reads it, so it is shown in the header, the PDF and the session info.
- When releasing, bump it together with `DESCRIPTION`, `CITATION.cff` and `CHANGELOG.md`, then tag `git tag v0.1.0`.
- PATCH = bug fix. MINOR = new compatible feature. MAJOR = breaking change to inputs or outputs.

## Citation

> Abhimanyu Raj Singh (2026). *Bruting Calls: BirdNET call detection, verification and reporting for Demeter acoustic recordings* (v0.4.0) [Software]. Demeter Insights.

Please also cite BirdNET: Kahl, S., Wood, C. M., Eibl, M. & Klinck, H. (2021). BirdNET: A deep learning solution for avian diversity monitoring. *Ecological Informatics* 61, 101236. https://doi.org/10.1016/j.ecoinf.2021.101236

## Credits

BirdNET by the K. Lisa Yang Center for Conservation Bioacoustics (Cornell Lab of Ornithology) and Chemnitz University of Technology, via [birdnetlib](https://github.com/joeweiss/birdnetlib). Check the BirdNET model licence (CC BY-NC-SA 4.0) before commercial use.
