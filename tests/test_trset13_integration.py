"""End-to-end integration test for TRSET-13 selectable duration (Feature gate).

Per the approved plan: no mocks anywhere in the exercised path, and every assertion
is made at a boundary of the full system -- the generated JSON on disk, the config
the run itself resolved and wrote, the trace the simulator produced, and the app's
own rendered results -- never on the functions that produced them.

    the app's own duration picker, driven through AppTest
        -> meal_config.generate_configs / write_config_library   (real JSON on disk)
        -> gui_runner.validate_config_dir                        (real ConfigValidator)
        -> gui_runner.run_risk_assessment                        (real ScenarioParserV2,
                                                                  real Swift simulations)
        -> the results pane                                      (the sub-8-hour marking)

Two real runs, because one cannot prove both halves:

  * A 2-hour run, in the default suite. It proves the selected duration reaches every
    override, survives the parser into the ``Simulation`` the run actually built, and
    shortens the trace -- and that the results pane then marks LBGI/DKAI/Severity as
    not valid. Shorter than the 8-hour default, so it is also cheaper than TRSET-9's.
  * A 24-hour run, behind ``@pytest.mark.slow`` (deselected by pytest.ini's addopts;
    run it with ``-m slow``). It is the only thing that proves the midnight-crossing
    path in a real run: an entry authored at 23:30 on the start day, and a trace that
    continues into the following calendar day. Roughly three times the 8-hour cost,
    which is why it is opt-in.

The 8- and 23-hour options are asserted at the config boundary without being run:
what is unproven for them is the number in the JSON, and running a third and fourth
real simulation would not prove anything the 2- and 24-hour runs do not already.

The temp library is the established one: real layout from the ``tidepool_risk_v2``
level down, the real ``reusable/`` symlinked so pointer resolution behaves as it does
for a real collection, the app's own env seams, and zero writes into the installed
library.
"""

import datetime
import json
import os

import pandas as pd
import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from tidepool_data_science_simulator.projects.risk.gui_runner import (  # noqa: E402
    validate_config_dir,
)

import meal_config  # noqa: E402
import streamlit_app  # noqa: E402


SHORT_RUN_HOURS = 2.0
MEAL_TIME = datetime.time(12, 0)
BOLUS_TIME = datetime.time(12, 0)
NO_LOOP_UNITS = 3.3

# An entry only a midnight-crossing window can hold: past the 20:00 end of the
# 8-hour default, and inside the 24-hour one.
LATE_MEAL_TIME = datetime.time(23, 30)

RUN_TIMEOUT_SECONDS = 900
SLOW_RUN_TIMEOUT_SECONDS = 2700

# The simulator steps every 5 minutes, so a trace ends within one step of the
# requested duration rather than exactly on it.
STEP = datetime.timedelta(minutes=5)

EMPTY_COLLECTION = "_pytest_trset13_empty_collection"

DURATION_LABEL_BY_HOURS = {
    hours: label for label, hours in streamlit_app.DURATION_LABELS.items() if hours
}


@pytest.fixture(scope="module")
def temp_library(tmp_path_factory):
    """Point the app at a temp scenario library holding one EMPTY collection.

    Same fixture shape TRSET-9's suite uses: the real ``reusable/`` symlinked in, so
    the baselines the generator reads and the pointers the run resolves are real,
    while nothing this feature writes can land in the installed library.
    """
    prior_configs_root = os.environ.get(meal_config.SCENARIO_CONFIGS_ROOT_ENV)
    prior_allowed = os.environ.get("LOOP_RISK_GUI_ALLOWED_COLLECTIONS")
    source_root = prior_configs_root or meal_config.scenario_configs_root()

    temp_root = str(tmp_path_factory.mktemp("trset13_lib"))
    temp_v2 = os.path.join(temp_root, "tidepool_risk_v2")
    os.makedirs(os.path.join(temp_v2, "loop_risk_v2_0", EMPTY_COLLECTION))
    os.symlink(
        os.path.join(source_root, "tidepool_risk_v2", "reusable"),
        os.path.join(temp_v2, "reusable"),
    )

    os.environ[meal_config.SCENARIO_CONFIGS_ROOT_ENV] = temp_root
    os.environ["LOOP_RISK_GUI_ALLOWED_COLLECTIONS"] = EMPTY_COLLECTION
    yield temp_root
    for variable, prior in (
        (meal_config.SCENARIO_CONFIGS_ROOT_ENV, prior_configs_root),
        ("LOOP_RISK_GUI_ALLOWED_COLLECTIONS", prior_allowed),
    ):
        if prior is None:
            os.environ.pop(variable, None)
        else:
            os.environ[variable] = prior


