"""Loop-home-screen static chart renderer (TRSET-22).

Consumes a :class:`SimulationTrace` (TRSET-21) and produces a static PNG that
*evokes* Loop's iOS home screen -- three stacked sections (Glucose, Active
Insulin, Active Carbohydrates) drawn from the run's **history only**. There is
no prediction/forecast curve (the trace exposes none by design), no "now"
divider, and no reserved forward gutter: the simulation history spans the full
chart width. The upstream seed CGM history (~12h of warmup that gives the run
physiologically true insulin needs but is not itself simulated output) is
trimmed in the shaping step, so every series -- glucose included -- starts at
simulation t=0.

This is a presentation artifact for an internal audience that contextualizes
risk-simulation outcomes in a familiar frame while remaining clearly
non-clinical. It is *not* the regulatory-consumption plot -- the simulator's own
``visualization/sim_viz.py::plot_sim_results`` remains that, unchanged and not
imported here.

Import surface is pinned to the ``trace`` tier only (Known Constraints): this
module imports nothing from ``gui_runner``/``projects``/``validation``/
``post_processing``, which keeps the renderer a pure, installable
``trace``-consumer.

Structure: a shaping step (:func:`_shape_trace`) turns the trace into a plain,
backend-agnostic :class:`LoopHomeChartData` intermediate; a drawing step
(:func:`_draw`) renders that intermediate with matplotlib. This one seam is what
lets a future Plotly backend consume the same shaped data (static now,
Plotly-ready structure) -- no Plotly dependency is added here.
"""
import io
from dataclasses import dataclass

# Force the Agg (headless, non-interactive) backend BEFORE importing pyplot.
# The GUI renders on a background thread (streamlit_app runs each assessment off
# a worker thread), and the AppKit ``macosx`` backend is not thread-safe; global
# pyplot state also leaks across repeated saves. Agg + fig.savefig()/close (never
# global plt.savefig) is the af787571 / f2a65282 lesson.
import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from tidepool_data_science_simulator.trace import SimulationTrace  # noqa: E402

# --- Target range band (AC #5) --------------------------------------------
# Fixed MVP band, defined once (DRY). These are rendering constants, NOT read
# from patient/scenario data (the trace carries no target-range column).
# Reading the band from the scenario config is explicitly future work.
TARGET_RANGE_LOW_MG_DL = 100
TARGET_RANGE_HIGH_MG_DL = 110

# --- Output geometry -------------------------------------------------------
# Portrait, evoking the stacked phone layout. Fixed size + dpi so the produced
# image has deterministic dimensions (900 x 1100 px) the integration test can
# assert against.
FIGSIZE_INCHES = (9, 11)
DPI = 100

# --- Palette (AC #6) -------------------------------------------------------
# Reuse the app's existing Tidepool tokens (streamlit_app._BRAND_CSS /
# .streamlit/config.toml) -- no new brand colors. Series are distinguished by
# color + linestyle/marker. Deliberately NO green "in-range good" affordance:
# the target band is a neutral periwinkle wash, never a clinical status signal.
_INDIGO = "#281946"      # config.toml textColor / secondaryBackgroundColor
_PERIWINKLE = "#627CFF"  # config.toml primaryColor
_BAND_ALPHA = 0.15
_AREA_ALPHA = 0.25
_GRID_ALPHA = 0.3


@dataclass(frozen=True)
class MarkerSeries:
    """Discrete event markers (boluses, carb entries) as parallel arrays.

    ``time`` and ``value`` are already filtered to non-NaN, nonzero events --
    the shaping step drops the empty/zero cells so the drawing step never
    fabricates or forward-fills an event (AC #7).
    """

    time: np.ndarray
    value: np.ndarray


@dataclass(frozen=True)
class LoopHomeChartData:
    """Backend-agnostic shaped intermediate for one Loop-home chart.

    Every field is a plain pandas/numpy value (no matplotlib artifacts), so a
    future Plotly backend can consume exactly this (AC #8). NaN gaps are
    preserved as-is on the line series; discrete events are pre-extracted into
    :class:`MarkerSeries`.
    """

    sim_id: str
    time: pd.DatetimeIndex
    # Glucose
    bg: pd.Series
    bg_sensor: pd.Series
    target_low: float
    target_high: float
    # Active insulin
    iob: pd.Series
    sbr: pd.Series
    temp_basal: pd.Series
    true_bolus: MarkerSeries
    reported_bolus: MarkerSeries
    # Active carbohydrates
    true_carb: MarkerSeries
    reported_carb: MarkerSeries
    loop_cob: pd.Series


