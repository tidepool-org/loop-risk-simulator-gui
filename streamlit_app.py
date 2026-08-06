"""
Streamlit MVP for running a Tidepool Loop risk assessment without a terminal.

View layer only -- all simulator/validation logic lives in gui_runner.py.
Library browsing (listing config collections, resolving a chosen name to a
path) lives here, not in gui_runner.py, per the locked extensibility
constraint (design doc, 2026-07-21) that keeps the door open for a future
"configure parameters directly" mode to hand gui_runner a freshly-written
temp directory with no changes required there.
"""

import base64
import datetime
import io
import os
import tempfile
import threading
import zipfile

import pandas as pd
import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx

from tidepool_data_science_simulator.trace import TraceReadError, read_trace
# gui_runner is the single validated entry point for simulator behavior, and it
# re-exports severity_model's stage identity (classify_sim_id / STAGE_ORDER /
# STAGE_DISPLAY) so the view layer neither redeclares the stage vocabulary nor
# replicates the post_processing/ sys.path setup that makes it importable.
from tidepool_data_science_simulator.projects.risk.gui_runner import (
    classify_sim_id,
    run_risk_assessment,
    validate_config_dir,
    STAGE_DISPLAY,
    STAGE_ORDER,
)

from loop_home_renderer import render_loop_home_screen
# Zip/RTF assembly for the export (TRSET-7). Kept out of this module so the view
# layer stays presentation-only, per this file's docstring.
from export_bundle import build_export_zip, chart_filename
# Scenario-config generation from GUI-authored meal/bolus entries (TRSET-9). Same
# reasoning as export_bundle: streamlit-free, so the generated JSON is asserted
# directly rather than through AppTest.
import meal_config

# Root of the config library the selector browses. Defaults to the simulator's
# in-tree scenario_configs (correct for an editable/sibling install). Under the
# Phase 4 packaged bundle the simulator is a pinned, non-editable install, so
# PROJECT_ROOT_DIR points into site-packages where scenario_configs does NOT
# ship (it isn't part of the installed package) -- the bundle launcher sets
# LOOP_RISK_GUI_SCENARIO_CONFIGS_ROOT to the vendored subtree instead. Same env
# seam the integration tests use to point at a temp fixture root. Unset -> today's
# behavior, so editable/dev checkouts are unaffected. The resolution itself lives in
# meal_config (TRSET-9) so this module and the generator cannot disagree about
# where the library is.
LIBRARY_ROOT = os.path.join(
    meal_config.scenario_configs_root(), "tidepool_risk_v2", "loop_risk_v2_0"
)
LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Tidepool_Logo_Light_Large_3000.jpg")

# Logo sizing. We render an explicitly-sized HTML <img> rather than st.logo
# because st.logo exposes neither a numeric width nor alt-text (TRSET-3).
# Baseline is the width st.logo(size="large") rendered at (160px, measured in a
# running app); TRSET-3 asks for 1.5x that. Width is the only dimension set --
# the asset's native 3000x600 (5:1) aspect ratio then fixes the height (48px),
# so this stays DRY and there is no second magic number to keep in sync.
LOGO_BASELINE_WIDTH_PX = 160
LOGO_SCALE = 1.5
LOGO_WIDTH_PX = round(LOGO_BASELINE_WIDTH_PX * LOGO_SCALE)  # 240
LOGO_ALT_TEXT = "Tidepool logo"

# Regulatory disclaimer banner (TRSET-5). Verbatim, non-clinical-status text,
# defined once (DRY) and rendered as a custom caution box at the top of the
# main page. Colors reuse the existing Tidepool palette -- no new brand colors:
# the box background is the theme's secondaryBackgroundColor (#281946) and the
# text is the same #F5F5FA the _BRAND_CSS input-value override already uses, a
# pair the TRSET-4 contrast gate already clears (~14.7:1 >> 4.5:1). Styling is
# inline on the element (not folded into _BRAND_CSS) so the TRSET-4
# _css_override_text_color() invariant -- exactly one color override in
# _BRAND_CSS -- is preserved.
DISCLAIMER_TEXT = (
    "The Tidepool Risk Severity Evaluation Tool is not medical software. "
    "It is intended only for risk exploration and must not be used to make "
    "insulin dosing decisions."
)
_DISCLAIMER_BANNER_BG = "#281946"  # == config.toml theme secondaryBackgroundColor
_DISCLAIMER_BANNER_FG = "#F5F5FA"  # == the _BRAND_CSS input-value override color
_DISCLAIMER_WARNING_ICON = "⚠"  # ⚠ -- carries the warning independent of color

