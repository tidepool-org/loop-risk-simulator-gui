"""End-to-end integration test for TRSET-15 controller settings (Feature gate).

Per the approved five-phase plan: no mocks anywhere in the exercised path, and every
assertion is made at a boundary of the full system -- the generated JSON on disk, the
app's rendered output, the session state -- never on the function that produced it.

    the app's own Controller settings / Dosing strategy radios, driven through AppTest
        -> meal_config.MealConfigSpec / generate_configs / write_config_library
        -> the real config files on disk    (base_config, controller.settings, metadata)
        -> ScenarioParserV2                 (they parse, and they run)
        -> the generated-configs summary    (echoed back FROM the JSON)

The temp library is the established one (TRSET-9, TRSET-13, TRSET-36): real layout from
the ``tidepool_risk_v2`` level down, the real ``reusable/`` symlinked so the baselines
the generator reads and the pointers it writes are real, the app's own env seams, and
zero writes into the installed library.

Deliberately NOT asserted here, per the plan: anything about Loop 1.x *results*. The
flag TRSET-49 corrects changes what 1.x runs produce, so a results assertion would be a
tripwire on a sibling ticket. This suite proves the selected group reaches the config.
"""

import datetime
import json
import os

import pytest

pytest.importorskip("streamlit")
# The shared AppTest factory (tests/conftest.py), which acknowledges the TRSET-34
# start-page gate so this suite drives the tool directly, as every suite before it does.
from conftest import make_app_test  # noqa: E402

import meal_config  # noqa: E402
import streamlit_app  # noqa: E402

from tidepool_data_science_simulator.projects.risk.gui_runner import (  # noqa: E402
    run_risk_assessment,
    validate_config_dir,
)
from tidepool_data_science_simulator.makedata.scenario_json_parser_v2 import (  # noqa: E402
    ScenarioParserV2,
)


# The base configs run 8/15/2019 12:00 for 8 hours; entries are datetimes, not times of
# day, so the generator's window check has a real day to place them on.
NOON = datetime.datetime.combine(datetime.date(2019, 8, 15), datetime.time(12, 0))
MEAL_TIME = datetime.time(13, 0)

# Phase 2 compares against a fixture captured on unmodified main. Its spec is fixed --
# standard mode, one meal at 13:00, 8 hours, this risk id, no description -- so the
# comparison is meaningful; see tests/test_data/trset15_pre_change/README.md.
BASELINE_RISK_ID = "TLR-20260904-120000"
BASELINE_DURATION_HOURS = meal_config.DURATION_OVERDELIVERY_HOURS
PRE_CHANGE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "test_data", "trset15_pre_change"
)

# The two metadata keys this ticket adds. Phase 2 removes exactly these before
# comparing, and asserts they are the ONLY difference in the key set (AC 4).
NEW_METADATA_KEYS = {"controller_settings_group", "dosing_strategy"}

GROUP_2X, GROUP_1X = meal_config.SETTINGS_GROUPS

# A short run for the one end-to-end combination the plan budgets for (Phase 5).
RUN_HOURS = 2.0
RUN_TIMEOUT_SECONDS = 900

EMPTY_COLLECTION = "_pytest_trset15_empty_collection"


@pytest.fixture(scope="module")
def temp_library(tmp_path_factory):
    """Point the app at a temp scenario library holding one EMPTY collection."""
    prior_configs_root = os.environ.get(meal_config.SCENARIO_CONFIGS_ROOT_ENV)
    prior_allowed = os.environ.get("LOOP_RISK_GUI_ALLOWED_COLLECTIONS")
    source_root = prior_configs_root or meal_config.scenario_configs_root()

    temp_root = str(tmp_path_factory.mktemp("trset15_lib"))
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


def _editor_app(timeout=60):
    """The app with the configure-meals source selected and one standard meal.

    The bolus row is dropped rather than filled: a bolus needs a dose, and this feature
    has nothing to do with one. One meal is enough for a valid spec.
    """
    at = make_app_test(default_timeout=timeout)
    at.run()
    at.radio(key="config_source").set_value(streamlit_app.SOURCE_CONFIGURE).run()
    at.number_input(key="pm_bolus_count").set_value(0).run()
    at.time_input(key="pm_meal_time_0").set_value(MEAL_TIME).run()
    return at


