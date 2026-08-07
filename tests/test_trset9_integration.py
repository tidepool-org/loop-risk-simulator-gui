"""End-to-end integration test for TRSET-9 configurable meal entries (Feature gate).

Per the approved plan: no mocks anywhere in the exercised path, and every assertion
is made at a boundary of the full system -- the generated JSON on disk, the resolved
configs the run itself wrote, and the export zip -- never on the functions that
produced them.

    the app's own meal/bolus editor, driven through AppTest
        -> meal_config.generate_configs / write_config_library   (real JSON on disk)
        -> gui_runner.validate_config_dir                        (real ConfigValidator)
        -> gui_runner.run_risk_assessment                        (real ScenarioParserV2,
                                                                  real Swift simulations)
        -> streamlit_app's export control                        (real RTFs, charts, zip)

The whole module rides on ONE real run, started from the app exactly the way a user
starts it, because a Feature gate that stops short of the real simulator proves
nothing about schema conformance. That run is four config files x three stages; it
takes roughly a minute.

The temp library is the established one: real layout from the ``tidepool_risk_v2``
level down, the real ``reusable/`` symlinked so pointer resolution behaves as it
does for a real collection, the app's own env seams, and zero writes into the
installed library (asserted in test_nothing_was_written_into_the_real_library).
"""

import datetime
import io
import json
import os
import zipfile

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from tidepool_data_science_simulator.projects.risk.gui_runner import (  # noqa: E402
    METADATA_FILENAME,
    STAGE_ORDER,
    classify_sim_id,
    validate_config_dir,
)

import export_bundle  # noqa: E402
import meal_config  # noqa: E402
import streamlit_app  # noqa: E402


# The spec the app is driven to build. Deliberately exercises, in one run:
# multiplier mode above 1.0, two meals of which only one sets an absorption
# duration, and an accept_recommendation bolus carrying the numeric units the No
# Loop stage has to use instead.
MULTIPLIER = 2.0
SECOND_MEAL_MULTIPLIER = 0.25
SECOND_MEAL_DURATION = 45
MEAL_TIMES = (datetime.time(12, 0), datetime.time(15, 30))
BOLUS_TIME = datetime.time(12, 0)
NO_LOOP_UNITS = 3.3

# baseline x MULTIPLIER, per profile -- what AC 2's worked example calls for.
EXPECTED_FIRST_MEAL_GRAMS = {"median": 62.0, "resistant": 66.0, "adolescent": 120.0, "sensitive": 50.0}
EXPECTED_SECOND_MEAL_GRAMS = {
    "median": 7.75, "resistant": 8.25, "adolescent": 15.0, "sensitive": 6.25,
}

RUN_TIMEOUT_SECONDS = 600


EMPTY_COLLECTION = "_pytest_trset9_empty_collection"


@pytest.fixture(scope="module")
def temp_library(tmp_path_factory):
    """Point the app at a temp scenario library holding one EMPTY collection.

    The collection is empty on purpose: this feature must never write a config into
    the library, and an empty allowlisted collection makes that directly assertable
    (test_nothing_was_written_into_the_real_library) instead of merely likely. The
    real ``reusable/`` is symlinked in, so the baselines the generator reads and the
    pointers the run resolves are the real ones.
    """
    prior_configs_root = os.environ.get(meal_config.SCENARIO_CONFIGS_ROOT_ENV)
    prior_allowed = os.environ.get("LOOP_RISK_GUI_ALLOWED_COLLECTIONS")
    source_root = (
        prior_configs_root
        if prior_configs_root
        else meal_config.scenario_configs_root()
    )
    temp_root = str(tmp_path_factory.mktemp("trset9_lib"))
    temp_v2 = os.path.join(temp_root, "tidepool_risk_v2")
    os.makedirs(os.path.join(temp_v2, "loop_risk_v2_0", EMPTY_COLLECTION))
    # Symlink reusable/ rather than copy it (18MB), the way the other suites do.
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


def _configure_and_generate(at):
    """Drive the app's editor to the spec above and click Generate configs."""
    at.radio(key="config_source").set_value(streamlit_app.SOURCE_CONFIGURE).run()
    at.radio(key="meal_mode").set_value("Multiplier of standard").run()
    at.number_input(key="pm_meal_count").set_value(2).run()

    at.number_input(key="pm_meal_multiplier_0").set_value(MULTIPLIER)
    at.time_input(key="pm_meal_time_0").set_value(MEAL_TIMES[0])
    at.number_input(key="pm_meal_multiplier_1").set_value(SECOND_MEAL_MULTIPLIER)
    at.time_input(key="pm_meal_time_1").set_value(MEAL_TIMES[1])
    at.number_input(key="pm_meal_duration_1").set_value(SECOND_MEAL_DURATION)
    at.time_input(key="pm_bolus_time_0").set_value(BOLUS_TIME)
    at.number_input(key="pm_bolus_units_0").set_value(NO_LOOP_UNITS)
    at.run()

    generate = [button for button in at.button if button.label == "Generate configs"]
    assert len(generate) == 1, [button.label for button in at.button]
    generate[0].click().run()
    assert not at.exception
    assert not at.error, [error.value for error in at.error]


