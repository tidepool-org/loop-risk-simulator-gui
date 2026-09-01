import os
import sys

import tidepool_data_science_simulator

# The GUI's tests (and gui_runner.py itself) import `severity_model` as a bare
# name. severity_model.py lives in the simulator's top-level post_processing/
# dir, which is NOT part of the installed package -- so it must be put on
# sys.path explicitly. Two install models to support:
#
#   * Editable / sibling checkout (dev): post_processing/ sits beside the
#     installed package's parent -- derive it from the package __file__, same
#     approach the simulator's own tests/conftest.py uses.
#   * Pinned, non-editable bundle (Phase 4): the simulator lives in
#     site-packages with no post_processing/ beside it; the bundle vendors
#     post_processing/ separately and points LOOP_RISK_GUI_POST_PROCESSING_DIR
#     at it (the launcher also puts it on PYTHONPATH, so the import may already
#     resolve -- this env seam just makes the tests self-sufficient).
#
# Prefer the explicit env seam; fall back to the editable derivation. Only add
# a path that actually exists, so a stale/wrong value fails loudly at import
# rather than silently masking the real location.
_env_pp = os.environ.get("LOOP_RISK_GUI_POST_PROCESSING_DIR")
_SIMULATOR_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(tidepool_data_science_simulator.__file__)))
_derived_pp = os.path.join(_SIMULATOR_ROOT, "post_processing")
_POST_PROCESSING_DIR = _env_pp if _env_pp else _derived_pp
if os.path.isdir(_POST_PROCESSING_DIR) and _POST_PROCESSING_DIR not in sys.path:
    sys.path.insert(0, _POST_PROCESSING_DIR)


# Project root (holds streamlit_app.py), so the factory below can import the app
# module and resolve its path without depending on the working directory.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

APP_PATH = os.path.join(_PROJECT_ROOT, "streamlit_app.py")


def make_app_test(path=None, default_timeout=30):
    """Build an ``AppTest`` for the app with the TRSET-34 start page already
    acknowledged, so the harness lands on the tool the way it did before the
    start page existed.

    Every existing suite goes through here rather than repeating the flag: the
    gate is bypassed in exactly one place, and the day it moves (the multipage
    migration) there is one line to change. TRSET-34's own integration test
    deliberately does NOT use this -- it drives the real, unacknowledged first
    render and clicks the real button.

    Imported lazily so this conftest stays importable in an environment without
    streamlit (the streamlit suites already guard themselves with
    ``pytest.importorskip``); the non-streamlit suites must still collect.
    """
    from streamlit.testing.v1 import AppTest
    import streamlit_app

    at = AppTest.from_file(path or APP_PATH, default_timeout=default_timeout)
    at.session_state[streamlit_app.START_PAGE_ACK_KEY] = True
    return at