# Interim allowlist restricting the selector to the two collections in active
# use. Remove once the library gains a first-class notion of "active" vs
# "archived" collections.
#
# Overridable via env var so integration tests can register their own
# synthetic/temp fixture collections without the production UI seeing them --
# AppTest execs this file fresh per test, so an env var (read at import time,
# set by the test before `AppTest.from_file`) is the only seam available; a
# module-level monkeypatch wouldn't reach the exec'd copy.
_env_override = os.environ.get("LOOP_RISK_GUI_ALLOWED_COLLECTIONS")
_ALLOWED_COLLECTIONS = (
    tuple(_env_override.split(",")) if _env_override
    else ("loop_risk_v2_2_0_full", "loop_risk_v2_510k")
)


def _list_collections():
    if not os.path.isdir(LIBRARY_ROOT):
        return []
    missing = [
        name for name in _ALLOWED_COLLECTIONS
        if not os.path.isdir(os.path.join(LIBRARY_ROOT, name))
    ]
    if missing:
        raise FileNotFoundError(
            f"Allowlisted config collection(s) not found under {LIBRARY_ROOT}: {missing}. "
            "Update _ALLOWED_COLLECTIONS in streamlit_app.py if these were renamed or removed."
        )
    return list(_ALLOWED_COLLECTIONS)


def _list_tlr_dirs(collection_dir):
    return sorted(
        d for d in os.listdir(collection_dir)
        if os.path.isdir(os.path.join(collection_dir, d)) and "TLR-" in d
    )


def _profile_label(name, risk_dir_name):
    """Best-effort VP-profile label read back from a profile-bearing filename.

    Works for either name the run produces, because both embed the same
    "Simulation-Configuration-{risk_dir_name}_{profile}_profile..." segment:

      * a scenario config filename, e.g.
        "Simulation-Configuration-TLR-909_02_05_adolescent_profile_v1.json"
        (one config file is one VP profile) -- the row labels for the Loop-home
        charts;
      * gui_runner's figure filename, "{risk_dir_name}_{scenario_json_name}_{ts}.png".

    The profile token is the segment before "_profile". This is
    presentation-only -- the profile identity is already in the name, so we read
    it here rather than changing anything the simulator writes. Returns None if
    the name doesn't match the expected shape, so callers can fall back rather
    than fail.
    """
    stem = os.path.basename(name)
    if stem.lower().endswith(".png"):
        stem = stem[:-4]
    marker = "Simulation-Configuration-"
    start = stem.find(marker)
    if start == -1:
        return None
    scenario = stem[start + len(marker):].split(".json")[0]
    if scenario.startswith(risk_dir_name + "_"):
        scenario = scenario[len(risk_dir_name) + 1:]
    cut = scenario.lower().find("_profile")
    if cut <= 0:
        return None
    profile = scenario[:cut].replace("_", " ").strip()
    if not profile:
        return None
    return f"{profile.capitalize()} profile"


def _profile_stage_rows(trace_paths, risk_dir_name: str) -> list:
    """Group a RiskDirRunResult's trace paths into one row per VP profile.

    Returns ``[(profile_label, {stage: tsv_path}), ...]`` sorted by label. One
    scenario config file is one VP profile and its sim_ids are that profile's
    stages, so the scenario file is the row unit; the stage comes from
    ``classify_sim_id`` -- severity_model's single source of truth, not any
    prefix logic of our own.

    A sim_id whose stage does not classify is left out, so its column shows the
    explicit "No data" placeholder instead of being forced into a wrong stage.
    Two distinct sim_ids in one config file never classify to the same stage
    (verified across all 2724 config files in the library), so the per-file
    stage map cannot silently lose a sim.
    """
    rows = [
        (
            _profile_label(scenario_config_name, risk_dir_name) or scenario_config_name,
            {
                stage: tsv_path
                for sim_id, tsv_path in sims.items()
                if (stage := classify_sim_id(sim_id)) is not None
            },
        )
        for scenario_config_name, sims in trace_paths.items()
    ]
    return sorted(rows, key=lambda row: row[0])


@st.cache_data(show_spinner=False)
def _trace_png(tsv_path: str) -> bytes:
    """Loop-home-screen PNG bytes for one sim's trace, cached by path.

    Streamlit re-executes the whole script on every interaction, and one TLR
    directory is 3 charts per profile -- far too much matplotlib work to redo on
    each rerun. Run output directories are timestamped and never rewritten, so
    the path alone is a sound cache key.
    """
    return render_loop_home_screen(read_trace(tsv_path))


def _render_stage_chart(tsv_path) -> None:
    """Render one stage's Loop-home chart, or an explicit placeholder for it.

    A stage with no trace renders "No data" -- never blank-silent and never a
    fabricated chart. An unreadable trace is surfaced as its own message naming
    the file, so a failed read stays distinguishable from an absent stage rather
    than being swallowed.
    """
    if tsv_path is None:
        st.info("No data")
        return
    try:
        st.image(_trace_png(tsv_path))
    except (TraceReadError, OSError) as exc:
        st.warning(f"Could not render {os.path.basename(tsv_path)}: {exc}")


