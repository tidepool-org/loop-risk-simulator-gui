"""Unit tests for the TRSET-22 Loop-home-screen renderer.

Drive the renderer with synthetic, in-memory :class:`SimulationTrace` objects
(no files, no simulator run) to exercise the contract directly: PNG output,
no figure-state leakage across repeated calls, the fixed target band, tolerance
of empty/NaN series, and the shaping/drawing seam. The real-trace end-to-end
path is covered separately in ``test_loop_home_renderer_integration.py``.
"""
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pytest

from tidepool_data_science_simulator.trace import SimulationTrace

import loop_home_renderer as renderer

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _make_trace(n=24, loop_cob_all_nan=False, sim_id="unit-trace"):
    """Build a synthetic SimulationTrace with realistic gaps and a few events.

    Line series carry deliberate NaN gaps; boluses/carbs are sparse events with
    NaN/zero cells around a single nonzero entry, so the shaping step's
    event-extraction is exercised.
    """
    time = pd.DatetimeIndex(pd.date_range("2019-08-15T08:00:00", periods=n, freq="5min"), name="time")

    def series(values, name):
        return pd.Series(values, index=time, name=name)

    bg = series(120 + 20 * np.sin(np.linspace(0, 3, n)), "bg")
    bg_sensor = bg + 5.0
    bg_sensor.iloc[3:6] = np.nan  # sensor gap -> rendered as a gap, not filled

    iob = series(np.linspace(2.0, 0.2, n), "iob")
    sbr = series(np.full(n, 0.75), "sbr")
    temp_basal = series(np.full(n, np.nan), "temp_basal")
    temp_basal.iloc[5:9] = 1.2

    def event_series(index_value_pairs, name):
        s = pd.Series(np.nan, index=time, name=name)
        for i, v in index_value_pairs:
            s.iloc[i] = v
        return s

    true_bolus = event_series([(7, 2.5)], "true_bolus")
    reported_bolus = event_series([(7, 2.5)], "reported_bolus")
    true_carb_value = event_series([(2, 40.0)], "true_carb_value")
    reported_carb_value = event_series([(2, 40.0)], "reported_carb_value")

    loop_cob_values = np.full(n, np.nan) if loop_cob_all_nan else np.clip(
        40 - np.arange(n) * 2.0, 0, None
    )
    loop_cob = series(loop_cob_values, "loop_cob")

    return SimulationTrace(
        sim_id=sim_id,
        time=time,
        bg=bg,
        bg_sensor=bg_sensor,
        iob=iob,
        sbr=sbr,
        temp_basal=temp_basal,
        true_bolus=true_bolus,
        reported_bolus=reported_bolus,
        true_carb_value=true_carb_value,
        reported_carb_value=reported_carb_value,
        loop_cob=loop_cob,
    )


def _png_dimensions(png_bytes):
    """Parse (width, height) from a PNG's IHDR without needing Pillow."""
    assert png_bytes[:8] == _PNG_SIGNATURE, "not a PNG"
    width = int.from_bytes(png_bytes[16:20], "big")
    height = int.from_bytes(png_bytes[20:24], "big")
    return width, height


def test_render_returns_nonempty_png_bytes():
    png = renderer.render_loop_home_screen(_make_trace())
    assert png[:8] == _PNG_SIGNATURE
    assert len(png) > 1000  # a real rendered image, not an empty stub


def test_render_dimensions_are_deterministic():
    png = renderer.render_loop_home_screen(_make_trace())
    expected = (
        renderer.FIGSIZE_INCHES[0] * renderer.DPI,
        renderer.FIGSIZE_INCHES[1] * renderer.DPI,
    )
    assert _png_dimensions(png) == expected


def test_render_writes_output_path_matching_returned_bytes(tmp_path):
    out = tmp_path / "loop_home.png"
    png = renderer.render_loop_home_screen(_make_trace(), output_path=str(out))
    assert out.exists()
    assert out.read_bytes() == png


def test_no_figure_state_leak_across_repeated_calls():
    """AC #1: repeated calls in one process must not leak figures."""
    assert plt.get_fignums() == []
    for _ in range(3):
        renderer.render_loop_home_screen(_make_trace())
        assert plt.get_fignums() == [], "render left an open figure behind"


def test_build_figure_has_three_stacked_sections():
    fig = renderer.build_loop_home_figure(_make_trace())
    try:
        assert len(fig.axes) == 3
    finally:
        plt.close(fig)


def test_glucose_section_shades_the_fixed_target_band():
    """AC #5: a target band fixed at the module constants is drawn on Glucose."""
    fig = renderer.build_loop_home_figure(_make_trace())
    try:
        glucose_ax = fig.axes[0]
        # axhspan is a full-width Rectangle (matplotlib >=3.9); its y-extent is
        # the band. Read bottom/height rather than vertices so this holds across
        # the Rectangle/Polygon return-type change.
        band_spans = [
            (round(patch.get_y(), 6), round(patch.get_y() + patch.get_height(), 6))
            for patch in glucose_ax.patches
        ]
        assert (
            float(renderer.TARGET_RANGE_LOW_MG_DL),
            float(renderer.TARGET_RANGE_HIGH_MG_DL),
        ) in band_spans
    finally:
        plt.close(fig)


