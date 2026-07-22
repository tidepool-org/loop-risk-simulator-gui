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