def _select(at, group=None, dosing=None):
    """Set either radio through the rendered widget, as a user would."""
    if group is not None:
        at.radio(key=streamlit_app.CONTROLLER_SETTINGS_KEY).set_value(group.display).run()
    if dosing is not None:
        at.radio(key=streamlit_app.DOSING_STRATEGY_KEY).set_value(dosing).run()
    return at


def _click_generate(at):
    generate = [button for button in at.button if button.label == "Generate configs"]
    assert len(generate) == 1, [button.label for button in at.button]
    generate[0].click().run()
    assert not at.exception
    assert not at.error, [error.value for error in at.error]
    return at


def _risk_dir(at):
    return os.path.join(
        at.session_state["generated_config_dir"], at.session_state["generated_risk_id"]
    )


def _on_disk_configs(at):
    """The generated configs re-read from disk -- not the in-memory dicts.

    Globbed by extension rather than by walking the directory: a .DS_Store lands here
    on macOS and is gitignored, but a directory walk would try to json.load it.
    """
    read_back = {}
    for filename in sorted(os.listdir(_risk_dir(at))):
        if not filename.endswith(".json"):
            continue
        with open(os.path.join(_risk_dir(at), filename)) as handle:
            read_back[filename] = json.load(handle)
    return read_back


def _captions(at):
    return [caption.value for caption in at.caption]


def _generated_for(group, dosing):
    """One generated set, authored through the app with this selection."""
    at = _select(_editor_app(), group=group, dosing=dosing)
    return _click_generate(at)


@pytest.fixture(scope="module")
def run_2x_autobolus(temp_library):
    return _generated_for(GROUP_2X, meal_config.DOSING_AUTOBOLUS)


@pytest.fixture(scope="module")
def run_2x_temp_basal(temp_library):
    return _generated_for(GROUP_2X, meal_config.DOSING_TEMP_BASAL)


@pytest.fixture(scope="module")
def run_1x(temp_library):
    # Dosing is not passed: selecting 1.x removes Autobolus from the options, so
    # there is nothing to choose. That coupling is what Phase 3 asserts.
    return _generated_for(GROUP_1X, None)


def _stage(config, sim_id_prefix):
    """One stage of a generated config, found by its sim_id prefix."""
    matches = [
        override
        for override in config["override_config"]
        if override["sim_id"].startswith(sim_id_prefix)
    ]
    assert len(matches) == 1, [o["sim_id"] for o in config["override_config"]]
    return matches[0]


PRE_LOOP, NO_LOOP, POST_LOOP = (stage.sim_id_prefix for stage in meal_config.STAGES)


# ---------------------------------------------------------------------------
# Phase 1: the generated config, per combination (AC 3, 5, 6, 7, 8)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile", meal_config.PROFILES, ids=lambda p: p.token)
def test_2x_autobolus_writes_the_2x_base_and_no_override(run_2x_autobolus, profile):
    """The default path: 2.x base, and neither Loop stage gains an override."""
    configs = _on_disk_configs(run_2x_autobolus)
    config = configs[meal_config.config_filename(
        run_2x_autobolus.session_state["generated_risk_id"], profile
    )]
    assert config["base_config"] == f"reusable.simulations.base_{profile.token}_2_0_v1"
    assert "controller" not in _stage(config, PRE_LOOP)
    assert _stage(config, POST_LOOP)["controller"]["settings"] == (
        f"reusable.mitigations.guardrails.controller_settings_{profile.token}_swift"
    )


@pytest.mark.parametrize("profile", meal_config.PROFILES, ids=lambda p: p.token)
def test_2x_temp_basal_overrides_both_loop_stages(run_2x_temp_basal, profile):
    """AC 5. The inline dict is compared against the guardrails file READ AT TEST TIME.

    Hardcoding 75 / 1.75 here would keep this passing after the implementation stopped
    reading the file, which is the one thing AC 5 exists to forbid.
    """
    configs = _on_disk_configs(run_2x_temp_basal)
    config = configs[meal_config.config_filename(
        run_2x_temp_basal.session_state["generated_risk_id"], profile
    )]
    assert config["base_config"] == f"reusable.simulations.base_{profile.token}_2_0_v1"

    pre = _stage(config, PRE_LOOP)
    assert pre["controller"]["settings"] == {"partial_application_factor": 0.0}

    expected = dict(meal_config.guardrails_settings(profile))
    expected["partial_application_factor"] = 0.0
    post_settings = _stage(config, POST_LOOP)["controller"]["settings"]
    assert isinstance(post_settings, dict), "post-mitigation settings must be inlined"
    assert post_settings == expected