def _render_profile_charts(result) -> None:
    """Render one row per VP profile: three co-equal Loop-home chart columns.

    TWI-0006 section 2.g.ii: the stages are presented side by side as equals and
    the reader compares across them to judge which applies. There is deliberately
    no selector, no "primary" stage and no auto-collapsing -- every stage column
    is always drawn, carrying either its chart or an explicit placeholder.
    """
    rows = _profile_stage_rows(result.trace_paths, result.risk_dir_name)
    if not rows:
        st.caption("No simulation traces were produced for this directory.")
        return

    st.markdown("**Loop home-screen charts**")
    for profile_label, stage_paths in rows:
        st.markdown(f"**{profile_label}**")
        for column, stage in zip(st.columns(len(STAGE_ORDER)), STAGE_ORDER):
            with column:
                st.markdown(f"*{STAGE_DISPLAY[stage]}*")
                _render_stage_chart(stage_paths.get(stage))


# ---------------------------------------------------------------------------
# TRSET-9: configure meal and bolus entries, and generate configs from them
# ---------------------------------------------------------------------------

SOURCE_LIBRARY = "Choose from the config library"
SOURCE_CONFIGURE = "Configure meals & boluses"

# Display label -> meal_config mode. The modes are mutually exclusive and chosen
# once for the whole configuration.
MODE_LABELS = {
    "Standard meal": meal_config.MODE_STANDARD,
    "Multiplier of standard": meal_config.MODE_MULTIPLIER,
    "Custom carb value": meal_config.MODE_CUSTOM,
}

BOLUS_UNITS_CHOICE = "Units"
BOLUS_ACCEPT_CHOICE = "Accept Loop's recommendation"

# Why the pump timeline never carries the sentinel -- stated in the UI rather than
# silently dropped, since a user who typed it deserves to know where it went.
PUMP_SENTINEL_NOTE = (
    f"`{meal_config.ACCEPT_RECOMMENDATION}` is a patient-side value: it is written to "
    "the patient model only, never to the pump timeline, which the Loop bridge reads "
    "verbatim. The No Loop stage has no controller to make a recommendation, so it "
    "uses the numeric units given alongside it."
)

MAX_ENTRIES = 10


def _meal_entry_rows(key_prefix: str, mode: str, count: int, window_start) -> list:
    """Render `count` meal rows and return them as MealEntry objects.

    Every widget carries a full label (WCAG 1.3/2.1, guarded by the TRSET-4 tests),
    so the row index is part of the label rather than only a column header. Leaving
    the absorption duration blank omits it from the config entirely, which is what
    selects the parser's own default.
    """
    entries = []
    for index in range(count):
        time_column, duration_column, value_column = st.columns(3)
        with time_column:
            start_time = st.time_input(
                f"Meal {index + 1} start time",
                value=window_start,
                step=datetime.timedelta(minutes=5),
                key=f"{key_prefix}_meal_time_{index}",
            )
        with duration_column:
            duration = st.number_input(
                f"Meal {index + 1} absorption (minutes)",
                min_value=1,
                max_value=int(meal_config.CARB_DURATION_MINUTES_MAX),
                value=None,
                step=15,
                placeholder=f"default {meal_config.DEFAULT_CARB_DURATION_MINUTES}",
                key=f"{key_prefix}_meal_duration_{index}",
            )
        with value_column:
            if mode == meal_config.MODE_MULTIPLIER:
                value_input = st.number_input(
                    f"Meal {index + 1} multiplier of standard",
                    min_value=meal_config.MULTIPLIER_STEP,
                    value=1.0,
                    step=meal_config.MULTIPLIER_STEP,
                    key=f"{key_prefix}_meal_multiplier_{index}",
                )
            elif mode == meal_config.MODE_CUSTOM:
                value_input = st.number_input(
                    f"Meal {index + 1} carbs (g)",
                    min_value=1.0,
                    max_value=meal_config.CARB_GRAMS_MAX,
                    value=30.0,
                    step=1.0,
                    key=f"{key_prefix}_meal_grams_{index}",
                )
            else:
                value_input = None
                st.caption(f"Meal {index + 1}: per-profile standard")
        entries.append(meal_config.MealEntry(start_time, duration, value_input))
    return entries


