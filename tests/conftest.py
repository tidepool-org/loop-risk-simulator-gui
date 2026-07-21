import os
import sys

import tidepool_data_science_simulator

# test_streamlit_app.py imports `severity_model` as a bare name, resolved via
# a relative sys.path.insert(0, "post_processing") that only works when
# pytest is invoked from a repo root that has a post_processing/ dir --
# true in data-science-simulator (this test's original home), not here.
# post_processing/ stays in the simulator repo, so derive its absolute path
# from the editable-installed package instead (same approach the simulator's
# own tests/conftest.py uses, just rooted at the installed package).
_SIMULATOR_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(tidepool_data_science_simulator.__file__)))
_POST_PROCESSING_DIR = os.path.join(_SIMULATOR_ROOT, "post_processing")
if _POST_PROCESSING_DIR not in sys.path:
    sys.path.insert(0, _POST_PROCESSING_DIR)