# Series that carry the pre-simulation seed CGM history. During warmup the
# simulator stores ONLY true & sensor glucose (models/simulation.py::init seeds
# those rows with controller_state=None and active=0); every other series is NaN
# until simulation t=0, where the first full state is stored (active=1). So the
# first row carrying any non-glucose state marks t=0. The authoritative marker
# is the results' ``active`` flag, but SimulationTrace does not expose it and
# TRSET-22 must not modify the simulator -- this "first non-glucose state" rule
# is an exact, data-derived equivalent given how init() seeds the history.
_SEED_ONLY_FIELDS = ("bg", "bg_sensor")


def _simulation_start_position(trace: SimulationTrace) -> int:
    """Positional index of simulation t=0 within the trace.

    The ~12h of seed glucose history (needed upstream for physiologically true
    insulin needs, but not part of the simulation) is dropped from the chart:
    everything -- glucose included -- starts at t=0. Returns 0 if the trace
    carries no non-glucose state (nothing to trim), so a degenerate/all-seed
    trace still renders rather than collapsing to empty.
    """
    populated = np.zeros(len(trace.time), dtype=bool)
    for field in vars(trace):
        if field in ("sim_id", "time") or field in _SEED_ONLY_FIELDS:
            continue
        populated |= getattr(trace, field).notna().to_numpy()
    if not populated.any():
        return 0
    return int(populated.argmax())


def _nonzero_events(series):
    """Extract discrete events (non-NaN and nonzero) as a :class:`MarkerSeries`.

    Boluses and carb entries are sparse event series -- mostly NaN/0 with the
    occasional dose/entry. Selecting the populated cells here (vectorized, no
    Python loop) keeps event extraction in the shaping seam and out of the
    drawing code, and guarantees no zero/empty cell is ever plotted as a marker.
    """
    mask = series.notna().to_numpy() & (series.fillna(0).to_numpy() != 0)
    return MarkerSeries(
        time=series.index.to_numpy()[mask],
        value=series.to_numpy()[mask],
    )


def _shape_trace(trace: SimulationTrace) -> LoopHomeChartData:
    """Shape a :class:`SimulationTrace` into a drawing-ready intermediate.

    Pure data-shaping -- no matplotlib. The pre-simulation seed glucose history
    is trimmed here: every series (glucose included) starts at simulation t=0
    (:func:`_simulation_start_position`). Line series (BG, IOB, basal, COB) then
    pass through unchanged so their NaN cells stay gaps (AC #7); discrete events
    (boluses, carbs) are pre-extracted into :class:`MarkerSeries`. The target
    band bounds are attached from the module constants (AC #5).
    """
    start = _simulation_start_position(trace)
    window = slice(start, None)
    return LoopHomeChartData(
        sim_id=trace.sim_id,
        time=trace.time[start:],
        bg=trace.bg.iloc[window],
        bg_sensor=trace.bg_sensor.iloc[window],
        target_low=TARGET_RANGE_LOW_MG_DL,
        target_high=TARGET_RANGE_HIGH_MG_DL,
        iob=trace.iob.iloc[window],
        sbr=trace.sbr.iloc[window],
        temp_basal=trace.temp_basal.iloc[window],
        true_bolus=_nonzero_events(trace.true_bolus.iloc[window]),
        reported_bolus=_nonzero_events(trace.reported_bolus.iloc[window]),
        true_carb=_nonzero_events(trace.true_carb_value.iloc[window]),
        reported_carb=_nonzero_events(trace.reported_carb_value.iloc[window]),
        loop_cob=trace.loop_cob.iloc[window],
    )


def _style_section(ax, title, ylabel, accent):
    """Apply the shared per-section styling (label, y-axis, grid)."""
    ax.set_title(title, loc="left", color=accent, fontweight="bold", fontsize=13)
    ax.set_ylabel(ylabel)
    ax.grid(True, alpha=_GRID_ALPHA)
    ax.margins(x=0)  # history spans the full width -- no forward gutter (AC #3)


