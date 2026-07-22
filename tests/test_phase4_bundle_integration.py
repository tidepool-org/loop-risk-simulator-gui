"""
Phase 4 bundle-boundary integration test (Group A of the approved plan).

Validates the FEATURE at the full-system boundary: a bundle built by
packaging/build_bundle.py, installed into a fresh arm64 conda env with NO
sibling data-science-simulator checkout, actually resolves the PINNED simulator
and launches. This is the test the code request names ("a bundle built by the
new tooling actually resolves the pinned simulator and launches").

It is heavy (needs a real built bundle + conda env) so it is OPT-IN: skipped
unless LOOP_RISK_GUI_BUNDLE_DIR points at an extracted bundle. Run it with the
BUNDLE ENV's own interpreter from a neutral cwd (not the simulator checkout,
which would shadow the pinned install):

    export LOOP_RISK_GUI_BUNDLE_DIR=/path/to/extracted/bundle
    cd /tmp && ~/miniconda3/envs/<bundle-env>/bin/python -m pytest \\
        $LOOP_RISK_GUI_BUNDLE_DIR/../<gui-repo>/tests/test_phase4_bundle_integration.py

The place_vendored_paths fixture symlinks the vendored orphan paths beside the
installed package exactly as the launcher does, so resolution is native (no env
seams). No mocks in the exercised path.
"""

import json
import os
import sysconfig

import pytest

BUNDLE_DIR = os.environ.get("LOOP_RISK_GUI_BUNDLE_DIR")

pytestmark = pytest.mark.skipif(
    not BUNDLE_DIR,
    reason="Set LOOP_RISK_GUI_BUNDLE_DIR (extracted bundle) to run the Phase 4 bundle-boundary test.",
)

EXPECTED_SIMULATOR_REF = "gui-bundle-v0.1.0"
REAL_COLLECTION = "test"
REAL_DIR_NAME = "TLR-QAE-482-test"


@pytest.fixture(scope="module", autouse=True)
def place_vendored_paths():
    """Replicate what the bundle launcher does: symlink the vendored orphan paths
    beside the installed package so native resolution works. severity_model.py is
    placed top-level (gui_runner does `import severity_model`); scenario_configs is
    placed beside the package (ScenarioParserV2 resolves `reusable.*` from a path
    hardcoded relative to its own module, not from any env var). Torn down after."""
    site_packages = sysconfig.get_paths()["purelib"]
    links = {
        os.path.join(site_packages, "scenario_configs"):
            os.path.join(BUNDLE_DIR, "vendor", "sim", "scenario_configs"),
        os.path.join(site_packages, "severity_model.py"):
            os.path.join(BUNDLE_DIR, "vendor", "sim", "post_processing", "severity_model.py"),
    }
    created = []
    for link, target in links.items():
        if os.path.islink(link) or os.path.exists(link):
            os.remove(link) if os.path.islink(link) else None
        if not os.path.exists(link):
            os.symlink(target, link)
            created.append(link)
    yield
    for link in created:
        if os.path.islink(link):
            os.remove(link)


def test_simulator_is_a_pinned_non_editable_install():
    """The whole point: the simulator resolves from the pinned install, not a
    sibling editable checkout."""
    import tidepool_data_science_simulator as sim

    pkg_path = os.path.abspath(sim.__file__)
    assert "site-packages" in pkg_path, (
        f"Simulator import resolved to {pkg_path}, not a site-packages install -- "
        "the env is not using the pinned (non-editable) dependency."
    )


def test_severity_model_and_gui_runner_import_under_pinned_install():
    """post_processing/severity_model.py is NOT part of the installed package;
    it must resolve from the vendored copy (via the PYTHONPATH/seam the launcher
    wires). gui_runner imports it at module load, so a clean import proves it."""
    import severity_model

    assert hasattr(severity_model, "build_assessment")
    from tidepool_data_science_simulator.projects.risk.gui_runner import (  # noqa: F401
        run_risk_assessment,
        validate_config_dir,
    )


def test_scenario_configs_resolve_beside_the_installed_package():
    """scenario_configs/ is vendored and placed beside the package, so the app's
    LIBRARY_ROOT (PROJECT_ROOT_DIR-relative) and the parser's hardcoded pointer
    dir both resolve to it natively -- no env var involved."""
    from tidepool_data_science_simulator.utils import PROJECT_ROOT_DIR

    scenario_root = os.path.join(PROJECT_ROOT_DIR, "scenario_configs")
    library_root = os.path.join(scenario_root, "tidepool_risk_v2", "loop_risk_v2_0")
    assert os.path.isdir(os.path.join(library_root, REAL_COLLECTION, REAL_DIR_NAME))
    # reusable/ must resolve at the tidepool_risk_v2 level or `reusable.*` pointers break.
    assert os.path.isdir(os.path.join(scenario_root, "tidepool_risk_v2", "reusable"))


def test_version_stamp_matches_the_built_tag():
    with open(os.path.join(BUNDLE_DIR, "BUNDLE_VERSION.json")) as fh:
        stamp = json.load(fh)
    assert stamp["simulator_ref"] == EXPECTED_SIMULATOR_REF
    assert stamp["bundle_version"]
    assert len(stamp["simulator_sha"]) == 40  # a real resolved SHA


def test_real_run_completes_end_to_end_through_the_launched_app():
    """Drive the bundled streamlit_app.py against the vendored real config and
    confirm a populated assessment + a non-blank PNG -- the app launches and a
    real assessment flows through to the GUI under the pinned install."""
    # Single-directory scope avoids the "test" collection's known-broken TLR-000
    # config (flagged separately); TLR-QAE-482-test is known-good.
    os.environ.setdefault("LOOP_RISK_GUI_ALLOWED_COLLECTIONS", REAL_COLLECTION)

    from streamlit.testing.v1 import AppTest

    app_path = os.path.join(BUNDLE_DIR, "streamlit_app.py")
    at = AppTest.from_file(app_path, default_timeout=60)
    at.run()

    at.selectbox[0].select(REAL_COLLECTION).run()
    at.radio[0].set_value("One specific directory").run()
    tlr_selectbox = [sb for sb in at.selectbox if sb.label == "TLR-* directory"][0]
    tlr_selectbox.select(REAL_DIR_NAME).run()

    assert not at.button[0].disabled
    at.button[0].click().run()
    thread = at.session_state["run_thread"]
    assert thread is not None
    thread.join(timeout=180)
    assert not thread.is_alive(), "Run did not complete within 180s"
    at.run()

    assert not at.exception
    result = at.session_state["run_result"]
    assert result is not None and result.cancelled is False
    assessment = result.risk_dir_results[0].assessment
    assert assessment is not None
    assert set(assessment.stages.keys()) == {"pre", "no_loop", "post"}

    # PNGs generated and non-blank (the af787571 regression class).
    png_paths = result.risk_dir_results[0].png_paths
    assert png_paths, "no PNGs produced"
    for p in png_paths:
        assert os.path.getsize(p) > 5000, f"PNG suspiciously small (blank?): {p}"