@pytest.mark.parametrize("profile", meal_config.PROFILES, ids=lambda p: p.token)
def test_1x_writes_the_1x_base_and_no_override(run_1x, profile):
    """AC 3 and AC 6: the 1.x base for every profile, and no factor override anywhere.

    1dotX.json already carries partial_application_factor 0.0, so an override would
    restate what the base config resolves to.
    """
    configs = _on_disk_configs(run_1x)
    config = configs[meal_config.config_filename(
        run_1x.session_state["generated_risk_id"], profile
    )]
    assert config["base_config"] == f"reusable.simulations.base_{profile.token}_1dotx"
    assert "controller" not in _stage(config, PRE_LOOP)
    assert _stage(config, POST_LOOP)["controller"]["settings"] == (
        f"reusable.mitigations.guardrails.controller_settings_{profile.token}_swift"
    )
    assert "partial_application_factor" not in json.dumps(config)


def _contains_key(node, key):
    """Whether `key` appears as a dict key anywhere in a nested structure."""
    if isinstance(node, dict):
        return key in node or any(_contains_key(value, key) for value in node.values())
    if isinstance(node, list):
        return any(_contains_key(item, key) for item in node)
    return False


@pytest.mark.parametrize(
    "fixture_name", ["run_2x_autobolus", "run_2x_temp_basal", "run_1x"]
)
def test_maximum_autobolus_is_never_written_anywhere(fixture_name, request):
    """AC 7, asserted as recursive absence across the whole file.

    Checking only controller.settings would miss the key being written somewhere else,
    and writing it at all would fail the run with "Only applied N of M overriding
    values" -- resolve_override applies only keys already present in the resolved base.
    """
    at = request.getfixturevalue(fixture_name)
    for filename, config in _on_disk_configs(at).items():
        assert not _contains_key(config, "maximum_autobolus"), filename
        assert not _contains_key(config, "minimum_autobolus"), filename


@pytest.mark.parametrize(
    "fixture_name", ["run_2x_autobolus", "run_2x_temp_basal", "run_1x"]
)
def test_the_no_loop_stage_is_untouched_by_every_selection(fixture_name, request):
    """AC 8: no controller to configure, under any combination."""
    at = request.getfixturevalue(fixture_name)
    for filename, config in _on_disk_configs(at).items():
        assert _stage(config, NO_LOOP)["controller"] is None, filename


# ---------------------------------------------------------------------------
# Phase 2: the no-delta baseline (AC 4)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def baseline_configs(temp_library):
    """The default selection generated against the fixture's exact spec.

    Built through meal_config rather than the app because the fixture's risk id is
    fixed -- a runtime id would differ on every run and land in metadata.simulation_id,
    making the comparison meaningless.
    """
    spec = meal_config.MealConfigSpec.aligned(
        meal_config.MODE_STANDARD,
        meal_config.EntrySet(meals=[meal_config.MealEntry(
            datetime.datetime.combine(NOON.date(), MEAL_TIME)
        )]),
        duration_hours=BASELINE_DURATION_HOURS,
    )
    return meal_config.generate_configs(spec, BASELINE_RISK_ID)


def _pre_change_configs():
    configs = {}
    for filename in sorted(os.listdir(PRE_CHANGE_DIR)):
        if not filename.endswith(".json"):
            continue
        with open(os.path.join(PRE_CHANGE_DIR, filename)) as handle:
            configs[filename] = json.load(handle)
    return configs


def test_the_pre_change_fixture_is_present_and_complete():
    """Guard: a silently-missing fixture would make Phase 2 vacuously pass."""
    configs = _pre_change_configs()
    assert len(configs) == len(meal_config.PROFILES), sorted(configs)


def test_the_default_selection_still_produces_the_pre_change_output(baseline_configs):
    """AC 4. If this fails, the DEFAULT config-generation path moved.

    That is a finding to investigate, not a fixture to regenerate -- the baseline
    cannot be reconstructed once meal_config.py has changed.
    """
    pre_change = _pre_change_configs()
    assert sorted(baseline_configs) == sorted(pre_change)
    for filename, expected in pre_change.items():
        produced = json.loads(json.dumps(baseline_configs[filename]))
        for key in NEW_METADATA_KEYS:
            produced["metadata"].pop(key, None)
        assert produced == expected, filename


