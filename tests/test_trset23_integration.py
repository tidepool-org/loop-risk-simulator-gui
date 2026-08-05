"""End-to-end integration test for the TRSET-23 Loop-home Streamlit wiring.

Per the approved TRSET-23 plan (Feature gate): exercises the whole chain against
a **real simulation run**, no mocks anywhere in the exercised path --

    run_risk_assessment (real sim)
        -> RiskDirRunResult.trace_paths          (the new gui_runner contract)
        -> read_trace                            (TRSET-21, real reader)
        -> render_loop_home_screen               (TRSET-22, real renderer)
        -> streamlit_app's rendered element tree (TRSET-23, via AppTest)

The config directory is the real ``test/TLR-QAE-482-test``, copied into a
test-controlled temp library (the Phase 3 pattern: real layout, symlinked
``reusable/``, pointed at through the app's own env seams, zero writes into the
installed library). It is used because it genuinely defines only two stages --
``pre-Loop_NoMitigations_t1_median`` and ``post-Loop-WithMitigations_t1_median``
-- so the missing ``no_loop`` stage that must render a "No data" placeholder is
real data, not a hole crafted for the test.

The copied config is renamed to carry the library's dominant
``..._<profile>_profile_v1.json`` convention (its own name omits ``_profile``, so
the profile label would otherwise fall back to the raw filename -- that fallback
is covered by a unit test instead). Only the filename changes; the config content
is the real one, untouched.

The simulation is run once for the module -- it is genuinely slow (a real
multi-process sim, ~4x slower per file since simulator TRSET-24).
"""

import os
import shutil

import numpy as np
import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from tidepool_data_science_simulator.utils import PROJECT_ROOT_DIR  # noqa: E402
from tidepool_data_science_simulator.trace import read_trace  # noqa: E402
from tidepool_data_science_simulator.projects.risk.gui_runner import (  # noqa: E402
    run_risk_assessment,
)

import loop_home_renderer as renderer  # noqa: E402
from streamlit_app import _profile_stage_rows  # noqa: E402

REAL_COLLECTION = "test"
REAL_DIR_NAME = "TLR-QAE-482-test"
SOURCE_CONFIG_FILENAME = "Simulation-Configuration-TLR-QAE-482-test_median_v1.json"
# Same content, renamed to the library's dominant profile-bearing convention.
CONFIG_FILENAME = "Simulation-Configuration-TLR-QAE-482-test_median_profile_v1.json"

EXPECTED_PROFILE_LABEL = "Median profile"
EXPECTED_SIM_IDS = {
    "pre-Loop_NoMitigations_t1_median": "pre",
    "post-Loop-WithMitigations_t1_median": "post",
}
DELIBERATELY_ABSENT_STAGE = "no_loop"

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
    """Run a real risk assessment once and yield the RunResult it returns.

    Builds the temp library first (so nothing is written into the installed
    library) and leaves the app's env seams pointed at it for the duration, so
    the AppTest case renders against the same fixture.
    """
    prior_configs_root = os.environ.get("LOOP_RISK_GUI_SCENARIO_CONFIGS_ROOT")
    prior_allowed = os.environ.get("LOOP_RISK_GUI_ALLOWED_COLLECTIONS")

    source_root = _source_scenario_configs_root()
    source_dir = os.path.join(
        source_root, "tidepool_risk_v2", "loop_risk_v2_0", REAL_COLLECTION, REAL_DIR_NAME
    )

    temp_root = str(tmp_path_factory.mktemp("trset23_lib"))
    temp_v2 = os.path.join(temp_root, "tidepool_risk_v2")
    library_root = os.path.join(temp_v2, "loop_risk_v2_0")
    os.makedirs(library_root, exist_ok=True)
    # Symlink reusable/ rather than copy it (18MB): pointer resolution only needs
    # the path to exist at the tidepool_risk_v2 level.
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
def real_run(real_run_result):
    """The single RiskDirRunResult produced by the real run."""
    return real_run_result.risk_dir_results[0]


# ---------------------------------------------------------------------------
# The new gui_runner contract, from a real run
# ---------------------------------------------------------------------------