def _bolus_entry_rows(key_prefix: str, count: int, window_start, allow_sentinel: bool) -> list:
    """Render `count` bolus rows and return them as BolusEntry objects.

    ``allow_sentinel`` is False for an independently-configured pump timeline: the
    sentinel is invalid there (see PUMP_SENTINEL_NOTE), so it is not offered rather
    than offered and then discarded.
    """
    entries = []
    for index in range(count):
        time_column, kind_column, value_column = st.columns(3)
        with time_column:
            bolus_time = st.time_input(
                f"Bolus {index + 1} time",
                value=window_start,
                step=datetime.timedelta(minutes=5),
                key=f"{key_prefix}_bolus_time_{index}",
            )
        with kind_column:
            kind = (
                st.selectbox(
                    f"Bolus {index + 1} value type",
                    options=[BOLUS_ACCEPT_CHOICE, BOLUS_UNITS_CHOICE],
                    key=f"{key_prefix}_bolus_kind_{index}",
                )
                if allow_sentinel
                else BOLUS_UNITS_CHOICE
            )
        with value_column:
            # No default: a dose is never guessed on the user's behalf. Left blank,
            # generation fails with a message naming the entry rather than quietly
            # running a 0 U bolus.
            units = st.number_input(
                f"Bolus {index + 1} units"
                + (" (No Loop stage)" if kind == BOLUS_ACCEPT_CHOICE else ""),
                min_value=0.0,
                max_value=meal_config.BOLUS_UNITS_MAX,
                value=None,
                step=0.1,
                placeholder="units",
                key=f"{key_prefix}_bolus_units_{index}",
            )
        if kind == BOLUS_ACCEPT_CHOICE:
            entries.append(
                meal_config.BolusEntry(
                    bolus_time, meal_config.ACCEPT_RECOMMENDATION, no_loop_units=units
                )
            )
        else:
            entries.append(meal_config.BolusEntry(bolus_time, units))
    return entries


def _entry_set_editor(key_prefix: str, heading: str, mode: str, window_start, allow_sentinel: bool):
    """One timeline's worth of entries: the meal rows and the bolus rows."""
    st.markdown(f"**{heading}**")
    meal_count = st.number_input(
        f"{heading}: number of meal entries",
        min_value=0,
        max_value=MAX_ENTRIES,
        value=1,
        step=1,
        key=f"{key_prefix}_meal_count",
    )
    meals = _meal_entry_rows(key_prefix, mode, int(meal_count), window_start)
    bolus_count = st.number_input(
        f"{heading}: number of bolus entries",
        min_value=0,
        max_value=MAX_ENTRIES,
        value=1,
        step=1,
        key=f"{key_prefix}_bolus_count",
    )
    boluses = _bolus_entry_rows(key_prefix, int(bolus_count), window_start, allow_sentinel)
    return meal_config.EntrySet(meals, boluses)


def _reset_generated_state() -> None:
    """Drop any generated configs, so a stale set is never run or downloaded."""
    st.session_state.generated_config_dir = None
    st.session_state.generated_risk_id = None
    st.session_state.generated_configs = None
    st.session_state.generated_error = None


def _generated_temp_dir() -> str:
    """Session-scoped temp root the generated config library is written under.

    Created once per session and reused; nothing is ever written into the real
    scenario-config library.
    """
    if st.session_state.generated_temp_dir is None:
        st.session_state.generated_temp_dir = tempfile.mkdtemp(prefix="risk_configs_")
    return st.session_state.generated_temp_dir


def _generate_configs(spec) -> None:
    """Generate the four configs for `spec` and write them to the temp library."""
    generated_risk_id = meal_config.risk_id()
    try:
        configs = meal_config.generate_configs(spec, generated_risk_id)
        config_dir = meal_config.write_config_library(
            configs, generated_risk_id, _generated_temp_dir()
        )
    except (meal_config.MealConfigError, OSError) as exc:  # surfaced, never swallowed
        _reset_generated_state()
        st.session_state.generated_error = str(exc)
    else:
        st.session_state.generated_config_dir = config_dir
        st.session_state.generated_risk_id = generated_risk_id
        st.session_state.generated_configs = configs
        st.session_state.generated_error = None
        # A previous run's export must not be offered against a new config set.
        _reset_export_state()