@pytest.fixture(scope="module")
def app_run(temp_library):
    """Configure, generate, run and export -- all through the real app.

    Yields the AppTest after the export has been built, so every test below reads
    from one real run.
    """
    at = AppTest.from_file("streamlit_app.py", default_timeout=RUN_TIMEOUT_SECONDS)
    at.run()
    assert not at.exception

    _configure_and_generate(at)

    run_buttons = [button for button in at.button if button.label == "Run Tool"]
    assert len(run_buttons) == 1, "generated configs must be runnable"
    run_buttons[0].click().run()

    thread = at.session_state["run_thread"]
    assert thread is not None, "Run Tool did not start a background thread"
    thread.join(timeout=RUN_TIMEOUT_SECONDS)
    assert not thread.is_alive(), f"run did not finish within {RUN_TIMEOUT_SECONDS}s"
    at.run()
    assert at.session_state["run_error"] is None, at.session_state["run_error"]

    export_buttons = [button for button in at.button if button.label == "Export results"]
    assert len(export_buttons) == 1, "a completed run must offer the export control"
    export_buttons[0].click().run()
    assert not at.exception
    assert not at.error, [error.value for error in at.error]
    return at


@pytest.fixture(scope="module")
def generated(app_run):
    """``(config_dir, risk_id, {filename: config})`` as the app generated them."""
    return (
        app_run.session_state["generated_config_dir"],
        app_run.session_state["generated_risk_id"],
        app_run.session_state["generated_configs"],
    )


@pytest.fixture(scope="module")
def on_disk_configs(generated):
    """The generated configs re-read from disk -- not the in-memory dicts."""
    config_dir, generated_risk_id, configs = generated
    risk_dir = os.path.join(config_dir, generated_risk_id)
    read_back = {}
    for filename in os.listdir(risk_dir):
        with open(os.path.join(risk_dir, filename)) as handle:
            read_back[filename] = json.load(handle)
    return read_back


@pytest.fixture(scope="module")
def run_result(app_run):
    return app_run.session_state["run_result"]


@pytest.fixture(scope="module")
def export_root(run_result):
    return export_bundle.export_root_name(run_result.save_dir)


@pytest.fixture(scope="module")
def archived_names(app_run):
    with zipfile.ZipFile(app_run.session_state["export_zip_path"]) as archive:
        return archive.namelist()


def _profile_of(filename):
    """The profile token a generated filename belongs to."""
    for profile in meal_config.PROFILES:
        if f"_{profile.display}_Profile.json" in filename:
            return profile
    raise AssertionError(f"unrecognized generated filename: {filename}")


# ---------------------------------------------------------------------------
# 1-2: the four files, their names and their identity
# ---------------------------------------------------------------------------

def test_one_config_file_per_t1_profile_is_written(on_disk_configs, generated):
    _, generated_risk_id, _ = generated
    assert sorted(on_disk_configs) == sorted(
        f"Simulation-Configuration-{generated_risk_id}_{profile.display}_Profile.json"
        for profile in meal_config.PROFILES
    )


def test_risk_id_is_the_generation_timestamp_and_matches_the_filename_token(
    on_disk_configs, generated
):
    _, generated_risk_id, _ = generated
    assert generated_risk_id.startswith("TLR-")
    # TLR-YYYYMMDD-HHMMSS -- parses as a real timestamp, not just a shaped string.
    datetime.datetime.strptime(generated_risk_id[len("TLR-"):], "%Y%m%d-%H%M%S")

    for filename, config in on_disk_configs.items():
        assert config["metadata"]["risk_id"] == generated_risk_id
        assert generated_risk_id in filename


# ---------------------------------------------------------------------------
# 3-4: the meal values and the absorption duration
# ---------------------------------------------------------------------------

def test_multiplier_mode_scaled_each_profiles_own_baseline(on_disk_configs):
    for filename, config in on_disk_configs.items():
        token = _profile_of(filename).token
        entries = config["override_config"][0]["patient"]["patient_model"]["carb_entries"]
        assert [entry["value"] for entry in entries] == [
            EXPECTED_FIRST_MEAL_GRAMS[token],
            EXPECTED_SECOND_MEAL_GRAMS[token],
        ], filename


