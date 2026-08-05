"""End-to-end integration test for the TRSET-7 exportable results (Feature gate).

Per the approved plan: no mocks anywhere in the exercised path --

    run_risk_assessment (real sim, real metadata.json)
        -> create_severity_summary.process_results_directory   (real RTF renderer)
        -> _trace_png -> render_loop_home_screen               (real chart PNGs)
        -> export_bundle.build_export_zip                      (real zip)
        -> streamlit_app's export control, via AppTest

and everything is asserted **at the zip boundary** -- the archive is opened and
read, not inspected through the functions that produced it.

The fixture is the TRSET-23 one, for the same reason: the real
``test/TLR-QAE-482-test`` config directory, copied into a test-controlled temp
library (real layout, symlinked ``reusable/``, the app's own env seams, zero
writes into the installed library). It genuinely defines only two stages --
``pre-Loop_NoMitigations_t1_median`` and ``post-Loop-WithMitigations_t1_median``
-- so "no chart is written for a stage that has no trace" is exercised against
real data rather than a hole crafted for the test.

The simulation runs once for the module -- it is a real multi-process run.
"""

import json
import os
import shutil
import zipfile

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from tidepool_data_science_simulator.utils import PROJECT_ROOT_DIR  # noqa: E402
from tidepool_data_science_simulator.projects.risk.gui_runner import (  # noqa: E402
    METADATA_FILENAME,
    run_risk_assessment,
)

import export_bundle  # noqa: E402
import loop_home_renderer as renderer  # noqa: E402
from streamlit_app import _export_chart_files  # noqa: E402

REAL_COLLECTION = "test"
REAL_DIR_NAME = "TLR-QAE-482-test"
SOURCE_CONFIG_FILENAME = "Simulation-Configuration-TLR-QAE-482-test_median_v1.json"
# Same content, renamed to the library's dominant profile-bearing convention
# (see the TRSET-23 integration test -- the raw-name fallback is a unit test).
CONFIG_FILENAME = "Simulation-Configuration-TLR-QAE-482-test_median_profile_v1.json"

EXPECTED_PROFILE_LABEL = "Median profile"
# The two stages this directory really defines, and the one it really lacks.
EXPECTED_STAGE_DISPLAYS = ("Pre-mitigation", "Post-mitigation")
DELIBERATELY_ABSENT_STAGE_DISPLAY = "No Loop"
EXPECTED_SIM_IDS = (
    "pre-Loop_NoMitigations_t1_median",
    "post-Loop-WithMitigations_t1_median",
)

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_EXPECTED_DIMENSIONS = (
    renderer.FIGSIZE_INCHES[0] * renderer.DPI,
    renderer.FIGSIZE_INCHES[1] * renderer.DPI,
)


def _png_dimensions(png_bytes):
    assert png_bytes[:8] == _PNG_SIGNATURE, "not a PNG"
    return (
        int.from_bytes(png_bytes[16:20], "big"),
        int.from_bytes(png_bytes[20:24], "big"),
    )


def _source_scenario_configs_root():
    """The real scenario_configs/ in effect -- the vendored bundle copy (via the
    env seam) if set, else the editable-install checkout."""
    override = os.environ.get("LOOP_RISK_GUI_SCENARIO_CONFIGS_ROOT")
    return override if override else os.path.join(PROJECT_ROOT_DIR, "scenario_configs")


@pytest.fixture(scope="module")
def real_run_result(tmp_path_factory):
    """Run a real risk assessment once and yield the RunResult it returns."""
    prior_configs_root = os.environ.get("LOOP_RISK_GUI_SCENARIO_CONFIGS_ROOT")
    prior_allowed = os.environ.get("LOOP_RISK_GUI_ALLOWED_COLLECTIONS")

    source_root = _source_scenario_configs_root()
    source_dir = os.path.join(
        source_root, "tidepool_risk_v2", "loop_risk_v2_0", REAL_COLLECTION, REAL_DIR_NAME
    )

    temp_root = str(tmp_path_factory.mktemp("trset7_lib"))
    temp_v2 = os.path.join(temp_root, "tidepool_risk_v2")
    library_root = os.path.join(temp_v2, "loop_risk_v2_0")
    os.makedirs(library_root, exist_ok=True)
    # Symlink reusable/ rather than copy it (18MB).
    os.symlink(
        os.path.join(source_root, "tidepool_risk_v2", "reusable"),
        os.path.join(temp_v2, "reusable"),
    )
    risk_dir = os.path.join(library_root, REAL_COLLECTION, REAL_DIR_NAME)
    os.makedirs(risk_dir, exist_ok=True)
    shutil.copyfile(
        os.path.join(source_dir, SOURCE_CONFIG_FILENAME),
        os.path.join(risk_dir, CONFIG_FILENAME),
    )

    os.environ["LOOP_RISK_GUI_SCENARIO_CONFIGS_ROOT"] = temp_root
    os.environ["LOOP_RISK_GUI_ALLOWED_COLLECTIONS"] = REAL_COLLECTION

    config_dir = os.path.join(library_root, REAL_COLLECTION)
    run_result = run_risk_assessment(config_dir, target_risk_dir=REAL_DIR_NAME)

    assert len(run_result.risk_dir_results) == 1, "expected exactly one TLR directory"
    yield run_result

    for var, prior in (
        ("LOOP_RISK_GUI_SCENARIO_CONFIGS_ROOT", prior_configs_root),
        ("LOOP_RISK_GUI_ALLOWED_COLLECTIONS", prior_allowed),
    ):
        if prior is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = prior


