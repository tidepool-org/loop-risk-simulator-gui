"""End-to-end integration test for the TRSET-22 Loop-home renderer.

Renders from a **real** :class:`SimulationTrace` built by the actual
``read_trace`` (TRSET-21) on genuine ``<sim_id>.tsv`` output captured from a
``loop_risk_v2_0`` run (the ``TLR-000-swift`` median suite) -- not a mock. The
two committed fixtures are two stages of that run:

  * ``pre-Loop_NoMitigations_t1_median``  -- an active-Loop stage: ``loop_cob``
    is populated.
  * ``pre-noLoop_t1_median``              -- a no-Loop stage: ``loop_cob`` is
    entirely empty, plus a real bolus/carb event.

This exercises the real read -> shape -> draw path with no simulator run and no
``.dylib`` needed at test time (rendering is a pure ``trace`` consumer).
"""
import os

import numpy as np
import pandas as pd
import pytest

from tidepool_data_science_simulator.trace import read_trace

import loop_home_renderer as renderer

_TEST_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_data")
_POPULATED_COB_TSV = os.path.join(_TEST_DATA_DIR, "pre-Loop_NoMitigations_t1_median.tsv")
_EMPTY_COB_TSV = os.path.join(_TEST_DATA_DIR, "pre-noLoop_t1_median.tsv")

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


@pytest.mark.parametrize("tsv_path", [_POPULATED_COB_TSV, _EMPTY_COB_TSV])
def test_renders_nonblank_image_of_expected_dimensions_from_real_trace(tsv_path):
    trace = read_trace(tsv_path)  # real TRSET-21 reader, real run output
    png = renderer.render_loop_home_screen(trace)

    assert png[:8] == _PNG_SIGNATURE
    assert _png_dimensions(png) == _EXPECTED_DIMENSIONS

    # Non-blank: re-render to a figure and confirm the canvas carries content.
    fig = renderer.build_loop_home_figure(trace)
    try:
        fig.canvas.draw()
        pixels = np.asarray(fig.canvas.buffer_rgba())
        assert np.unique(pixels).size > 1, "rendered image is blank"
    finally:
        import matplotlib.pyplot as plt
        plt.close(fig)


def test_empty_loop_cob_stage_still_renders():
    """AC #10: the all-empty-loop_cob stage renders a valid image without error."""
    trace = read_trace(_EMPTY_COB_TSV)
    # Guard the fixture's meaning: this stage really does carry no COB.
    assert not trace.loop_cob.notna().any(), "fixture no longer has an empty loop_cob"

    png = renderer.render_loop_home_screen(trace)
    assert png[:8] == _PNG_SIGNATURE
    assert _png_dimensions(png) == _EXPECTED_DIMENSIONS


def test_populated_cob_fixture_actually_has_cob():
    """Guard the companion fixture's meaning (active-Loop stage has COB)."""
    trace = read_trace(_POPULATED_COB_TSV)
    assert trace.loop_cob.notna().any(), "fixture no longer has a populated loop_cob"


def test_seed_glucose_history_trimmed_to_simulation_start_on_real_trace():
    """The ~12h seed CGM warmup in a real run is dropped; the chart begins at
    simulation t=0 (2019-08-15 12:00 for the TLR-000-swift median suite)."""
    trace = read_trace(_POPULATED_COB_TSV)
    assert len(trace.time) > 20, "fixture should include the seed warmup rows"

    data = renderer._shape_trace(trace)
    assert len(data.time) < len(trace.time), "seed history was not trimmed"
    expected_t0 = pd.Timestamp("2019-08-15 12:00:00")
    assert data.time[0] == expected_t0
    # Glucose is trimmed too: the first shaped BG sample is the t=0 row.
    assert data.bg.index[0] == expected_t0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