# ---------------------------------------------------------------------------
# Driving the app
# ---------------------------------------------------------------------------


def _short_term_app(hours):
    """An editor with the short-term option selected and `hours` entered."""
    at = AppTest.from_file("streamlit_app.py", default_timeout=RUN_TIMEOUT_SECONDS)
    at.run()
    at.radio(key="config_source").set_value(streamlit_app.SOURCE_CONFIGURE).run()
    at.radio(key="sim_duration_choice").set_value(
        streamlit_app.SHORT_TERM_DURATION_LABEL
    ).run()
    at.number_input(key="sim_duration_hours").set_value(hours).run()
    return at


def _preset_app(hours):
    """An editor with the preset option for `hours` selected."""
    at = AppTest.from_file("streamlit_app.py", default_timeout=RUN_TIMEOUT_SECONDS)
    at.run()
    at.radio(key="config_source").set_value(streamlit_app.SOURCE_CONFIGURE).run()
    at.radio(key="sim_duration_choice").set_value(DURATION_LABEL_BY_HOURS[hours]).run()
    return at


def _configure_and_generate(at, meal_time):
    """One meal and one bolus, then Generate configs."""
    at.time_input(key="pm_meal_time_0").set_value(meal_time)
    at.time_input(key="pm_bolus_time_0").set_value(BOLUS_TIME)
    at.number_input(key="pm_bolus_units_0").set_value(NO_LOOP_UNITS)
    at.run()

    generate = [button for button in at.button if button.label == "Generate configs"]
    assert len(generate) == 1, [button.label for button in at.button]
    generate[0].click().run()
    assert not at.exception
    assert not at.error, [error.value for error in at.error]
    return at


def _run_to_completion(at, timeout):
    run_buttons = [button for button in at.button if button.label == "Run Tool"]
    assert len(run_buttons) == 1, "generated configs must be runnable"
    run_buttons[0].click().run()

    thread = at.session_state["run_thread"]
    assert thread is not None, "Run Tool did not start a background thread"
    thread.join(timeout=timeout)
    assert not thread.is_alive(), f"run did not finish within {timeout}s"
    at.run()
    assert at.session_state["run_error"] is None, at.session_state["run_error"]
    return at


def _on_disk_configs(at):
    """The generated configs re-read from disk -- not the in-memory dicts."""
    config_dir = at.session_state["generated_config_dir"]
    risk_dir = os.path.join(config_dir, at.session_state["generated_risk_id"])
    read_back = {}
    for filename in os.listdir(risk_dir):
        with open(os.path.join(risk_dir, filename)) as handle:
            read_back[filename] = json.load(handle)
    return read_back


def _resolved_config(run_result, sim_id):
    """The fully-resolved config the run itself wrote for one sim."""
    path = os.path.join(run_result.save_dir, f"{sim_id}_override_config.json")
    with open(path) as handle:
        return json.load(handle)


def _trace_times(run_result, sim_id):
    """The timestamps of one completed sim's trace, as the simulator wrote them."""
    trace_paths = run_result.risk_dir_results[0].trace_paths
    path = next(
        paths[sim_id] for paths in trace_paths.values() if sim_id in paths
    )
    return pd.to_datetime(pd.read_csv(path, sep="\t")["time"])