def test_real_run_returns_a_trace_path_per_completed_sim(real_run):
    assert real_run.risk_dir_name == REAL_DIR_NAME
    assert list(real_run.trace_paths) == [CONFIG_FILENAME], (
        "one scenario config file -> one entry"
    )

    sims = real_run.trace_paths[CONFIG_FILENAME]
    assert set(sims) == set(EXPECTED_SIM_IDS), (
        "trace_paths must carry exactly the sim_ids that ran"
    )
    for sim_id, tsv_path in sims.items():
        assert os.path.basename(tsv_path) == f"{sim_id}.tsv"
        assert os.path.isfile(tsv_path), f"run did not write {tsv_path}"
        # Written into this run's own results dir for this TLR directory.
        assert os.path.basename(os.path.dirname(tsv_path)) == REAL_DIR_NAME


# ---------------------------------------------------------------------------
# read_trace -> render_loop_home_screen on the real returned paths
# ---------------------------------------------------------------------------

def test_every_returned_trace_renders_a_nonblank_loop_home_chart(real_run):
    sims = real_run.trace_paths[CONFIG_FILENAME]
    assert sims, "nothing to render"

    for sim_id, tsv_path in sims.items():
        trace = read_trace(tsv_path)  # real TRSET-21 reader on real run output
        png = renderer.render_loop_home_screen(trace)

        assert png[:8] == _PNG_SIGNATURE, f"{sim_id} did not render a PNG"
        assert _png_dimensions(png) == _EXPECTED_DIMENSIONS, f"{sim_id} wrong dimensions"

        figure = renderer.build_loop_home_figure(trace)
        try:
            figure.canvas.draw()
            pixels = np.asarray(figure.canvas.buffer_rgba())
            assert np.unique(pixels).size > 1, f"{sim_id} rendered a blank image"
        finally:
            import matplotlib.pyplot as plt

            plt.close(figure)


# ---------------------------------------------------------------------------
# The app's (profile -> stage) grouping over real run output
# ---------------------------------------------------------------------------

def test_grouping_yields_one_profile_row_with_the_absent_stage_missing(real_run):
    rows = _profile_stage_rows(real_run.trace_paths, real_run.risk_dir_name)

    assert len(rows) == 1, "one VP profile in this directory -> one row"
    label, stage_paths = rows[0]
    assert label == EXPECTED_PROFILE_LABEL
    assert set(stage_paths) == set(EXPECTED_SIM_IDS.values())
    # Guard the fixture's meaning: this directory really does define no no_loop
    # stage, which is what makes the "No data" assertion below a real case.
    assert DELIBERATELY_ABSENT_STAGE not in stage_paths


# ---------------------------------------------------------------------------
# The rendered app, driven by the real run's result
# ---------------------------------------------------------------------------

def test_app_renders_present_stages_as_charts_and_the_absent_stage_as_no_data(
    real_run_result,
):
    at = AppTest.from_file("streamlit_app.py", default_timeout=180)
    # The real RunResult the runner returned, handed to the app exactly as the
    # background run thread hands it over.
    at.session_state["run_result"] = real_run_result
    at.run()

    assert not at.exception

    # One chart per present stage, from real run output.
    assert len(at.image) == len(EXPECTED_SIM_IDS)
    # The deliberately-absent stage renders an explicit placeholder, not a blank.
    assert [info.value for info in at.info] == ["No data"]

    markdown_values = [m.value for m in at.markdown]
    assert f"**{EXPECTED_PROFILE_LABEL}**" in markdown_values
    # TWI-0006 2.g.ii: all three stages presented co-equally, in severity_model's
    # order, with nothing hidden behind a selector.
    assert [
        v for v in markdown_values
        if v in ("*Pre-mitigation*", "*No Loop*", "*Post-mitigation*")
    ] == ["*Pre-mitigation*", "*No Loop*", "*Post-mitigation*"]
    assert not [sb for sb in at.selectbox if "stage" in sb.label.lower()]

    # The stage metrics table above the charts is unaffected by this change.
    assert len(at.dataframe) >= 1
    assert {"Pre-mitigation", "No Loop", "Post-mitigation"} <= set(
        at.dataframe[0].value["Stage"]
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