@pytest.fixture(scope="module")
def exported_zip(real_run_result, tmp_path_factory):
    """The real export of the real run: ``(zip_path, skipped_reasons)``.

    Built exactly the way the app builds it -- the app's own chart enumeration
    feeding export_bundle -- so what is asserted below is the archive a user
    downloads.
    """
    charts = []
    skipped = []
    for risk_dir_result in real_run_result.risk_dir_results:
        dir_charts, dir_skipped = _export_chart_files(risk_dir_result)
        charts.extend(dir_charts)
        skipped.extend(dir_skipped)

    dest_dir = str(tmp_path_factory.mktemp("trset7_export"))
    zip_path = export_bundle.build_export_zip(real_run_result.save_dir, charts, dest_dir)
    return zip_path, skipped


@pytest.fixture(scope="module")
def archived_names(exported_zip):
    zip_path, _ = exported_zip
    with zipfile.ZipFile(zip_path) as archive:
        return archive.namelist()


@pytest.fixture(scope="module")
def export_root(real_run_result):
    return export_bundle.export_root_name(real_run_result.save_dir)


# ---------------------------------------------------------------------------
# metadata.json -- written by the real run, read by the real RTF renderer
# ---------------------------------------------------------------------------

def test_run_wrote_metadata_json_and_the_export_carries_it(
    real_run_result, exported_zip, export_root
):
    on_disk = os.path.join(real_run_result.save_dir, METADATA_FILENAME)
    assert os.path.isfile(on_disk), "the real run did not write metadata.json"

    zip_path, _ = exported_zip
    with zipfile.ZipFile(zip_path) as archive:
        metadata = json.loads(archive.read(f"{export_root}/{METADATA_FILENAME}"))

    assessment = real_run_result.risk_dir_results[0].assessment
    # The timestamp the summaries are dated with is the one the GUI displayed.
    assert metadata["timestamp"] == assessment.timestamp


# ---------------------------------------------------------------------------
# The severity-summary RTFs, produced at export time by the real renderer
# ---------------------------------------------------------------------------

def test_export_carries_a_real_rtf_summary_per_tlr_directory(
    real_run_result, exported_zip, export_root, archived_names
):
    assessment = real_run_result.risk_dir_results[0].assessment
    rtf_name = f"{export_root}/{REAL_DIR_NAME}/risk_summary_{assessment.simulation_id}.rtf"
    assert rtf_name in archived_names, archived_names

    zip_path, _ = exported_zip
    with zipfile.ZipFile(zip_path) as archive:
        rtf = archive.read(rtf_name).decode()

    # Real RTF from the unmodified renderer: its own header, this directory's
    # name, the run's timestamp and the results table it always emits.
    assert rtf.startswith(r"{\rtf1")
    assert f"Risk severity summary for {assessment.subdirectory_name}" in rtf
    assert assessment.timestamp.replace("T", " ").split(".")[0] in rtf
    assert "Table of results" in rtf
    for stage_row in ("Pre-mitigation", "No Loop", "Post-mitigation"):
        assert stage_row in rtf


def test_the_exported_rtf_is_byte_identical_to_the_one_left_on_disk(
    real_run_result, exported_zip, export_root
):
    """The archive is not a re-render: it ships the very bytes the renderer wrote
    into the results directory, which is the same directory (and same function)
    the CLI operates on."""
    assessment = real_run_result.risk_dir_results[0].assessment
    rtf_filename = f"risk_summary_{assessment.simulation_id}.rtf"
    on_disk_path = os.path.join(real_run_result.save_dir, REAL_DIR_NAME, rtf_filename)
    with open(on_disk_path, "rb") as rtf_file:
        on_disk_bytes = rtf_file.read()

    zip_path, _ = exported_zip
    with zipfile.ZipFile(zip_path) as archive:
        archived = archive.read(f"{export_root}/{REAL_DIR_NAME}/{rtf_filename}")

    assert archived == on_disk_bytes


# ---------------------------------------------------------------------------
# The charts/ folder
# ---------------------------------------------------------------------------

def test_charts_folder_holds_one_named_png_per_stage_that_produced_a_trace(
    exported_zip, export_root, archived_names
):
    chart_names = sorted(
        name for name in archived_names
        if name.startswith(f"{export_root}/{export_bundle.CHARTS_DIR_NAME}/")
    )

    expected = sorted(
        f"{export_root}/{export_bundle.CHARTS_DIR_NAME}/"
        f"{export_bundle.chart_filename(REAL_DIR_NAME, EXPECTED_PROFILE_LABEL, stage_display)}"
        for stage_display in EXPECTED_STAGE_DISPLAYS
    )
    assert chart_names == expected
    # Filesystem-safe: the profile label's space and nothing else survives as "_".
    assert all(" " not in name and ":" not in name for name in chart_names)


