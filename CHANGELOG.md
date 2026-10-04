# Changelog

All notable changes are listed here. Versions follow [semantic versioning](https://semver.org):
PATCH = bug fix, MINOR = new compatible feature, MAJOR = breaking change to inputs or outputs.
Bump `APP_VERSION` (app.py), `DESCRIPTION` and `CITATION.cff` together, then tag `vX.Y.Z`.

## [0.4.0] — 2026-10-04

### Added
- **70s pastel theme** taken from a King Parrot in a berry tree: tomato, mustard, avocado, teal and rose on cream, with Righteous + Nunito fonts.
  - Each workflow step and its tab share one colour, and results sub-tabs use the same pill style.
  - Charts use one teal hue.
- **About hero**: King Parrot photo (© Abhimanyu Raj Singh), a playable call bar with waveform (Petra Holland, iNaturalist, CC BY 4.0), and start buttons.
- **Demo data**: 3 bundled iNaturalist recordings with GUANO metadata (`demo/`, see `demo/ATTRIBUTION.md`). "Try with demo data" loads them in one click.
- **Next / Back buttons** on every tab.
- **Live processing checklist** with emoji status: files checked → model loaded → listening → clips → results saved → done.
- **↺ Start over** and **⏻ End session** (deletes your data now). **Stop the app** appears only when `BRUTING_ALLOW_STOP=1` (local use).
- Test that the page JavaScript is syntactically valid (a broken script silently disables verification and drag & drop).

### Changed
- Default temporary cache is now **20 GB** (`BRUTING_CACHE_GB`).
- `app.R` takes its CSS and JavaScript from `app.py` (`python app.py assets`), so both front-ends always match.

## [0.3.0] — 2026-10-04

### Added
- **Site profile** (`site_profile.json`), so the app learns from your verifications across sessions without keeping data on the server.
  - Download it from Results → Reliability or the Report tab; it's also in the CSV bundle.
  - Load it on the Upload tab next time: past checks are applied again and aren't asked twice.
- **Reliability** results tab, for each species:
  - checks, correct / wrong and precision (share confirmed)
  - a learned threshold: the lowest confidence that reaches the target precision (default 90 %, at least 5 checks)
  - manual thresholds and **Block species** rules
- "Per-species thresholds" switch (on by default). Species without a rule use the global threshold.
- `reliability.csv` in the bundle and a "Per-species reliability" page in the PDF.

- Verification is visible in the results:
  - "Calls per species" bars split verified and not-verified calls.
  - The timeline uses filled dots for verified calls and hollow dots for BirdNET-only ones.
  - The call-events table and PDF have a `verified` column.
  - A **Rejected by reviewer** list (screen, PDF, `rejected_species.csv`) shows species even when every call was rejected.

### Changed
- Verification records now store the detection's species, confidence, file, start and audio SHA-1, which the profile needs.

## [0.2.0] — 2026-10-04

### Added
- Coordinates on every row: `site`, `lat`, `lon`, `location_source` and `device` in the detections, call events and Detections table.
- Multi-site support: each file keeps its own recorder GPS (typed coordinates only fill files without GPS, unless "use for every file" is chosen).
  New **Sites** results tab, `sites.csv`, a per-site Daily counts table, a `sites` column in the species summary and a "Recording sites" PDF page.
- **Darwin Core export** (`darwin_core/occurrence.csv` + `meta.xml` + `eml.xml` in the CSV bundle) for GBIF / ALA:
  - one `MachineObservation` per call event
  - `eventDate` with UTC offset
  - WGS84 coordinates
  - `identificationVerificationStatus` taken from your Confirm/Reject decisions

### Changed
- `daily_counts.csv` gains a `site` column (counts are now per site; sum over sites for totals).

## [0.1.0] — 2026-10-04

First release.

### Added
- Shiny for Python (`app.py`) and Shiny for R (`app.R`) front-ends sharing one BirdNET engine.
- **About** tab (opens first): purpose, inputs, limits and runtime, data provenance, citation, licence, data dictionary.
- Session-info download (app, Python/R and package versions, BirdNET model checksums).
- Upload with multi-file picker, drag and drop, progress bar, 5 GB shared temporary cache, and GUANO metadata reading.
- Background BirdNET processing with a job queue, progress bar, time-left estimate and done notifications.
- Verification of low-confidence clips (spectrogram + playback): confirm, reject, relabel, undo.
- Results: confidence threshold, species IDs, counts in total and per day, call events, timestamps, durations,
  intervals, calls per hour, pitch (Hz), timeline, hourly activity and examples.
- PDF report and CSV bundle (with data dictionary, file manifest with SHA-1, settings, session info, citation).
- Temporary-data warnings (banner, start-up dialog, leave-page prompt) and per-session cleanup.
- Playwright UI tests (Chromium + WebKit) and a Dockerfile.