def test_the_only_difference_from_the_baseline_is_the_two_new_metadata_keys(
    baseline_configs,
):
    """AC 4's other half: the addition is exactly two keys, and nothing else moved."""
    pre_change = _pre_change_configs()
    for filename, expected in pre_change.items():
        produced = baseline_configs[filename]
        assert set(produced) == set(expected), filename
        added = set(produced["metadata"]) - set(expected["metadata"])
        removed = set(expected["metadata"]) - set(produced["metadata"])
        assert added == NEW_METADATA_KEYS, (filename, added)
        assert removed == set(), (filename, removed)


# ---------------------------------------------------------------------------
# Phase 3: the coupling and the widget wiring (AC 1, 2, 12, 13)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fresh_editor(temp_library):
    return _editor_app()


def test_both_radios_render_with_their_labels_options_and_defaults(fresh_editor):
    """AC 1: both groups, the ticket's option order, and the ticket's defaults."""
    settings = fresh_editor.radio(key=streamlit_app.CONTROLLER_SETTINGS_KEY)
    assert settings.label == streamlit_app.CONTROLLER_SETTINGS_LABEL
    assert settings.options == [group.display for group in meal_config.SETTINGS_GROUPS]
    assert settings.value == GROUP_2X.display

    dosing = fresh_editor.radio(key=streamlit_app.DOSING_STRATEGY_KEY)
    assert dosing.label == streamlit_app.DOSING_STRATEGY_LABEL
    assert dosing.options == list(meal_config.DOSING_STRATEGIES)
    assert dosing.value == meal_config.DOSING_AUTOBOLUS


def test_both_radios_carry_a_full_label(fresh_editor):
    """AC 13 (WCAG 1.3/2.1), consistent with test_accessibility.py."""
    for key in (streamlit_app.CONTROLLER_SETTINGS_KEY, streamlit_app.DOSING_STRATEGY_KEY):
        label = fresh_editor.radio(key=key).label
        assert label and label.strip(), key


def test_the_info_text_for_both_groups_is_rendered(fresh_editor):
    """AC 1: the info text, asserted on the rendered element."""
    rendered = [info.value for info in fresh_editor.info]
    assert streamlit_app.CONTROLLER_SETTINGS_INFO in rendered
    assert streamlit_app.DOSING_STRATEGY_INFO in rendered


def test_every_option_carries_its_own_explanatory_text(fresh_editor):
    """AC 1: per-option text, on the rendered element rather than in the source."""
    settings = fresh_editor.radio(key=streamlit_app.CONTROLLER_SETTINGS_KEY)
    assert settings.captions == [
        streamlit_app.CONTROLLER_SETTINGS_HELP[group.display]
        for group in meal_config.SETTINGS_GROUPS
    ]
    dosing = fresh_editor.radio(key=streamlit_app.DOSING_STRATEGY_KEY)
    expected = streamlit_app._dosing_strategy_help()
    assert dosing.captions == [expected[option] for option in dosing.options]


def test_the_autobolus_percentage_is_read_from_the_library_not_hardcoded(fresh_editor):
    """AC 12: the tooltip's percentage matches 2_0_v1.json, read at test time."""
    with open(os.path.join(
        meal_config.scenario_configs_root(), "tidepool_risk_v2", "reusable",
        "loop_settings", "2_0_v1.json",
    )) as handle:
        factor = json.load(handle)["partial_application_factor"]

    caption = fresh_editor.radio(key=streamlit_app.DOSING_STRATEGY_KEY).captions[0]
    assert f"{factor * 100:g}%" in caption, caption


def test_selecting_loop_1x_makes_autobolus_unselectable_with_a_caption(temp_library):
    """AC 2, asserted through the rendered element rather than a variable."""
    at = _select(_editor_app(), group=GROUP_1X)
    dosing = at.radio(key=streamlit_app.DOSING_STRATEGY_KEY)
    assert meal_config.DOSING_AUTOBOLUS not in dosing.options
    assert dosing.options == [meal_config.DOSING_TEMP_BASAL]
    assert meal_config.LOOP_1X_TEMP_BASAL_ONLY_NOTE in _captions(at)


