"""
End-to-end UI tests for the Shiny for Python app.

    pip install pytest pytest-playwright && python -m playwright install chromium webkit
    pytest tests/ --browser chromium --browser webkit

Audio fixtures: set BRUTING_TEST_AUDIO to a folder of WAV recordings (default ../recording_output).
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

REPO = Path(__file__).resolve().parents[1]
AUDIO_DIR = Path(os.environ.get("BRUTING_TEST_AUDIO", REPO.parent / "recording_output"))
TEST_FILES = ["SRD00689_20260807_034523.wav", "SRD00689_20260807_044523.wav"]
EMPTY_FILE = "SRD00689_20260807_053023.wav"
LONG = 120_000  # ms, BirdNET processing


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def app_url(tmp_path_factory):
    port, cache = free_port(), tmp_path_factory.mktemp("cache")
    env = dict(os.environ, BRUTING_CACHE=str(cache))
    proc = subprocess.Popen([sys.executable, "-m", "shiny", "run", "app.py", "--port", str(port)],
                            cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.2)
    yield f"http://127.0.0.1:{port}"
    proc.terminate()
    proc.wait(10)


@pytest.fixture(scope="session")
def audio(tmp_path_factory):
    missing = [f for f in TEST_FILES + [EMPTY_FILE] if not (AUDIO_DIR / f).exists()]
    if missing:
        pytest.skip(f"test audio not found in {AUDIO_DIR}")
    d = tmp_path_factory.mktemp("audio")
    for f in TEST_FILES + [EMPTY_FILE]:
        shutil.copy(AUDIO_DIR / f, d / f)
    return [d / f for f in TEST_FILES], d / EMPTY_FILE


def open_app(page: Page, url: str):
    page.goto(url)
    expect(page.get_by_role("heading", name="Your data is temporary")).to_be_visible()
    page.get_by_role("button", name="I understand").click()
    expect(page.locator(".modal")).to_have_count(0)


def go(page: Page, step: str):
    page.locator(f".bc-step[data-goto={step}]").click()


def upload(page: Page, files):
    go(page, "upload")
    with page.expect_file_chooser() as fc:
        page.get_by_text("Choose files…").click()
    chooser = fc.value
    assert chooser.is_multiple(), "file picker must allow selecting several recordings"
    chooser.set_files(files)
    expect(page.locator("#upload_progress")).to_contain_text("Upload complete", timeout=60_000)


# --------------------------------------------------------------------------- tests
def test_first_visit_warning_and_workflow_bar(page: Page, app_url):
    open_app(page, app_url)
    expect(page.locator(".bc-warn")).to_contain_text("Download the PDF report")
    steps = page.locator(".bc-step")
    expect(steps).to_have_count(5)
    expect(steps.nth(0)).to_have_class("bc-step active")
    for name in ("Upload", "Process", "Verify", "Results", "Report"):
        expect(page.locator(".bc-steps")).to_contain_text(name)


def test_upload_button_opens_file_picker(page: Page, app_url, audio):
    files, empty = audio
    open_app(page, app_url)
    go(page, "upload")
    btn = page.get_by_text("Choose files…")
    expect(btn).to_be_visible()
    expect(btn).to_be_enabled()
    upload(page, files + [empty])
    expect(page.locator("#files_note")).to_contain_text("3 files · 2 usable")
    expect(page.locator("#files_note")).to_contain_text("cannot be analysed")
    expect(page.locator(".bc-step").nth(0)).to_contain_text("2 usable file(s)")


def test_step_bar_navigates(page: Page, app_url):
    open_app(page, app_url)
    page.locator(".bc-step[data-goto=process]").click()
    expect(page.locator("#run")).to_be_visible()
    page.locator(".bc-step[data-goto=report]").click()
    expect(page.locator("#report_panel")).to_contain_text("Download before you leave")


def test_run_without_files_is_blocked(page: Page, app_url):
    open_app(page, app_url)
    page.locator(".bc-step[data-goto=process]").click()
    page.locator("#run").click()
    expect(page.locator(".shiny-notification")).to_contain_text("Upload at least one usable recording")


def test_full_workflow(page: Page, app_url, audio):
    files, _ = audio
    open_app(page, app_url)
    upload(page, files)

    page.locator(".bc-step[data-goto=process]").click()
    page.locator("#run").click()
    expect(page.locator("#job_status")).to_contain_text("DONE", timeout=LONG)
    expect(page.locator(".bc-step[data-goto=process]")).to_contain_text("detections")

    # verify: listen + confirm / reject / relabel
    page.wait_for_timeout(2500)  # results are polled every 1 s; let the final detections load before verifying
    page.locator(".bc-step[data-goto=verify]").click()
    cards = page.locator(".bc-card")
    first_id = cards.first.get_attribute("id")
    expect(cards.first).to_be_visible()
    assert cards.first.locator("audio").get_attribute("src").endswith(".wav")
    img_ok = cards.first.locator("img").evaluate("i => i.complete && i.naturalWidth > 0 || new Promise(r => i.onload = () => r(i.naturalWidth > 0))")
    assert img_ok, "spectrogram image did not load"
    cards.nth(0).get_by_role("button", name="✓ Confirm").click()
    expect(cards.nth(0)).to_have_class("bc-card st-confirm")
    cards.nth(1).get_by_role("button", name="✗ Reject").click()
    expect(cards.nth(1)).to_have_class("bc-card st-reject")
    cards.nth(2).locator("input").fill("Rainbow Lorikeet (Trichoglossus moluccanus)")
    cards.nth(2).get_by_role("button", name="Relabel").click()
    expect(cards.nth(2)).to_contain_text("Relabelled → Rainbow Lorikeet")
    expect(page.locator("#rev_progress")).to_contain_text("3 of")
    # verifying must not redraw the grid (that would stop audio playback)
    assert cards.first.get_attribute("id") == first_id

    # results + threshold
    page.locator(".bc-step[data-goto=results]").click()
    expect(page.locator("#kpis")).to_contain_text("species")
    expect(page.locator("#p_species img")).to_be_visible()
    before = page.locator("#kpis .bc-kpi").nth(2).locator(".v").inner_text()
    page.evaluate("Shiny.setInputValue('thr', 0.1)")
    expect(page.locator("#kpis .bc-kpi").nth(2).locator(".v")).not_to_have_text(before)

    # report downloads
    page.locator(".bc-step[data-goto=report]").click()
    with page.expect_download(timeout=LONG) as dl:
        page.locator("#dl_pdf2").click()
    assert dl.value.suggested_filename.endswith(".pdf")
    assert os.path.getsize(dl.value.path()) > 50_000
    expect(page.locator(".bc-step[data-goto=report]")).to_contain_text("downloaded")
    with page.expect_download() as dl2:
        page.locator("#dl_zip2").click()
    assert dl2.value.suggested_filename.endswith(".zip")
    import zipfile
    names = zipfile.ZipFile(dl2.value.path()).namelist()
    for f in ("detections_all.csv", "species_summary.csv", "call_events.csv", "daily_counts.csv",
              "data_dictionary.csv", "files_manifest.csv", "session_info.txt", "settings.json", "CITATION.txt",
              "sites.csv", "darwin_core/occurrence.csv", "darwin_core/meta.xml", "darwin_core/eml.xml"):
        assert f in names, f"{f} missing from CSV bundle"
    assert "site_profile.json" in names and "reliability.csv" in names

    # site profile: the 3 checks above are saved, and reused in a brand-new session
    with page.expect_download() as dl3:
        page.locator("#dl_profile2").click()
    prof = json.loads(Path(dl3.value.path()).read_text())
    assert prof["profile_version"] == 1 and len(prof["verifications"]) == 3
    page2 = page.context.new_page()
    open_app(page2, app_url)
    go(page2, "upload")
    with page2.expect_file_chooser() as fc:
        page2.get_by_text("Load profile…").click()
    fc.value.set_files(dl3.value.path())
    expect(page2.locator("#profile_status")).to_contain_text("3 check(s) in use")
    page2.close()


def test_mobile_layout(page: Page, app_url):
    page.set_viewport_size({"width": 390, "height": 844})
    open_app(page, app_url)
    go(page, "upload")
    expect(page.get_by_text("Choose files…")).to_be_in_viewport()
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 1, f"page scrolls horizontally by {overflow}px on a phone"


def test_upload_click_does_not_request_notification_permission(page: Page, app_url, tmp_path):
    """A permission prompt during the click can swallow the file dialog in some browsers."""
    page.add_init_script("""window.__permAsked = 0;
      if (window.Notification) { Object.defineProperty(Notification, 'permission', {get: () => 'default'});
        Notification.requestPermission = () => { window.__permAsked++; return Promise.resolve('default'); }; }""")
    open_app(page, app_url)
    go(page, "upload")
    with page.expect_file_chooser():
        page.get_by_text("Choose files…").click()
    assert page.evaluate("window.__permAsked") == 0
    page.locator(".bc-step[data-goto=process]").click()
    page.locator("#run").click()
    assert page.evaluate("window.__permAsked") == 1


def test_drag_and_drop_upload(page: Page, app_url, tmp_path):
    import numpy as np
    import soundfile as sf
    wav = tmp_path / "SRD00689_20260807_120000.wav"
    sf.write(wav, np.zeros(48000 * 3, dtype="float32"), 48000)
    open_app(page, app_url)
    go(page, "upload")
    expect(page.locator(".bc-drop")).to_contain_text("Drag & drop recordings here")
    data = list(wav.read_bytes())
    page.evaluate("""([name, bytes]) => {
        const dt = new DataTransfer();
        dt.items.add(new File([new Uint8Array(bytes)], name, {type: 'audio/wav'}));
        dt.items.add(new File(['x'], 'notes.txt', {type: 'text/plain'}));
        window.alert = () => {};
        const zone = document.querySelector('.bc-drop');
        zone.dispatchEvent(new DragEvent('dragover', {bubbles: true, cancelable: true, dataTransfer: dt}));
        zone.dispatchEvent(new DragEvent('drop', {bubbles: true, cancelable: true, dataTransfer: dt}));
    }""", [wav.name, data])
    expect(page.locator("#files_note")).to_contain_text("1 files · 1 usable", timeout=30_000)


def test_about_tab_opens_first_with_standards(page: Page, app_url):
    import re as _re
    open_app(page, app_url)
    expect(page.locator(".nav-tabs .nav-link.active")).to_have_text("About")
    expect(page.locator(".bc-ver")).to_have_text(_re.compile(r"^v\d+\.\d+\.\d+$"))
    about = page.locator(".tab-pane[data-value=about]")
    for heading in ("What this app does", "What you need", "Limits and runtime", "Data provenance",
                    "How to cite", "Licence", "Reproducibility"):
        expect(about).to_contain_text(heading)
    expect(about.locator(".bc-dict tbody tr")).not_to_have_count(0)
    expect(about).to_contain_text("Abhimanyu Raj Singh")
    with page.expect_download() as dl:
        page.locator("#dl_session").click()
    text = Path(dl.value.path()).read_text()
    assert text.startswith("App: Bruting Calls"), text[:80]
    for needle in ("birdnetlib", "Python:", "BirdNET_GLOBAL_6K_V2.4_Model", "This session"):
        assert needle in text, needle
    page.get_by_role("button", name="Upload my recordings ›").click()
    expect(page.get_by_text("Choose files…")).to_be_visible()


def test_demo_data_checklist_and_next_buttons(page: Page, app_url):
    open_app(page, app_url)
    expect(page.locator(".bc-hero img")).to_be_visible()
    expect(page.locator(".bc-call .wave i")).not_to_have_count(0)
    page.locator("#demo").click()
    expect(page.locator("#files_note")).to_contain_text("3 files · 3 usable", timeout=30_000)
    page.locator(".tab-pane.active .bc-next a.fwd").click()            # Next: Process
    expect(page.locator("#run")).to_be_visible()
    page.locator("#run").click()
    expect(page.locator("#job_check li.now")).to_be_visible(timeout=15_000)  # live checklist while running
    expect(page.locator("#job_check")).to_contain_text("Done — you've been notified", timeout=LONG)
    expect(page.locator("#job_check li.done")).to_have_count(6)
    page.locator(".tab-pane.active .bc-next a.fwd").click()            # Next: Verify
    expect(page.locator(".bc-card").first).to_be_visible()


def test_end_session_deletes_and_hides_stop_by_default(page: Page, app_url):
    open_app(page, app_url)
    page.locator("#end_session").click()
    expect(page.locator(".modal-title")).to_have_text("End this session?")
    expect(page.locator("#stop_app")).to_have_count(0)                # only with BRUTING_ALLOW_STOP=1
    page.locator("#end_confirm").click()
    expect(page.locator(".bc-bye")).to_contain_text("Session ended")


def test_results_is_last_step_and_report_has_session_info(page: Page, app_url):
    open_app(page, app_url)
    go(page, "results")
    footer = page.locator(".tab-pane[data-value=results] > .bc-subtabs .bc-next, .tab-pane[data-value=results] .bc-next").last
    expect(footer.locator("a.back")).to_have_text("‹ Back: Verify")
    expect(footer.locator("a.fwd")).to_have_count(0)          # nothing comes after Results
    go(page, "process")
    for txt in ("BirdNET ignores anything scored below", "Higher finds fainter", "3-second windows"):
        expect(page.locator(".tab-pane[data-value=process]")).to_contain_text(txt)
    go(page, "report")
    with page.expect_download() as dl:
        page.locator("#dl_session2").click()
    assert Path(dl.value.path()).read_text().startswith("App: Bruting Calls")