def generated_configs_zip(configs: dict, generated_risk_id: str) -> bytes:
    """The generated configs as one zip, nested under a single `<risk_id>/` folder.

    Small enough (four JSON files) to assemble in memory, unlike the run export.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, config in sorted(configs.items()):
            archive.writestr(
                os.path.join(generated_risk_id, filename), meal_config.config_bytes(config)
            )
    return buffer.getvalue()


def _render_generated_summary() -> None:
    """Report what was generated: the risk id, and the grams each profile resolved to."""
    configs = st.session_state.generated_configs
    generated_risk_id = st.session_state.generated_risk_id
    st.success(f"Generated {len(configs)} config file(s) with risk id `{generated_risk_id}`.")

    rows = []
    for filename, config in sorted(configs.items()):
        first_stage = config["override_config"][0]["patient"]["patient_model"]
        rows.append({
            "File": filename,
            "Carbs (g)": ", ".join(
                str(entry["value"]) for entry in first_stage["carb_entries"]
            ) or "-",
            "Boluses": ", ".join(
                str(entry["value"]) for entry in first_stage["bolus_entries"]
            ) or "-",
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True)

    st.download_button(
        "Download configs (.zip)",
        data=generated_configs_zip(configs, generated_risk_id),
        file_name=f"{generated_risk_id}_configs.zip",
        mime="application/zip",
    )


def _render_meal_config_editor():
    """The meal/bolus editor. Returns (config_dir, target_risk_dir), or (None, None).

    (None, None) until configs have been generated -- there is nothing to validate or
    run before that.
    """
    st.markdown("### Meal and bolus configuration")
    _, window_start, window_hours = meal_config.simulation_window()
    window_end = (
        datetime.datetime.combine(datetime.date.today(), window_start)
        + datetime.timedelta(hours=window_hours)
    ).time()
    st.caption(
        f"Entries must fall within the simulation window "
        f"{window_start.strftime('%H:%M')}-{window_end.strftime('%H:%M')} "
        f"({window_hours:g} hours). Leaving absorption blank uses the simulator's "
        f"{meal_config.DEFAULT_CARB_DURATION_MINUTES}-minute default."
    )

    mode_label = st.radio(
        "Meal value", options=list(MODE_LABELS), horizontal=True, key="meal_mode"
    )
    mode = MODE_LABELS[mode_label]
    if mode == meal_config.MODE_STANDARD:
        st.caption(
            "Standard per profile: "
            + ", ".join(
                f"{profile.display} {meal_config.standard_carb_grams(profile):g} g"
                for profile in meal_config.PROFILES
            )
        )
    elif mode == meal_config.MODE_MULTIPLIER:
        st.caption(
            f"Each profile's standard is scaled by the multiplier "
            f"({meal_config.MULTIPLIER_STEP} increments)."
        )
    else:
        st.caption("One carb value, applied identically to all four profiles.")

    aligned = st.toggle(
        "Use the same entries for the patient model and the pump",
        value=True,
        key="timelines_aligned",
    )
    st.caption(PUMP_SENTINEL_NOTE)

    patient_entries = _entry_set_editor(
        "pm", "Patient model", mode, window_start, allow_sentinel=True
    )
    if aligned:
        spec = meal_config.MealConfigSpec.aligned(mode, patient_entries)
    else:
        pump_entries = _entry_set_editor(
            "pump", "Pump", mode, window_start, allow_sentinel=False
        )
        spec = meal_config.MealConfigSpec(mode, patient_entries, pump_entries)

    if st.button("Generate configs"):
        _generate_configs(spec)

    if st.session_state.generated_error is not None:
        st.error(f"Could not generate configs: {st.session_state.generated_error}")
        return None, None
    if st.session_state.generated_configs is None:
        st.info("Generate configs to validate and run them.")
        return None, None

    _render_generated_summary()
    return st.session_state.generated_config_dir, st.session_state.generated_risk_id


def _render_library_selector():
    """Today's library browsing. Returns (config_dir, target_risk_dir)."""
    collections = _list_collections()
    if not collections:
        st.error(f"No scenario config collections found under {LIBRARY_ROOT}")
        return None, None

    collection = st.selectbox("Config collection", options=collections, key="config_collection")
    config_dir = os.path.join(LIBRARY_ROOT, collection)

    tlr_dirs = _list_tlr_dirs(config_dir)
    # Keyed so tests address it by name: it is no longer the app's first radio now
    # that the config source precedes it.
    scope_choice = st.radio(
        "Run scope",
        options=["All directories in this collection", "One specific directory"],
        horizontal=True,
        key="run_scope",
    )
    target_risk_dir = None
    if scope_choice == "One specific directory":
        target_risk_dir = st.selectbox("TLR-* directory", options=tlr_dirs)
    return config_dir, target_risk_dir


def _reset_export_state() -> None:
    """Drop any built export, so it is never offered against a different run."""
    st.session_state.export_zip_path = None
    st.session_state.export_skipped = []
    st.session_state.export_error = None


def _export_temp_dir() -> str:
    """Session-scoped temp directory the export zips are built in.

    A temp path (not memory, not a hardcoded location) so a large run's archive
    streams to disk; created once per session and reused, because the zip has to
    outlive the rerun that the download click triggers.
    """
    if st.session_state.export_temp_dir is None:
        st.session_state.export_temp_dir = tempfile.mkdtemp(prefix="risk_export_")
    return st.session_state.export_temp_dir


def _export_chart_files(result) -> tuple:
    """``(charts, skipped)`` for one TLR dir: one PNG per profile x present stage.

    Charts are ``(filename, png_bytes)``, reusing the same
    ``_trace_png`` -> ``render_loop_home_screen`` path (and its cache) the results
    pane draws, so exported and on-screen charts cannot diverge. A stage with no
    trace, or one whose trace cannot be read, is skipped and reported in
    ``skipped`` -- never written as a blank or fabricated chart.
    """
    charts = []
    skipped = []
    for profile_label, stage_paths in _profile_stage_rows(result.trace_paths, result.risk_dir_name):
        for stage in STAGE_ORDER:
            tsv_path = stage_paths.get(stage)
            label = f"{result.risk_dir_name} / {profile_label} / {STAGE_DISPLAY[stage]}"
            if tsv_path is None:
                skipped.append(f"{label}: no simulation trace for this stage.")
                continue
            try:
                png_bytes = _trace_png(tsv_path)
            except (TraceReadError, OSError) as exc:
                skipped.append(f"{label}: could not render {os.path.basename(tsv_path)}: {exc}")
                continue
            charts.append(
                (chart_filename(result.risk_dir_name, profile_label, STAGE_DISPLAY[stage]), png_bytes)
            )
    return charts, skipped