def _stage_table(at):
    """The results pane's per-stage metrics table, found by its own columns."""
    tables = [df.value for df in at.dataframe if "Stage" in df.value.columns]
    assert len(tables) == 1, [list(df.value.columns) for df in at.dataframe]
    return tables[0]


def _captions(at):
    return [caption.value for caption in at.caption]


# ---------------------------------------------------------------------------
# The 2-hour run -- picker to trace, in the default suite
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def short_run(temp_library):
    """One real 2-hour run, configured and started through the app itself."""
    at = _short_term_app(SHORT_RUN_HOURS)
    _configure_and_generate(at, MEAL_TIME)
    return _run_to_completion(at, RUN_TIMEOUT_SECONDS)


def test_the_picked_duration_reaches_every_override_of_every_generated_file(short_run):
    configs = _on_disk_configs(short_run)
    assert len(configs) == len(meal_config.PROFILES)
    for filename, config in configs.items():
        durations = [override["duration_hours"] for override in config["override_config"]]
        assert durations == [SHORT_RUN_HOURS] * len(meal_config.STAGES), filename


def test_the_shortened_configs_still_validate_with_zero_errors(short_run):
    """No parser or schema change was made, so ConfigValidator must be unmoved."""
    result = validate_config_dir(
        short_run.session_state["generated_config_dir"],
        short_run.session_state["generated_risk_id"],
    )
    assert result.errors_by_file == {}, result.errors_by_file
    assert result.is_valid


def test_the_run_produced_an_assessment_for_all_four_profiles(short_run):
    run_result = short_run.session_state["run_result"]
    assert not run_result.cancelled
    assert len(run_result.risk_dir_results) == 1
    dir_result = run_result.risk_dir_results[0]
    assert dir_result.assessment_status == "ok", dir_result.assessment_detail
    assert dir_result.assessment.profile_count == len(meal_config.PROFILES)


def test_the_duration_survived_the_parser_into_the_config_the_run_resolved(short_run):
    """The key already exists in base, so resolve_override replaces it -- proven
    here on the merged config the run wrote, not on the generator's output."""
    run_result = short_run.session_state["run_result"]
    for profile in meal_config.PROFILES:
        for stage in meal_config.STAGES:
            resolved = _resolved_config(run_result, stage.sim_id_prefix + profile.token)
            assert resolved["duration_hours"] == SHORT_RUN_HOURS, (
                profile.display, stage.sim_id_prefix
            )


def test_the_simulation_actually_stopped_after_the_selected_duration(short_run):
    """The boundary that matters: the trace the simulator produced, not the JSON."""
    run_result = short_run.session_state["run_result"]
    start, _, _ = meal_config.simulation_window(SHORT_RUN_HOURS)
    expected_end = start + datetime.timedelta(hours=SHORT_RUN_HOURS)

    for profile in meal_config.PROFILES:
        times = _trace_times(run_result, f"pre-Loop_NoMitigations_t1_{profile.token}")
        assert abs(times.max() - expected_end) <= STEP, (profile.display, times.max())
        # And nowhere near the 8-hour default it would have run for before.
        default_end = start + datetime.timedelta(
            hours=meal_config.DURATION_OVERDELIVERY_HOURS
        )
        assert times.max() < default_end, profile.display


def test_the_results_pane_marks_the_sub_eight_hour_metrics_as_not_valid(short_run):
    table = _stage_table(short_run)
    for column in streamlit_app.INVALIDATED_METRIC_COLUMNS:
        assert (table[column] == streamlit_app.SUB_MINIMUM_DURATION_CELL).all(), column
    assert meal_config.SHORT_DURATION_CAVEAT in _captions(short_run)