def test_switching_to_1x_while_autobolus_is_selected_resolves_to_temp_basal(temp_library):
    """The transition a naive implementation gets wrong.

    If the coupling were applied after the dosing radio rendered, the stale Autobolus
    selection would survive one cycle -- so the config generated from that render would
    disagree with what the UI had just been told.
    """
    at = _select(_editor_app(), group=GROUP_2X, dosing=meal_config.DOSING_AUTOBOLUS)
    assert at.radio(key=streamlit_app.DOSING_STRATEGY_KEY).value == (
        meal_config.DOSING_AUTOBOLUS
    ), "precondition: Autobolus is selected before the switch"

    at = _select(at, group=GROUP_1X)
    assert not at.exception
    assert at.radio(key=streamlit_app.DOSING_STRATEGY_KEY).value == (
        meal_config.DOSING_TEMP_BASAL
    )

    at = _click_generate(at)
    for filename, config in _on_disk_configs(at).items():
        assert config["metadata"]["dosing_strategy"] == meal_config.DOSING_TEMP_BASAL, (
            filename
        )


# ---------------------------------------------------------------------------
# Phase 4: metadata, echo, invalidation (AC 9, 10, 11)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture_name,group,dosing",
    [
        ("run_2x_autobolus", GROUP_2X, meal_config.DOSING_AUTOBOLUS),
        ("run_2x_temp_basal", GROUP_2X, meal_config.DOSING_TEMP_BASAL),
        ("run_1x", GROUP_1X, meal_config.DOSING_TEMP_BASAL),
    ],
)
def test_metadata_carries_both_selections_as_their_displayed_labels(
    fixture_name, group, dosing, request
):
    """AC 9, including that the pre-existing four keys are unchanged."""
    at = request.getfixturevalue(fixture_name)
    for filename, config in _on_disk_configs(at).items():
        metadata = config["metadata"]
        assert metadata["controller_settings_group"] == group.display, filename
        assert metadata["dosing_strategy"] == dosing, filename
        assert sorted(metadata) == sorted([
            "risk_id",
            "simulation_id",
            "risk_description",
            "config_format_version",
            "controller_settings_group",
            "dosing_strategy",
        ]), filename


@pytest.mark.parametrize(
    "fixture_name,group,dosing",
    [
        ("run_2x_autobolus", GROUP_2X, meal_config.DOSING_AUTOBOLUS),
        ("run_2x_temp_basal", GROUP_2X, meal_config.DOSING_TEMP_BASAL),
        ("run_1x", GROUP_1X, meal_config.DOSING_TEMP_BASAL),
    ],
)
def test_the_summary_echoes_both_selections(fixture_name, group, dosing, request):
    """AC 10: rendered in the generated-configs summary."""
    at = request.getfixturevalue(fixture_name)
    captions = _captions(at)
    assert any(
        group.display in caption and dosing in caption for caption in captions
    ), captions


def test_the_echo_reads_the_json_rather_than_the_widgets(temp_library):
    """AC 10's real claim, proven the TRSET-36 way.

    After a change that invalidates, the widget shows the new value while no generated
    set exists -- so an echo sourced from the widget would still render, and an echo
    sourced from the JSON cannot.
    """
    at = _click_generate(_select(_editor_app(), group=GROUP_2X,
                                dosing=meal_config.DOSING_AUTOBOLUS))
    assert at.session_state["generated_configs"] is not None

    at = _select(at, dosing=meal_config.DOSING_TEMP_BASAL)
    assert at.session_state["generated_configs"] is None
    assert not any(
        meal_config.DOSING_TEMP_BASAL in caption and GROUP_2X.display in caption
        for caption in _captions(at)
    ), "the summary echoed a selection with no generated set behind it"


@pytest.mark.parametrize(
    "group,dosing",
    [
        (GROUP_1X, None),                                  # settings group changed
        (GROUP_2X, meal_config.DOSING_TEMP_BASAL),         # dosing strategy changed
    ],
)
def test_changing_either_radio_invalidates_a_generated_set(temp_library, group, dosing):
    """AC 11: a set built against a different controller must not survive to be run."""
    at = _click_generate(_select(_editor_app(), group=GROUP_2X,
                                 dosing=meal_config.DOSING_AUTOBOLUS))
    assert at.session_state["generated_configs"] is not None

    at = _select(at, group=group, dosing=dosing)
    assert at.session_state["generated_configs"] is None
    assert at.session_state["generated_config_dir"] is None
    assert not [button for button in at.button if button.label == "Run Tool"]


def test_an_unrelated_change_does_not_invalidate(temp_library):
    """AC 11's other half -- TRSET-34's lesson.

    Editing the meal count must leave the generated set and both stored selections
    intact; over-eager invalidation is as much a defect as none.
    """
    at = _click_generate(_select(_editor_app(), group=GROUP_2X,
                                 dosing=meal_config.DOSING_TEMP_BASAL))
    assert at.session_state["generated_configs"] is not None

    at.number_input(key="pm_meal_count").set_value(2).run()
    assert at.session_state["generated_configs"] is not None
    assert at.session_state["generated_settings_group"] == GROUP_2X.display
    assert at.session_state["generated_dosing_strategy"] == meal_config.DOSING_TEMP_BASAL