def test_the_meal_without_a_duration_omits_the_key_and_the_other_carries_it(on_disk_configs):
    for filename, config in on_disk_configs.items():
        first, second = config["override_config"][0]["patient"]["patient_model"]["carb_entries"]
        assert "duration" not in first, filename
        assert second["duration"] == SECOND_MEAL_DURATION, filename


def test_the_parser_gives_the_duration_less_meal_its_own_default(on_disk_configs, generated):
    """The omitted key must resolve to the parser's 180 minutes, in the real parser."""
    from tidepool_data_science_simulator.makedata.scenario_json_parser_v2 import ScenarioParserV2

    config_dir, generated_risk_id, _ = generated
    filename = next(iter(sorted(on_disk_configs)))
    parser = ScenarioParserV2(
        path_to_json_config=os.path.join(config_dir, generated_risk_id, filename)
    )
    timeline = parser.carb_entries_to_timeline(
        on_disk_configs[filename]["override_config"][0]["patient"]["patient_model"]["carb_entries"]
    )
    durations = [timeline.events[when].duration_minutes for when in sorted(timeline.events)]
    assert durations == [meal_config.DEFAULT_CARB_DURATION_MINUTES, SECOND_MEAL_DURATION]


# ---------------------------------------------------------------------------
# 5: schema conformance (AC 8, first half)
# ---------------------------------------------------------------------------

def test_generated_configs_validate_with_zero_errors(generated):
    config_dir, generated_risk_id, _ = generated
    result = validate_config_dir(config_dir, generated_risk_id)
    assert result.errors_by_file == {}, result.errors_by_file
    assert result.is_valid


# ---------------------------------------------------------------------------
# 6: a real run produces results (AC 8, second half)
# ---------------------------------------------------------------------------

def test_the_real_run_produced_an_assessment_for_all_four_profiles(run_result, generated):
    _, generated_risk_id, _ = generated
    assert not run_result.cancelled
    assert len(run_result.risk_dir_results) == 1
    dir_result = run_result.risk_dir_results[0]
    assert dir_result.risk_dir_name == generated_risk_id
    assert dir_result.assessment_status == "ok", dir_result.assessment_detail
    assert dir_result.assessment.profile_count == len(meal_config.PROFILES)


def test_every_stage_ran_for_every_profile(run_result):
    dir_result = run_result.risk_dir_results[0]
    assert sorted(dir_result.assessment.stages) == sorted(STAGE_ORDER)

    stages_by_profile = {
        scenario: sorted(classify_sim_id(sim_id) for sim_id in sims)
        for scenario, sims in dir_result.trace_paths.items()
    }
    assert len(stages_by_profile) == len(meal_config.PROFILES)
    for scenario, stages in stages_by_profile.items():
        assert stages == sorted(STAGE_ORDER), scenario


# ---------------------------------------------------------------------------
# 7-8: what actually reached the simulations
# ---------------------------------------------------------------------------

def _resolved_config(run_result, sim_id):
    """The fully-resolved config the run itself wrote for one sim."""
    path = os.path.join(run_result.save_dir, f"{sim_id}_override_config.json")
    with open(path) as handle:
        return json.load(handle)


def test_the_configured_meals_are_the_ones_the_simulations_ran(run_result):
    for profile in meal_config.PROFILES:
        resolved = _resolved_config(run_result, f"pre-Loop_NoMitigations_t1_{profile.token}")
        entries = resolved["patient"]["patient_model"]["carb_entries"]
        assert [entry["value"] for entry in entries] == [
            EXPECTED_FIRST_MEAL_GRAMS[profile.token],
            EXPECTED_SECOND_MEAL_GRAMS[profile.token],
        ], profile.display


def test_the_sentinel_reached_the_patient_model_and_never_the_pump(run_result):
    """On the pump timeline it would be dumped into the Loop input JSON verbatim."""
    for stage_prefix in ("pre-Loop_NoMitigations_t1_", "post-Loop_WithMitigations_t1_"):
        resolved = _resolved_config(run_result, f"{stage_prefix}median")
        patient_boluses = resolved["patient"]["patient_model"]["bolus_entries"]
        assert [entry["value"] for entry in patient_boluses] == [
            meal_config.ACCEPT_RECOMMENDATION
        ]
        assert resolved["patient"]["pump"]["bolus_entries"] == []


def test_the_no_loop_stage_ran_with_the_numeric_bolus_not_the_placeholder(run_result):
    """With controller: null nothing resolves the sentinel, so it must not be there."""
    for profile in meal_config.PROFILES:
        resolved = _resolved_config(run_result, f"pre-noLoop_t1_{profile.token}")
        assert resolved["controller"] is None
        for timeline in ("patient_model", "pump"):
            values = [
                entry["value"] for entry in resolved["patient"][timeline]["bolus_entries"]
            ]
            assert values == [NO_LOOP_UNITS], (profile.display, timeline)