@st.cache_data(show_spinner=False)
def _zip_bytes(zip_path: str) -> bytes:
    """The built zip's bytes for st.download_button, cached by path.

    st.download_button needs its payload present on every rerun, and each
    interaction reruns the script; the zip is written once to a unique temp path
    and never rewritten, so the path alone is a sound cache key.
    """
    with open(zip_path, "rb") as zip_file:
        return zip_file.read()


def _build_export(run_result) -> None:
    """Build the export zip for a completed run and record the outcome.

    Two steps (build, then download) is not a stylistic choice: st.download_button
    materializes its payload at render time, so a single-click version would
    rebuild the whole zip on every rerun.
    """
    try:
        charts = []
        skipped = []
        for risk_dir_result in run_result.risk_dir_results:
            dir_charts, dir_skipped = _export_chart_files(risk_dir_result)
            charts.extend(dir_charts)
            skipped.extend(dir_skipped)
        zip_path = build_export_zip(
            run_result.save_dir,
            charts,
            _export_temp_dir(),
            # TRSET-9: a run started from generated configs ships them alongside its
            # results, so the export records exactly what was run. Empty for a run
            # started from the library, which has no generated configs.
            generated_configs=st.session_state.run_generated_configs,
        )
    except Exception as exc:  # surfaced in the UI -- never swallowed
        _reset_export_state()
        st.session_state.export_error = str(exc)
    else:
        st.session_state.export_zip_path = zip_path
        st.session_state.export_skipped = skipped
        st.session_state.export_error = None


def _render_export_control(run_result) -> None:
    """Offer the completed run as one downloadable zip.

    The zip carries the severity-summary RTFs, the Loop-home chart PNGs and every
    raw output the run wrote. Only shown for a run that completed -- a cancelled
    run has no complete set of outputs to export.
    """
    st.markdown("**Export**")
    st.caption(
        "One zip with the severity-summary RTFs, the Loop home-screen charts and "
        "this run's raw output files."
    )
    if st.button("Export results"):
        _build_export(run_result)

    if st.session_state.export_error is not None:
        st.error(f"Export failed: {st.session_state.export_error}")
    if st.session_state.export_skipped:
        st.warning(
            "Some charts were not exported:\n\n"
            + "\n".join(f"- {reason}" for reason in st.session_state.export_skipped)
        )

    zip_path = st.session_state.export_zip_path
    if zip_path is not None:
        st.download_button(
            "Download export (.zip)",
            data=_zip_bytes(zip_path),
            file_name=os.path.basename(zip_path),
            mime="application/zip",
        )


