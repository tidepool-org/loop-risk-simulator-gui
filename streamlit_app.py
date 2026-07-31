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
import os
import threading

import pandas as pd
import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx

from tidepool_data_science_simulator.utils import PROJECT_ROOT_DIR
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

# Root of the config library the selector browses. Defaults to the simulator's
# in-tree scenario_configs (correct for an editable/sibling install). Under the
# Phase 4 packaged bundle the simulator is a pinned, non-editable install, so
# PROJECT_ROOT_DIR points into site-packages where scenario_configs does NOT
# ship (it isn't part of the installed package) -- the bundle launcher sets
# LOOP_RISK_GUI_SCENARIO_CONFIGS_ROOT to the vendored subtree instead. Same env
# seam the integration tests use to point at a temp fixture root. Unset -> today's
# behavior, so editable/dev checkouts are unaffected.
_scenario_configs_root = os.environ.get("LOOP_RISK_GUI_SCENARIO_CONFIGS_ROOT")
if _scenario_configs_root:
    LIBRARY_ROOT = os.path.join(_scenario_configs_root, "tidepool_risk_v2", "loop_risk_v2_0")
else:
    LIBRARY_ROOT = os.path.join(PROJECT_ROOT_DIR, "scenario_configs", "tidepool_risk_v2", "loop_risk_v2_0")
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


def _init_session_state():
    defaults = {
        "cancel_event": None,
        "run_thread": None,
        "progress": None,  # (completed, total, risk_dir_name)
        "run_result": None,
        "run_error": None,
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
   gets the light override, not the whole widget. */
[data-testid="stSelectbox"] div[role="group"],
[data-testid="stMultiSelect"] div[role="group"],
[data-testid="stTextInput"] div[role="group"],
[data-testid="stNumberInput"] div[role="group"],
[data-testid="stTextArea"] div[role="group"],
[data-testid="stSelectbox"] input,
[data-testid="stMultiSelect"] input,
[data-testid="stTextInput"] input,
[data-testid="stNumberInput"] input,
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

    collections = _list_collections()
    if not collections:
        st.error(f"No scenario config collections found under {LIBRARY_ROOT}")
        return

    collection = st.selectbox("Config collection", options=collections)
    config_dir = os.path.join(LIBRARY_ROOT, collection)

    tlr_dirs = _list_tlr_dirs(config_dir)
    scope_choice = st.radio(
        "Run scope", options=["All directories in this collection", "One specific directory"], horizontal=True
    )
    target_risk_dir = None
    if scope_choice == "One specific directory":
        target_risk_dir = st.selectbox("TLR-* directory", options=tlr_dirs)

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
        for risk_dir_result in result.risk_dir_results:
            _render_risk_dir_result(risk_dir_result)


if __name__ == "__main__":
    main()