def test_no_chart_is_fabricated_for_the_stage_this_directory_lacks(
    exported_zip, export_root, archived_names
):
    """This config really defines no no_loop stage, so the export must simply not
    contain that chart -- and must say so rather than shipping a blank."""
    absent = export_bundle.chart_filename(
        REAL_DIR_NAME, EXPECTED_PROFILE_LABEL, DELIBERATELY_ABSENT_STAGE_DISPLAY
    )
    assert not [name for name in archived_names if name.endswith(absent)]

    _, skipped = exported_zip
    assert len(skipped) == 1
    assert DELIBERATELY_ABSENT_STAGE_DISPLAY in skipped[0]
    assert "no simulation trace" in skipped[0]


def test_every_exported_chart_is_a_real_loop_home_png(exported_zip, export_root, archived_names):
    zip_path, _ = exported_zip
    chart_names = [
        name for name in archived_names
        if name.startswith(f"{export_root}/{export_bundle.CHARTS_DIR_NAME}/")
    ]
    assert chart_names, "nothing to check"

    with zipfile.ZipFile(zip_path) as archive:
        for name in chart_names:
            png_bytes = archive.read(name)
            assert png_bytes[:8] == _PNG_SIGNATURE, name
            assert _png_dimensions(png_bytes) == _EXPECTED_DIMENSIONS, name


# ---------------------------------------------------------------------------
# The raw run outputs, and nothing else
# ---------------------------------------------------------------------------

def test_export_carries_the_runs_raw_outputs(archived_names, export_root):
    tlr_prefix = f"{export_root}/{REAL_DIR_NAME}/"
    in_tlr_dir = [name[len(tlr_prefix):] for name in archived_names if name.startswith(tlr_prefix)]

    for sim_id in EXPECTED_SIM_IDS:
        assert f"{sim_id}.tsv" in in_tlr_dir, in_tlr_dir
    assert [
        name for name in in_tlr_dir
        if name.startswith("summary_results_Simulation-Configuration-TLR") and name.endswith(".csv")
    ]
    # The three-panel simulator figure the run still writes.
    assert [name for name in in_tlr_dir if name.endswith(".png")]
    # The Loop algorithm I/O captured per step.
    assert [name for name in in_tlr_dir if name.startswith("loop_algo_io/")]


def test_export_contains_nothing_from_outside_the_run_directory(
    real_run_result, archived_names, export_root
):
    """Everything is nested under one top folder, and every entry is either a file
    the run wrote or a chart -- so nothing (e.g. anything under data/PHI/) can be
    swept in from elsewhere."""
    assert all(name.startswith(f"{export_root}/") for name in archived_names), archived_names

    charts_prefix = f"{export_root}/{export_bundle.CHARTS_DIR_NAME}/"
    for name in archived_names:
        if name.startswith(charts_prefix):
            continue
        relative = name[len(export_root) + 1:]
        assert os.path.isfile(os.path.join(real_run_result.save_dir, relative)), name


# ---------------------------------------------------------------------------
# The whole thing through the app the user actually drives
# ---------------------------------------------------------------------------

def test_app_exports_the_real_run_and_offers_a_valid_zip_for_download(real_run_result):
    at = AppTest.from_file("streamlit_app.py", default_timeout=300)
    # The real RunResult the runner returned, handed over exactly as the
    # background run thread hands it to the app.
    at.session_state["run_result"] = real_run_result
    at.run()
    assert not at.exception

    export_buttons = [b for b in at.button if b.label == "Export results"]
    assert len(export_buttons) == 1, "completed run must offer the export control"
    assert not at.download_button, "the zip is built on activation, not speculatively"

    export_buttons[0].click().run()
    assert not at.exception
    assert not at.error, [e.value for e in at.error]

    assert len(at.download_button) == 1
    zip_path = at.session_state["export_zip_path"]
    assert os.path.basename(zip_path).startswith(export_bundle.EXPORT_STEM_PREFIX)
    assert os.path.basename(zip_path).endswith(".zip")

    # The offered archive is the real one, with the same contents asserted above.
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.testzip() is None, "corrupt archive"
        names = archive.namelist()
    root = export_bundle.export_root_name(real_run_result.save_dir)
    assessment = real_run_result.risk_dir_results[0].assessment
    assert f"{root}/{METADATA_FILENAME}" in names
    assert f"{root}/{REAL_DIR_NAME}/risk_summary_{assessment.simulation_id}.rtf" in names
    assert len([n for n in names if n.startswith(f"{root}/{export_bundle.CHARTS_DIR_NAME}/")]) == len(
        EXPECTED_STAGE_DISPLAYS
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