def _init_session_state():
    defaults = {
        "cancel_event": None,
        "run_thread": None,
        "progress": None,  # (completed, total, risk_dir_name)
        "run_result": None,
        "run_error": None,
        # Export state (TRSET-7). Held across reruns because clicking the
        # download button is itself a rerun, and the zip must survive it.
        "export_temp_dir": None,
        "export_zip_path": None,
        "export_skipped": [],
        "export_error": None,
        # Generated-config state (TRSET-9). Held across reruns because generating
        # and then running are separate interactions.
        "generated_temp_dir": None,
        "generated_config_dir": None,
        "generated_risk_id": None,
        "generated_configs": None,
        "generated_error": None,
        # The configs the CURRENT run was started from, as (filename, bytes) --
        # snapshotted at run start so the export ships what actually ran, even if
        # the editor has since been changed. Empty for a library run.
        "run_generated_configs": (),
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _start_run(config_dir, target_risk_dir):
    cancel_event = threading.Event()
    st.session_state.cancel_event = cancel_event
    st.session_state.progress = None
    st.session_state.run_result = None
    st.session_state.run_error = None
    generated = st.session_state.generated_configs
    st.session_state.run_generated_configs = (
        tuple(
            (filename, meal_config.config_bytes(config))
            for filename, config in sorted(generated.items())
        )
        if generated is not None and config_dir == st.session_state.generated_config_dir
        else ()
    )
    # A previous run's export must not be offered against the new run.
    _reset_export_state()

    def _progress_callback(completed, total, risk_dir_name):
        st.session_state.progress = (completed, total, risk_dir_name)

    def _target():
        try:
            result = run_risk_assessment(
                config_dir,
                target_risk_dir=target_risk_dir,
                progress_callback=_progress_callback,
                cancel_event=cancel_event,
            )
            st.session_state.run_result = result
        except Exception as exc:  # surfaced in the UI -- never swallowed
            st.session_state.run_error = str(exc)

    thread = threading.Thread(target=_target, daemon=True)
    # A bare background thread has no ScriptRunContext, so writes to
    # st.session_state from inside it silently no-op -- this must be attached
    # before start() for _target's session_state writes to actually persist.
    add_script_run_ctx(thread)
    st.session_state.run_thread = thread
    thread.start()


def _render_stage_table(assessment):
    rows = []
    for stage in STAGE_ORDER:
        stage_result = assessment.stages.get(stage)
        if stage_result is None:
            continue
        rows.append({
            "Stage": STAGE_DISPLAY[stage],
            "Harm type": stage_result.harm_type,
            "Severity": stage_result.severity,
            "TIR %": stage_result.tir,
            "TBR %": stage_result.tbr,
            "LBGI": stage_result.lbgi_value_avg,
            "DKAI": stage_result.dka_index_value_avg,
            "TAR %": stage_result.tar,
            "N sims": stage_result.n_sims,
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True)


def _render_risk_dir_result(result):
    with st.expander(result.risk_dir_name, expanded=True):
        if result.assessment is None:
            st.warning(
                "No usable data was found for this directory "
                "(no summary_results_Simulation-Configuration-TLR*.csv files)."
            )
            return

        assessment = result.assessment
        st.caption(f"{assessment.profile_count} profile(s) · timestamp {assessment.timestamp}")
        _render_stage_table(assessment)

        if assessment.catastrophic_findings:
            st.markdown("**Catastrophic findings (severity 4→5):**")
            st.dataframe(
                pd.DataFrame([f.to_dict() for f in assessment.catastrophic_findings]),
                hide_index=True,
            )

        if assessment.outlier_status != "ok":
            st.caption(f"Outlier detection: {assessment.outlier_status}")
        elif assessment.outlier_findings:
            st.markdown("**Outlier findings:**")
            st.dataframe(
                pd.DataFrame([f.to_dict() for f in assessment.outlier_findings]),
                hide_index=True,
            )

        # TRSET-23: the generic three-panel simulator PNGs (result.png_paths) are
        # no longer rendered -- per-stage, per-profile Loop-home charts replace
        # them. The png_paths plumbing stays in gui_runner for now (removal is a
        # follow-up once these charts are validated in use).
        _render_profile_charts(result)


@st.fragment(run_every=1)
def _render_progress_fragment():
    thread = st.session_state.run_thread
    if thread is None:
        return

    if thread.is_alive():
        progress = st.session_state.progress
        if progress is None:
            st.info("Starting...")
        else:
            completed, total, risk_dir_name = progress
            st.progress(completed / total if total else 0, text=f"Run {completed} of {total}: {risk_dir_name}")
        if st.button("Cancel", key="cancel_run_button"):
            st.session_state.cancel_event.set()
        return

    # Thread finished -- clear it so this fragment stops polling.
    st.session_state.run_thread = None
    st.rerun()


# DM Sans is the sanctioned web fallback for Basis Grotesque Pro (not
# licensed for web embedding). Streamlit's own expander already resolves
# secondaryBackgroundColor (brand indigo, #281946) against textColor with
# adequate contrast on its own -- verified against a throwaway probe app --
# so it needs no override. Input-style widgets (selectbox, text/number
# input) don't get the same treatment: their value box also renders on
# secondaryBackgroundColor but keeps the page's default (indigo) text,
# making it unreadable, so those need a targeted fix below.
_BRAND_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&display=swap');

/* Streamlit's own typography rules win on specificity against a plain
   html/body selector, so this needs !important to actually take. */
.stApp, .stApp * {
    font-family: 'DM Sans', sans-serif !important;
}

/* ...but that universal !important also clobbers the Material icon font on
   Streamlit's glyph spans (e.g. the expander toggle), so the ligature text
   "keyboard_arrow_down" renders literally and overflows onto the label -- the
   TRSET-3 results-header overlap. Restore the icon font on those spans. The
   .stApp descendant selector (0,2,0) outweighs .stApp * (0,1,0), so this wins
   regardless of source order. */
.stApp [data-testid="stIconMaterial"] {
    font-family: 'Material Symbols Rounded' !important;
}

/* Their label stays on the page background and must keep the default dark
   text, so only the value box (input/select + its role="group" wrapper)
   gets the light override, not the whole widget.

   stTimeInput joined the list with TRSET-9's meal/bolus editor: without it the
   entered time renders in the page's default indigo on the indigo value box --
   #281946 on #281946, invisible (verified in a running app). It needs its own
   selector rather than the role="group" one: it is a baseweb select whose value
   sits in a plain div, not an input, so neither the group nor the input selector
   reaches it. The label is a sibling stWidgetLabel, so this still leaves the label
   on the page background with its default dark text. */
[data-testid="stSelectbox"] div[role="group"],
[data-testid="stMultiSelect"] div[role="group"],
[data-testid="stTextInput"] div[role="group"],
[data-testid="stNumberInput"] div[role="group"],
[data-testid="stTextArea"] div[role="group"],
[data-testid="stTimeInput"] div[data-baseweb="select"],
[data-testid="stTimeInput"] [data-testid="stTimeInputTimeDisplay"],
[data-testid="stSelectbox"] input,
[data-testid="stMultiSelect"] input,
[data-testid="stTextInput"] input,
[data-testid="stNumberInput"] input,
[data-testid="stTimeInput"] input,
[data-testid="stTextArea"] textarea {
    color: #F5F5FA;
}
</style>
"""


def _render_disclaimer_banner():
    """Render the persistent regulatory disclaimer as a caution box at page top.

    A custom-styled ``role="alert"`` box (not ``st.warning``, so assertions keyed
    on ``at.warning`` are unaffected). The meaning is carried by text and the ⚠
    glyph, not color alone (WCAG 1.3); the icon is ``aria-hidden`` since the
    alert text already conveys it. Inline styles only -- no positive tabindex,
    no keyboard trap (WCAG 2.1).
    """
    st.markdown(
        f'<div role="alert" style="background-color: {_DISCLAIMER_BANNER_BG}; '
        f'color: {_DISCLAIMER_BANNER_FG}; padding: 0.75rem 1rem; '
        f'border-radius: 0.5rem; margin-bottom: 1rem;">'
        f'<span aria-hidden="true">{_DISCLAIMER_WARNING_ICON}</span> '
        f'{DISCLAIMER_TEXT}</div>',
        unsafe_allow_html=True,
    )


def _render_logo():
    # HTML <img> (not st.logo/st.image) so we can set an explicit numeric width
    # and alt-text, neither of which st.logo exposes (TRSET-3). The asset is
    # embedded as a data URI so a single markdown call is fully self-contained
    # (no static-file-serving config); width is fixed, height follows the
    # native aspect ratio.
    with open(LOGO_PATH, "rb") as fh:
        encoded = base64.b64encode(fh.read()).decode("ascii")
    st.markdown(
        f'<img src="data:image/jpeg;base64,{encoded}" '
        f'width="{LOGO_WIDTH_PX}" alt="{LOGO_ALT_TEXT}">',
        unsafe_allow_html=True,
    )


def main():
    st.set_page_config(page_title="Tidepool Loop Risk Severity Estimation Tool", layout="wide")
    st.markdown(_BRAND_CSS, unsafe_allow_html=True)
    _render_disclaimer_banner()
    if os.path.exists(LOGO_PATH):
        _render_logo()
    _init_session_state()
    st.title("Tidepool Loop Risk Severity Estimation Tool")
    st.markdown(
        "Estimates the clinical risk severity of Tidepool Loop across a library of "
        "virtual-patient scenarios, summarizing the glycemic and DKA risk metrics for each."
    )

    config_source = st.radio(
        "Config source",
        options=[SOURCE_LIBRARY, SOURCE_CONFIGURE],
        horizontal=True,
        key="config_source",
    )
    if config_source == SOURCE_LIBRARY:
        config_dir, target_risk_dir = _render_library_selector()
    else:
        config_dir, target_risk_dir = _render_meal_config_editor()

    if config_dir is None:
        return

    validation_result = validate_config_dir(config_dir, target_risk_dir)
    if validation_result.errors_by_file:
        st.error(f"{len(validation_result.errors_by_file)} config file(s) have validation errors:")
        for path, errors in validation_result.errors_by_file.items():
            for error in errors:
                st.write(f"- `{os.path.basename(path)}`: {error.error_message}")
    if validation_result.warnings_by_file:
        with st.expander(f"{len(validation_result.warnings_by_file)} config file(s) have warnings"):
            for path, warnings in validation_result.warnings_by_file.items():
                for warning in warnings:
                    st.write(f"- `{os.path.basename(path)}`: {warning.warning_message}")

    run_in_progress = st.session_state.run_thread is not None and st.session_state.run_thread.is_alive()

    if st.button("Run Tool", disabled=bool(validation_result.errors_by_file) or run_in_progress):
        _start_run(config_dir, target_risk_dir)
        st.rerun()

    if st.session_state.run_thread is not None:
        _render_progress_fragment()

    if st.session_state.run_error is not None:
        st.error(f"Run failed: {st.session_state.run_error}")

    result = st.session_state.run_result
    if result is not None:
        if result.cancelled:
            st.warning(f"Run cancelled. {len(result.risk_dir_results)} director(y/ies) completed before cancellation.")
        else:
            # Above the per-directory expanders so it is not buried under a
            # collection's worth of charts.
            _render_export_control(result)
        for risk_dir_result in result.risk_dir_results:
            _render_risk_dir_result(risk_dir_result)


if __name__ == "__main__":
    main()
