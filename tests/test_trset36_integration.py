"""End-to-end integration test for TRSET-36 narrative risk description (Feature gate).

Per the approved five-phase plan: no mocks anywhere in the exercised path, and every
assertion is made at a boundary of the full system -- the generated JSON on disk, the
app's own rendered summary, and the session state the editor's invalidation rests on --
never on the functions that produced them.

    the app's own Risk description field, driven through AppTest
        -> meal_config.MealConfigSpec / generate_configs / write_config_library
        -> the four real config files on disk        (metadata.risk_description)
        -> the generated-configs summary             (echoed back FROM the JSON)

Phase 4's limit check is asserted at ``generate_config()`` rather than through the app,
because ``max_chars`` blocks the over-long text before it can ever reach the generator:
what is under test there is the mirrored bound that defends the generator for a
non-Streamlit caller, which is exactly what the module's own docstring asks of every
other bound it mirrors. It is kept in this file so one file proves the feature.

The temp library is the established one (TRSET-9, TRSET-13): real layout from the
``tidepool_risk_v2`` level down, the real ``reusable/`` symlinked so the baselines the
generator reads are real, the app's own env seams, and zero writes into the installed
library.
"""

import datetime
import json
import os

import pytest

pytest.importorskip("streamlit")
# The shared AppTest factory (tests/conftest.py). It builds the harness with the
# TRSET-34 start-page gate already acknowledged, so this suite drives the tool
# directly, as every suite before it does.
from conftest import make_app_test  # noqa: E402

import meal_config  # noqa: E402
import streamlit_app  # noqa: E402


# The base configs run 8/15/2019 12:00 for 8 hours; entries are datetimes, not times
# of day, so the generator's window check has a real day to place them on.
NOON = datetime.datetime.combine(datetime.date(2019, 8, 15), datetime.time(12, 0))

RISK_ID = "TLR-20260902-141500"

# Padding either side of the payload, so the limit tests prove the limit is applied to
# the STRIPPED text (AC 6) rather than to what the user typed.
PAD = "   "

# What a real author would type: padded either side, and carrying a newline, so one
# round trip proves the strip (AC 6) and the newline preservation (AC 10) together.
DESCRIPTION_TYPED = (
    "  Large evening meal with bolus accepted\nfrom Loop; no correction for 4 h  "
)
DESCRIPTION_STORED = DESCRIPTION_TYPED.strip()

# A second, unmistakably different description, for the staleness phase.
DESCRIPTION_REVISED = "A different situation entirely: no bolus for a 90 g meal"

EMPTY_COLLECTION = "_pytest_trset36_empty_collection"


@pytest.fixture(scope="module")
def temp_library(tmp_path_factory):
    """Point the app at a temp scenario library holding one EMPTY collection.

    The fixture shape TRSET-9 and TRSET-13 both use: the real ``reusable/`` symlinked
    in, so the baselines the generator reads and the pointers it writes are real, while
    nothing this feature writes can land in the installed library.
    """
    prior_configs_root = os.environ.get(meal_config.SCENARIO_CONFIGS_ROOT_ENV)
    prior_allowed = os.environ.get("LOOP_RISK_GUI_ALLOWED_COLLECTIONS")
    source_root = prior_configs_root or meal_config.scenario_configs_root()

    temp_root = str(tmp_path_factory.mktemp("trset36_lib"))
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
# Driving the generator directly (Phase 4)
# ---------------------------------------------------------------------------


def _spec(risk_description):
    return meal_config.MealConfigSpec.aligned(
        meal_config.MODE_STANDARD,
        meal_config.EntrySet(meals=[meal_config.MealEntry(NOON)]),
        risk_description=risk_description,
    )


def _generated_config(risk_description, profile=meal_config.PROFILES[0]):
    return meal_config.generate_config(_spec(risk_description), RISK_ID, profile)


# ---------------------------------------------------------------------------
# Driving the app (Phases 1-3)
# ---------------------------------------------------------------------------