def _draw(data: LoopHomeChartData):
    """Render a shaped :class:`LoopHomeChartData` into a matplotlib Figure.

    Returns an open Figure -- the caller owns closing it. Three stacked sections
    share the x-axis so the history reads on one timeline across the full width;
    there is no "now" divider and no forecast series (AC #3/#4).
    """
    fig, (ax_bg, ax_insulin, ax_carb) = plt.subplots(
        3, 1, figsize=FIGSIZE_INCHES, dpi=DPI, sharex=True
    )

    time = data.time

    # --- Glucose --------------------------------------------------------
    # Neutral target-range wash (AC #5/#6) -- not a clinical "good" signal.
    ax_bg.axhspan(
        data.target_low, data.target_high, color=_PERIWINKLE, alpha=_BAND_ALPHA,
        label="Target range ({:g}-{:g} mg/dL)".format(data.target_low, data.target_high),
    )
    ax_bg.plot(time, data.bg.to_numpy(), color=_INDIGO, linewidth=1.8, label="Glucose")
    ax_bg.plot(
        time, data.bg_sensor.to_numpy(), color=_PERIWINKLE, linestyle="none",
        marker=".", markersize=4, alpha=0.8, label="Sensor",
    )
    _style_section(ax_bg, "Glucose", "mg/dL", _INDIGO)
    ax_bg.legend(loc="upper right", fontsize=8, framealpha=0.9)

    # --- Active Insulin -------------------------------------------------
    ax_insulin.fill_between(
        time, data.iob.to_numpy(), 0, color=_PERIWINKLE, alpha=_AREA_ALPHA,
        label="Active insulin (IOB)",
    )
    ax_insulin.plot(
        time, data.sbr.to_numpy(), color=_INDIGO, linestyle=":", linewidth=1.3,
        label="Scheduled basal",
    )
    ax_insulin.plot(
        time, data.temp_basal.to_numpy(), color=_PERIWINKLE, linestyle="--",
        linewidth=1.3, label="Temp basal",
    )
    # Simple dose markers (autobolus/manual iconography is future work).
    if data.true_bolus.time.size:
        ax_insulin.scatter(
            data.true_bolus.time, data.true_bolus.value, color=_INDIGO, marker="P",
            s=60, zorder=3, label="Bolus",
        )
    if data.reported_bolus.time.size:
        ax_insulin.scatter(
            data.reported_bolus.time, data.reported_bolus.value, color=_PERIWINKLE,
            marker="X", s=50, zorder=3, label="Reported bolus",
        )
    _style_section(ax_insulin, "Active Insulin", "U / U per hr", _PERIWINKLE)
    ax_insulin.set_ylim(bottom=0)
    ax_insulin.legend(loc="upper right", fontsize=8, framealpha=0.9, ncol=2)

    # --- Active Carbohydrates -------------------------------------------
    # loop_cob may be entirely empty on non-active-Loop stages -- an all-NaN
    # series plots as nothing, without error (AC #7).
    ax_carb.plot(time, data.loop_cob.to_numpy(), color=_PERIWINKLE, linewidth=1.5,
                 label="Carbs on board")
    if data.true_carb.time.size:
        ax_carb.scatter(
            data.true_carb.time, data.true_carb.value, color=_INDIGO, marker="P",
            s=60, zorder=3, label="Carb entry",
        )
    if data.reported_carb.time.size:
        ax_carb.scatter(
            data.reported_carb.time, data.reported_carb.value, color=_PERIWINKLE,
            marker="X", s=50, zorder=3, label="Reported carbs",
        )
    _style_section(ax_carb, "Active Carbohydrates", "g", _INDIGO)
    ax_carb.set_ylim(bottom=0)
    ax_carb.legend(loc="upper right", fontsize=8, framealpha=0.9)

    # Full-width history: pin x-limits to the data span so there is no empty
    # forward gutter (AC #3). Guard against an all-empty/degenerate axis.
    if len(time):
        ax_carb.set_xlim(time.min(), time.max())
    # Adaptive date ticks so the axis reads well across window lengths -- a short
    # early-stopped run and a multi-hour run both get sensible ticks (a fixed
    # 2-hour cadence would leave a short window with a single label).
    locator = mdates.AutoDateLocator()
    ax_carb.xaxis.set_major_locator(locator)
    ax_carb.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    ax_carb.set_xlabel("Time")

    fig.suptitle(data.sim_id, fontsize=10, color=_INDIGO, y=0.995)
    fig.tight_layout()
    return fig


def build_loop_home_figure(trace: SimulationTrace):
    """Build the Loop-home Figure for a trace (shaping + drawing).

    Returns an **open** matplotlib Figure; the caller is responsible for closing
    it (use :func:`render_loop_home_screen` for the leak-free save path). Exposed
    for callers/tests that need to introspect the figure before it is saved.
    """
    return _draw(_shape_trace(trace))


def render_loop_home_screen(trace: SimulationTrace, output_path=None) -> bytes:
    """Render a trace to a Loop-home-styled PNG.

    Parameters
    ----------
    trace : SimulationTrace
        The run history to render.
    output_path : str or os.PathLike, optional
        If given, the same PNG bytes are also written to this path.

    Returns
    -------
    bytes
        The rendered PNG image bytes.

    Notes
    -----
    The figure is always closed before returning (never a global
    ``plt.savefig``), so this is safe to call repeatedly in one process without
    figure-state leakage (AC #1 / af787571 lesson).
    """
    fig = build_loop_home_figure(trace)
    try:
        buffer = io.BytesIO()
        fig.savefig(buffer, format="png", dpi=DPI)
        png_bytes = buffer.getvalue()
    finally:
        plt.close(fig)

    if output_path is not None:
        with open(output_path, "wb") as handle:
            handle.write(png_bytes)

    return png_bytes
