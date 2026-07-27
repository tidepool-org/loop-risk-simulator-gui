"""
Phase 5 launcher-boundary integration test.

Validates the FEATURE end to end: the colleague-facing launcher's provisioning
helper (packaging/launcher.py `provision()`), driven against a real bundle built
by build_bundle.py, actually stands up a working install and a real risk
assessment flows through it. This is the Phase 5 analogue of the Phase 4 Group-A
bundle-boundary test -- but here the LAUNCHER itself discovers the arm64 conda,
builds the Swift .dylib, creates the env, copies the vendored orphan paths into
the durable per-user location, and places the load-bearing symlinks, instead of
a test fixture doing it.

The real run is driven through gui_runner.run_risk_assessment -- the sanctioned
in-process entry point that streamlit_app.py wraps -- rather than through the
Streamlit UI. That deliberately isolates what THIS test is responsible for (does
the launcher's provisioning make the runtime resolve and compute a real
assessment: the severity_model import, the scenario_configs/reusable symlink
seam, and a freshly built, symbol-complete .dylib) from the GUI view layer's
own concerns (background-thread progress/cancel, session_state), which are
covered by test_streamlit_app.py / test_phase3_integration.py.

Heavy (creates a real conda env from the pinned spec + builds the .dylib on
first run -- several minutes, network) and so OPT-IN: skipped unless
LOOP_RISK_GUI_BUNDLE_DIR points at an EXTRACTED bundle. Provisioning writes to a
pytest temp root, so nothing touches the real ~/Library/Application Support.

    python packaging/build_bundle.py build --version 0.1.0 \\
        --simulator-ref main --output-dir dist/
    mkdir -p /tmp/lrsg && tar -xzf dist/loop-risk-simulator-gui-0.1.0.tar.gz -C /tmp/lrsg
    export LOOP_RISK_GUI_BUNDLE_DIR=/tmp/lrsg
    cd /tmp && ~/miniconda3/envs/<any-arm64>/bin/python -m pytest \\
        <gui-repo>/tests/test_phase5_launcher_integration.py -s
"""

import json
import os
import subprocess
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "packaging"))
import launcher  # noqa: E402

BUNDLE_DIR = os.environ.get("LOOP_RISK_GUI_BUNDLE_DIR")

pytestmark = pytest.mark.skipif(
    not BUNDLE_DIR,
    reason="Set LOOP_RISK_GUI_BUNDLE_DIR (extracted bundle) to run the Phase 5 launcher-boundary test.",
)

REAL_COLLECTION = "test"
REAL_DIR_NAME = "TLR-QAE-482-test"

# Runs INSIDE the provisioned env (the outer pytest process is a different
# interpreter). Drives the sanctioned in-process entry point against the real
# vendored config and emits the assessment shape + PNG sizes as JSON. Importing
# gui_runner also imports `severity_model` (top-level), exercising that symlink.
_DRIVER = textwrap.dedent(
    """
    import json, os
    from tidepool_data_science_simulator.utils import PROJECT_ROOT_DIR
    from tidepool_data_science_simulator.projects.risk.gui_runner import (
        run_risk_assessment, validate_config_dir,
    )
    lib = os.path.join(PROJECT_ROOT_DIR, "scenario_configs", "tidepool_risk_v2", "loop_risk_v2_0")
    config_dir = os.path.join(lib, "{collection}")
    v = validate_config_dir(config_dir, "{tlr}")
    res = run_risk_assessment(config_dir, target_risk_dir="{tlr}")
    rd = res.risk_dir_results[0]
    out = {{
        "cancelled": res.cancelled,
        "valid": v.is_valid,
        "stages": sorted(rd.assessment.stages.keys()) if rd.assessment else None,
        "png_sizes": [os.path.getsize(p) for p in rd.png_paths],
    }}
    print("RESULT_JSON:" + json.dumps(out))
    """
)


@pytest.fixture(scope="module")
def provisioned(tmp_path_factory):
    """Provision once (the heavy step) and share it across the tests in this module."""
    root = str(tmp_path_factory.mktemp("app-support-root"))
    env_python = launcher.provision(BUNDLE_DIR, root=root)
    return {"root": root, "env_python": env_python}


def test_launcher_provisions_and_a_real_run_flows_through(provisioned, tmp_path):
    """Full boundary: provision() built the env + .dylib + wired the seam; a real
    TLR-QAE-482-test assessment then computes to completion in that env."""
    root, env_python = provisioned["root"], provisioned["env_python"]

    # Placement checks -- exactly what the launcher (not a fixture) produced.
    assert os.path.isfile(env_python), "provision() did not yield an env python"
    state = launcher.read_state(root)
    assert state and state["complete"] is True
    vendor_sim = state["vendor_sim_root"]
    assert os.path.isdir(os.path.join(vendor_sim, "scenario_configs", "tidepool_risk_v2", "reusable")), \
        "reusable/ must be vendored beside scenario_configs or `reusable.*` pointers break"
    site_packages = launcher.site_packages_of(state["env_prefix"])
    for link in launcher.plan_symlinks(site_packages, vendor_sim):
        assert os.path.islink(link) and os.path.exists(link), f"missing/broken symlink: {link}"

    # A real assessment flows through the sanctioned entry point in the env.
    driver = _DRIVER.format(collection=REAL_COLLECTION, tlr=REAL_DIR_NAME)
    proc = subprocess.run(
        [env_python, "-c", driver],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, f"driver failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    payload = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT_JSON:")]
    assert payload, f"driver produced no result:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    result = json.loads(payload[0][len("RESULT_JSON:"):])

    assert result["valid"] is True
    assert result["cancelled"] is False
    assert result["stages"] == ["no_loop", "post", "pre"]  # populated SeverityAssessment
    assert result["png_sizes"] and all(sz > 5000 for sz in result["png_sizes"]), \
        f"blank/absent PNG(s): {result['png_sizes']}"


def test_second_provision_reuses_without_rebuilding(provisioned):
    """Version-aware reuse: a second provision() against the same root + bundle
    returns the same env immediately (SHAs match, artifacts intact) and does not
    rewrite the state marker -- no env rebuild."""
    root = provisioned["root"]
    state_file = os.path.join(root, launcher.STATE_FILENAME)
    mtime = os.path.getmtime(state_file)

    second = launcher.provision(BUNDLE_DIR, root=root)

    assert second == provisioned["env_python"]
    assert os.path.getmtime(state_file) == mtime  # reuse path did not rewrite state