def _editor_app():
    """The app with the configure-meals source selected and one standard meal.

    The bolus row is dropped rather than filled: a bolus needs a dose, and this
    feature has nothing to do with one. One meal is enough for a valid spec.
    """
    at = make_app_test(default_timeout=60)
    at.run()
    at.radio(key="config_source").set_value(streamlit_app.SOURCE_CONFIGURE).run()
    at.number_input(key="pm_bolus_count").set_value(0).run()
    return at


def _describe(at, text):
    at.text_area(key=streamlit_app.RISK_DESCRIPTION_KEY).set_value(text).run()
    return at


def _click_generate(at):
    generate = [button for button in at.button if button.label == "Generate configs"]
    assert len(generate) == 1, [button.label for button in at.button]
    generate[0].click().run()
    assert not at.exception
    assert not at.error, [error.value for error in at.error]
    return at


def _on_disk_configs(at):
    """The generated configs re-read from disk -- not the in-memory dicts."""
    read_back = {}
    for filename in os.listdir(_risk_dir(at)):
        with open(os.path.join(_risk_dir(at), filename)) as handle:
            read_back[filename] = json.load(handle)
    return read_back


def _risk_dir(at):
    return os.path.join(
        at.session_state["generated_config_dir"], at.session_state["generated_risk_id"]
    )


def _captions(at):
    return [caption.value for caption in at.caption]


# ---------------------------------------------------------------------------
# Phase 1: the round trip -- typed text to the JSON on disk
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def described_run(temp_library):
    """One generated set, authored through the app with a real description."""
    return _click_generate(_describe(_editor_app(), DESCRIPTION_TYPED))


def test_the_typed_description_reaches_every_generated_file_stripped(described_run):
    configs = _on_disk_configs(described_run)
    assert len(configs) == len(meal_config.PROFILES)
    for filename, config in configs.items():
        assert config["metadata"]["risk_description"] == DESCRIPTION_STORED, filename


def test_the_embedded_newline_survives_into_the_written_json(described_run):
    r"""AC 10: newlines are preserved, not collapsed, and land as ``\n`` in the file."""
    assert "\n" in DESCRIPTION_STORED, "precondition: the typed text carries a newline"
    for filename in os.listdir(_risk_dir(described_run)):
        with open(os.path.join(_risk_dir(described_run), filename)) as handle:
            raw = handle.read()
        # Serialized as an escape, so the JSON stays one line per key.
        assert r"accepted\nfrom Loop" in raw, filename


def test_the_summary_echoes_the_description_that_was_written(described_run):
    """The echo is a verification of the file, not a mirror of the widget.

    That distinction is what the fallback test below proves: there the widget holds
    ``""`` and the summary shows ``RISK_DESCRIPTION``, which only a reader working from
    the written JSON can do.
    """
    captions = _captions(described_run)
    assert any(DESCRIPTION_STORED in caption for caption in captions), captions


def test_the_other_three_metadata_keys_are_untouched(described_run):
    """AC 14: this feature replaces one value and adds no key."""
    generated_risk_id = described_run.session_state["generated_risk_id"]
    for filename, config in _on_disk_configs(described_run).items():
        metadata = config["metadata"]
        assert sorted(metadata) == [
            "config_format_version", "risk_description", "risk_id", "simulation_id",
        ], filename
        assert metadata["risk_id"] == generated_risk_id, filename
        assert metadata["config_format_version"] == meal_config.CONFIG_FORMAT_VERSION
        assert metadata["simulation_id"].startswith(f"{generated_risk_id}-"), filename


# ---------------------------------------------------------------------------
# Phase 3: staleness -- the downloadable thing must be the thing on screen
# ---------------------------------------------------------------------------


def test_editing_the_description_drops_the_generated_set(temp_library):
    """AC 12: the export ships the configs as the record of what was run, so a set
    whose description no longer matches the field must not survive to be run."""
    at = _click_generate(_describe(_editor_app(), DESCRIPTION_TYPED))
    assert at.session_state["generated_configs"] is not None, "precondition"

    _describe(at, DESCRIPTION_REVISED)
    assert at.session_state["generated_configs"] is None
    assert not [button for button in at.button if button.label == "Run Tool"]