# ---------------------------------------------------------------------------
# 9: aligned timelines (the toggle's default, which this run used)
# ---------------------------------------------------------------------------

def test_aligned_mode_wrote_identical_carb_lists_to_both_timelines(on_disk_configs):
    for filename, config in on_disk_configs.items():
        for override in config["override_config"]:
            patient = override["patient"]["patient_model"]["carb_entries"]
            pump = override["patient"]["pump"]["carb_entries"]
            assert patient == pump, (filename, override["sim_id"])


# ---------------------------------------------------------------------------
# 10: the export
# ---------------------------------------------------------------------------

def test_the_export_carries_the_generated_configs(app_run, archived_names, generated, export_root):
    _, generated_risk_id, _ = generated
    config_names = sorted(
        name for name in archived_names
        if name.startswith(f"{export_root}/{export_bundle.GENERATED_CONFIGS_DIR_NAME}/")
    )
    assert len(config_names) == len(meal_config.PROFILES), config_names
    assert all(generated_risk_id in name for name in config_names)


def test_the_exported_configs_are_byte_identical_to_the_ones_that_ran(
    app_run, generated, export_root
):
    config_dir, generated_risk_id, _ = generated
    risk_dir = os.path.join(config_dir, generated_risk_id)
    with zipfile.ZipFile(app_run.session_state["export_zip_path"]) as archive:
        for filename in os.listdir(risk_dir):
            with open(os.path.join(risk_dir, filename), "rb") as handle:
                on_disk = handle.read()
            archived = archive.read(
                f"{export_root}/{export_bundle.GENERATED_CONFIGS_DIR_NAME}/{filename}"
            )
            assert archived == on_disk, filename


def test_the_export_still_carries_the_runs_own_outputs(archived_names, export_root, generated):
    _, generated_risk_id, _ = generated
    assert f"{export_root}/{METADATA_FILENAME}" in archived_names

    tlr_prefix = f"{export_root}/{generated_risk_id}/"
    in_tlr_dir = [name[len(tlr_prefix):] for name in archived_names if name.startswith(tlr_prefix)]
    assert [name for name in in_tlr_dir if name.endswith(".rtf")], in_tlr_dir
    assert [name for name in in_tlr_dir if name.endswith(".tsv")], in_tlr_dir
    assert [
        name for name in in_tlr_dir
        if name.startswith("summary_results_") and name.endswith(".csv")
    ], in_tlr_dir

    charts = [
        name for name in archived_names
        if name.startswith(f"{export_root}/{export_bundle.CHARTS_DIR_NAME}/")
    ]
    # One chart per profile x stage -- every stage this config defines produced one.
    assert len(charts) == len(meal_config.PROFILES) * len(STAGE_ORDER), charts


def test_everything_is_nested_under_one_top_folder(archived_names, export_root):
    assert all(name.startswith(f"{export_root}/") for name in archived_names)


# ---------------------------------------------------------------------------
# 11-12: the app's own rendering, and the library left untouched
# ---------------------------------------------------------------------------

def test_the_app_offers_the_generated_configs_and_the_run_export_for_download(app_run):
    labels = [button.label for button in app_run.download_button]
    assert "Download configs (.zip)" in labels
    assert "Download export (.zip)" in labels

    with zipfile.ZipFile(app_run.session_state["export_zip_path"]) as archive:
        assert archive.testzip() is None, "corrupt archive"


def test_the_downloadable_config_zip_nests_under_one_folder(generated):
    _, generated_risk_id, configs = generated
    payload = streamlit_app.generated_configs_zip(configs, generated_risk_id)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
    assert len(names) == len(meal_config.PROFILES)
    assert all(name.startswith(f"{generated_risk_id}/") for name in names)


def test_the_results_pane_renders_a_chart_row_per_profile(app_run):
    """Every generated profile x stage reaches the grid, with no 'No data' holes."""
    markdown = [element.value for element in app_run.markdown]
    for profile in meal_config.PROFILES:
        assert f"**{profile.display} profile**" in markdown, profile.display
    assert not app_run.info or all(
        element.value != "No data" for element in app_run.info
    ), [element.value for element in app_run.info]


def test_nothing_was_written_into_the_real_library(app_run, temp_library):
    """The generated configs live in their own temp root, not the scenario library."""
    generated_root = app_run.session_state["generated_temp_dir"]
    assert generated_root is not None
    assert not generated_root.startswith(temp_library)

    collection = os.path.join(
        temp_library, "tidepool_risk_v2", "loop_risk_v2_0", EMPTY_COLLECTION
    )
    assert os.listdir(collection) == [], os.listdir(collection)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