def test_the_severity_model_and_its_own_outputs_were_left_alone(short_run):
    """AC 11 is presentation: the assessment still carries the computed values."""
    assessment = short_run.session_state["run_result"].risk_dir_results[0].assessment
    for stage_result in assessment.stages.values():
        assert stage_result.severity != streamlit_app.SUB_MINIMUM_DURATION_CELL
        assert stage_result.lbgi_value_avg != streamlit_app.SUB_MINIMUM_DURATION_CELL


# ---------------------------------------------------------------------------
# The presets, at the config boundary -- what is unproven for them is the number
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hours",
    [meal_config.DURATION_OVERDELIVERY_HOURS, meal_config.DURATION_UNDERDELIVERY_HOURS],
)
def test_a_preset_duration_reaches_every_override_and_still_validates(temp_library, hours):
    at = _configure_and_generate(_preset_app(hours), MEAL_TIME)

    for filename, config in _on_disk_configs(at).items():
        durations = [override["duration_hours"] for override in config["override_config"]]
        assert durations == [hours] * len(meal_config.STAGES), filename

    result = validate_config_dir(
        at.session_state["generated_config_dir"], at.session_state["generated_risk_id"]
    )
    assert result.errors_by_file == {}, result.errors_by_file


def test_an_entry_outside_a_shortened_window_stops_generation_naming_the_window(
    temp_library
):
    """AC 8, through the app: the entry is rejected, never moved into the window."""
    at = _short_term_app(SHORT_RUN_HOURS)
    at.time_input(key="pm_meal_time_0").set_value(datetime.time(15, 30))
    at.number_input(key="pm_bolus_units_0").set_value(NO_LOOP_UNITS)
    at.run()
    [b for b in at.button if b.label == "Generate configs"][0].click().run()

    assert not at.exception
    assert at.session_state["generated_configs"] is None
    errors = [error.value for error in at.error]
    assert any("15:30:00" in error and "12:00:00-14:00:00" in error for error in errors), errors
    assert not [b for b in at.button if b.label == "Run Tool"]


# ---------------------------------------------------------------------------
# The 24-hour run -- the midnight-crossing path, opt-in
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_full_day_run_crosses_midnight_and_keeps_its_late_entry(temp_library):
    """Roughly three times the 8-hour cost, which is why this is behind -m slow.

    Nothing cheaper proves it: the wrap defect this feature fixes was invisible at
    every duration that stays inside one calendar day.
    """
    hours = meal_config.DURATION_FULL_DAY_HOURS
    at = _configure_and_generate(_preset_app(hours), LATE_MEAL_TIME)

    start, end, _ = meal_config.simulation_window(hours)
    assert end.date() > start.date(), "precondition: this window must cross midnight"

    # The late entry is authored on the start day and kept there, not clamped.
    for filename, config in _on_disk_configs(at).items():
        entry = config["override_config"][0]["patient"]["patient_model"]["carb_entries"][0]
        assert entry["start_time"] == meal_config.entry_token(
            datetime.datetime.combine(start.date(), LATE_MEAL_TIME)
        ), filename

    _run_to_completion(at, SLOW_RUN_TIMEOUT_SECONDS)
    run_result = at.session_state["run_result"]
    assert run_result.risk_dir_results[0].assessment_status == "ok"

    for profile in meal_config.PROFILES:
        sim_id = f"pre-Loop_NoMitigations_t1_{profile.token}"
        assert _resolved_config(run_result, sim_id)["duration_hours"] == hours
        times = _trace_times(run_result, sim_id)
        assert abs(times.max() - end) <= STEP, (profile.display, times.max())
        assert (times.dt.date > start.date()).any(), (
            f"{profile.display}: the trace never reached the day after the start"
        )

    # A 24-hour run is long enough for the metrics, so nothing is marked.
    assert meal_config.SHORT_DURATION_CAVEAT not in _captions(at)
    assert (
        _stage_table(at)["LBGI"] != streamlit_app.SUB_MINIMUM_DURATION_CELL
    ).all()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