# ---------------------------------------------------------------------------
# Phase 5: it parses, and it runs (AC 15, AC 14)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture_name", ["run_2x_autobolus", "run_2x_temp_basal", "run_1x"]
)
def test_every_combination_validates_with_zero_errors(fixture_name, request):
    """AC 15, first half: the real ConfigValidator, over all four profiles."""
    at = request.getfixturevalue(fixture_name)
    result = validate_config_dir(
        at.session_state["generated_config_dir"],
        at.session_state["generated_risk_id"],
    )
    assert result.errors_by_file == {}, result.errors_by_file
    assert result.is_valid


@pytest.mark.parametrize(
    "fixture_name", ["run_2x_autobolus", "run_2x_temp_basal", "run_1x"]
)
def test_every_combination_parses_for_every_profile(fixture_name, request):
    """AC 15: a real ScenarioParserV2 parse.

    This is the step that catches a malformed override -- resolve_override raises
    "Only applied N of M overriding values" here, at parse time, long before a run.
    """
    at = request.getfixturevalue(fixture_name)
    risk_dir = _risk_dir(at)
    parsed = 0
    for filename in sorted(os.listdir(risk_dir)):
        if not filename.endswith(".json"):
            continue
        sims = ScenarioParserV2(
            path_to_json_config=os.path.join(risk_dir, filename)
        ).get_sims()
        assert len(sims) == len(meal_config.STAGES), (filename, sorted(sims))
        parsed += 1
    assert parsed == len(meal_config.PROFILES)


@pytest.fixture(scope="module")
def temp_basal_end_to_end(temp_library):
    """The one full run the plan budgets for: 2.x + Temp basal.

    Chosen because it is the only combination that writes an override, and the only one
    whose post-mitigation stage is an inline dict -- so it is the only one where the
    parser has something new to resolve.
    """
    at = make_app_test(default_timeout=RUN_TIMEOUT_SECONDS)
    at.run()
    at.radio(key="config_source").set_value(streamlit_app.SOURCE_CONFIGURE).run()
    at.radio(key="sim_duration_choice").set_value(
        streamlit_app.SHORT_TERM_DURATION_LABEL
    ).run()
    at.number_input(key="sim_duration_hours").set_value(RUN_HOURS).run()
    at.number_input(key="pm_bolus_count").set_value(0).run()
    at.time_input(key="pm_meal_time_0").set_value(MEAL_TIME).run()
    at = _select(at, group=GROUP_2X, dosing=meal_config.DOSING_TEMP_BASAL)
    at = _click_generate(at)

    run_buttons = [button for button in at.button if button.label == "Run Tool"]
    assert len(run_buttons) == 1, "generated configs must be runnable"
    run_buttons[0].click().run()
    thread = at.session_state["run_thread"]
    assert thread is not None, "Run Tool did not start a background thread"
    thread.join(timeout=RUN_TIMEOUT_SECONDS)
    assert not thread.is_alive(), f"run did not finish within {RUN_TIMEOUT_SECONDS}s"
    at.run()
    assert at.session_state["run_error"] is None, at.session_state["run_error"]
    return at


def test_the_temp_basal_configs_run_end_to_end(temp_basal_end_to_end):
    """AC 15, second half: the override survives into a completed assessment."""
    run_result = temp_basal_end_to_end.session_state["run_result"]
    assert not run_result.cancelled
    assert len(run_result.risk_dir_results) == 1
    dir_result = run_result.risk_dir_results[0]
    assert dir_result.assessment_status == "ok", dir_result.assessment_detail
    assert dir_result.assessment.profile_count == len(meal_config.PROFILES)


def test_the_library_source_gains_no_controller_widgets(temp_library):
    """AC 14: the ticket's scope boundary, enforced by a test rather than intention.

    Library-sourced runs take their controller settings from the collection files, so
    neither radio may appear when that source is selected.
    """
    at = make_app_test(default_timeout=60)
    at.run()
    keys = {radio.key for radio in at.radio}
    assert streamlit_app.CONTROLLER_SETTINGS_KEY not in keys, keys
    assert streamlit_app.DOSING_STRATEGY_KEY not in keys, keys