def test_regenerating_after_an_edit_writes_the_new_description(temp_library):
    at = _click_generate(_describe(_editor_app(), DESCRIPTION_TYPED))
    at = _click_generate(_describe(at, DESCRIPTION_REVISED))
    for filename, config in _on_disk_configs(at).items():
        assert config["metadata"]["risk_description"] == DESCRIPTION_REVISED, filename


def test_an_unrelated_change_leaves_the_generated_set_alone(temp_library):
    """The other half of the rule, and the half a mistake hides in (TRSET-34's lesson):
    only the description invalidates, so editing the entries must not."""
    at = _click_generate(_describe(_editor_app(), DESCRIPTION_TYPED))
    at.number_input(key="pm_meal_count").set_value(2).run()

    assert at.session_state["generated_configs"] is not None
    assert at.session_state["generated_risk_description"] == DESCRIPTION_STORED
    assert [button for button in at.button if button.label == "Run Tool"]


# ---------------------------------------------------------------------------
# Phase 2: the fallback -- nothing said produces exactly today's output
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("typed", ["", "   ", " \n\t "])
def test_an_unfilled_field_writes_and_echoes_the_existing_constant(temp_library, typed):
    """AC 7, asserted against the constant so an empty string cannot pass for it.

    The echo half is also the proof that the summary reads the written JSON rather than
    the widget: the widget holds whitespace here, and the summary shows the constant.
    """
    at = _click_generate(_describe(_editor_app(), typed))
    for filename, config in _on_disk_configs(at).items():
        assert config["metadata"]["risk_description"] == meal_config.RISK_DESCRIPTION, filename
    captions = _captions(at)
    assert any(meal_config.RISK_DESCRIPTION in caption for caption in captions), captions


def test_a_spec_that_never_names_a_description_is_unchanged_from_today():
    """The None case, which the widget cannot produce but a non-Streamlit caller can."""
    metadata = _generated_config(None)["metadata"]
    assert metadata["risk_description"] == meal_config.RISK_DESCRIPTION


# ---------------------------------------------------------------------------
# Phase 4: the widget's wiring, and the mirrored limit at the generator
# ---------------------------------------------------------------------------


def test_the_field_is_the_first_control_under_the_editors_heading(temp_library):
    """AC 2: the description is the premise; the mechanics answer to it."""
    order = [
        (element.type, getattr(element, "key", None) or getattr(element, "value", None))
        for element in _editor_app().main
    ]
    heading = order.index(("markdown", "### Meal and bolus configuration"))
    assert order[heading + 1] == ("text_area", streamlit_app.RISK_DESCRIPTION_KEY), order
    assert order.index(("radio", "sim_duration_choice")) > heading + 1


def test_the_field_reads_its_limit_and_its_copy_from_the_constants(temp_library):
    """AC 3-4, as a rendered assertion rather than a source-level one.

    The scoping session recorded that ``max_chars`` might not be reachable through
    ``AppTest``; it is -- streamlit's ``TextArea`` element carries ``label``, ``help``
    and ``max_chars`` as fields -- so the wiring is checked where a user meets it.
    """
    field = _editor_app().text_area(key=streamlit_app.RISK_DESCRIPTION_KEY)
    assert field.label == streamlit_app.RISK_DESCRIPTION_LABEL
    assert field.help == streamlit_app.RISK_DESCRIPTION_HELP
    assert field.max_chars == meal_config.RISK_DESCRIPTION_MAX_CHARS


def test_a_description_one_over_the_limit_is_refused_naming_the_limit():
    limit = meal_config.RISK_DESCRIPTION_MAX_CHARS
    with pytest.raises(meal_config.MealConfigError) as raised:
        _generated_config(PAD + "x" * (limit + 1) + PAD)
    assert str(limit) in str(raised.value), str(raised.value)


def test_a_description_exactly_at_the_limit_generates():
    """The padding is stripped first, so this is 250 characters, not 256."""
    limit = meal_config.RISK_DESCRIPTION_MAX_CHARS
    assert _generated_config(PAD + "x" * limit + PAD)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