def test_empty_loop_cob_renders_without_error():
    """AC #7: an entirely empty loop_cob still produces a valid, non-blank PNG."""
    png = renderer.render_loop_home_screen(_make_trace(loop_cob_all_nan=True))
    assert png[:8] == _PNG_SIGNATURE
    assert len(png) > 1000


def test_render_output_is_not_blank():
    """The rendered canvas has real content (more than one pixel value)."""
    fig = renderer.build_loop_home_figure(_make_trace())
    try:
        fig.canvas.draw()
        pixels = np.asarray(fig.canvas.buffer_rgba())
        assert np.unique(pixels).size > 1, "rendered image is blank"
    finally:
        plt.close(fig)


def test_shaping_attaches_band_bounds_from_constants():
    data = renderer._shape_trace(_make_trace())
    assert data.target_low == renderer.TARGET_RANGE_LOW_MG_DL
    assert data.target_high == renderer.TARGET_RANGE_HIGH_MG_DL


def test_seed_glucose_history_is_trimmed_to_simulation_start():
    """The seed CGM warmup (glucose-only rows) is dropped; every series -- glucose
    included -- starts at simulation t=0."""
    seed_n, sim_n = 40, 12
    n = seed_n + sim_n
    time = pd.DatetimeIndex(
        pd.date_range("2019-08-15T00:00:00", periods=n, freq="5min"), name="time"
    )

    def s(values, name):
        return pd.Series(values, index=time, name=name)

    # Seed rows carry ONLY glucose (flat warmup); the simulation window carries
    # glucose that moves + patient/pump state -- exactly how init() seeds history.
    bg = s(np.concatenate([np.full(seed_n, 110.0), np.linspace(110, 150, sim_n)]), "bg")
    bg_sensor = bg + 1.0

    def state(name, value):
        arr = np.full(n, np.nan)
        arr[seed_n:] = value
        return s(arr, name)

    def all_nan(name):
        return s(np.full(n, np.nan), name)

    trace = SimulationTrace(
        sim_id="seeded",
        time=time,
        bg=bg,
        bg_sensor=bg_sensor,
        iob=state("iob", 1.0),
        sbr=state("sbr", 0.7),
        temp_basal=all_nan("temp_basal"),
        true_bolus=all_nan("true_bolus"),
        reported_bolus=all_nan("reported_bolus"),
        true_carb_value=all_nan("true_carb_value"),
        reported_carb_value=all_nan("reported_carb_value"),
        loop_cob=all_nan("loop_cob"),
    )

    assert renderer._simulation_start_position(trace) == seed_n
    data = renderer._shape_trace(trace)
    assert len(data.time) == sim_n
    assert data.time[0] == time[seed_n]
    # Glucose is trimmed too -- the flat 110 seed prefix is gone.
    assert len(data.bg) == sim_n
    assert data.bg.iloc[0] == bg.iloc[seed_n]


def test_trace_without_simulation_state_is_left_untrimmed():
    """A degenerate all-seed trace (no non-glucose state) renders whole rather
    than collapsing to empty."""
    n = 8
    time = pd.DatetimeIndex(
        pd.date_range("2019-08-15T00:00:00", periods=n, freq="5min"), name="time"
    )

    def s(values, name):
        return pd.Series(values, index=time, name=name)

    def all_nan(name):
        return s(np.full(n, np.nan), name)

    trace = SimulationTrace(
        sim_id="all-seed",
        time=time,
        bg=s(np.full(n, 110.0), "bg"),
        bg_sensor=s(np.full(n, 110.0), "bg_sensor"),
        iob=all_nan("iob"),
        sbr=all_nan("sbr"),
        temp_basal=all_nan("temp_basal"),
        true_bolus=all_nan("true_bolus"),
        reported_bolus=all_nan("reported_bolus"),
        true_carb_value=all_nan("true_carb_value"),
        reported_carb_value=all_nan("reported_carb_value"),
        loop_cob=all_nan("loop_cob"),
    )

    assert renderer._simulation_start_position(trace) == 0
    assert len(renderer._shape_trace(trace).time) == n


def test_shaping_drops_nan_and_zero_events():
    """The shaping seam extracts only populated, nonzero events (AC #7)."""
    trace = _make_trace()
    # Inject a zero and a NaN alongside the single real bolus at index 7.
    bolus = trace.true_bolus.copy()
    bolus.iloc[1] = 0.0
    bolus.iloc[2] = np.nan
    trace = SimulationTrace(**{**vars(trace), "true_bolus": bolus})

    markers = renderer._shape_trace(trace).true_bolus
    assert markers.value.tolist() == [2.5]
    assert markers.time.size == 1
