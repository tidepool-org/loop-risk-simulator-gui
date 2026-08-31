"""Unit tests for meal_config.py -- the TRSET-9 scenario-config generation.

These target the generated JSON and the on-disk layout in isolation: value
resolution per mode, the schema bounds mirrored from ValueValidators, the
patient-model/pump split, and the throwaway config library's shape. The real
chain -- generate -> validate -> run -> export, no mocks -- is exercised in
test_trset9_integration.py, per the approved plan.

Baseline grams are read from the real reusable/carb_doses files rather than
asserted as literals in most places, so these tests do not quietly become a second
source of truth for them. The one test that does pin the literals exists to catch a
baseline changing out from under the feature.
"""

import datetime
import json
import os

import pytest

import meal_config


# The base configs run 8/15/2019 12:00 for 8 hours. Entries are datetimes, not
# times of day (TRSET-13): once a run passes midnight, which day an entry falls on
# is part of when it is.
START_DAY = datetime.date(2019, 8, 15)


def _at(hour, minute=0, day=START_DAY):
    return datetime.datetime.combine(day, datetime.time(hour, minute, 0))


NOON = _at(12)
AFTERNOON = _at(15, 30)
BEFORE_WINDOW = _at(11, 59)
AFTER_WINDOW = _at(20, 1)

RISK_ID = "TLR-20260806-143000"


def _spec(mode=meal_config.MODE_STANDARD, meals=None, boluses=None, duration_hours=None):
    return meal_config.MealConfigSpec.aligned(
        mode,
        meal_config.EntrySet(
            meals=meals if meals is not None else [meal_config.MealEntry(NOON)],
            boluses=boluses if boluses is not None else [],
        ),
        duration_hours=duration_hours,
    )


def _median_config(spec):
    return meal_config.generate_config(spec, RISK_ID, meal_config.PROFILES[0])


def _stage(config, index):
    return config["override_config"][index]


def _pm(stage):
    return stage["patient"]["patient_model"]


def _pump(stage):
    return stage["patient"]["pump"]


# ---------------------------------------------------------------------------
# Baselines and value modes
# ---------------------------------------------------------------------------


def test_standard_baselines_match_the_library_values():
    """Pins the four documented baselines, so a library change is caught here."""
    grams = {p.display: meal_config.standard_carb_grams(p) for p in meal_config.PROFILES}
    assert grams == {"Median": 31.0, "Resistant": 33.0, "Adolescent": 60.0, "Sensitive": 25.0}


def test_standard_baselines_are_read_from_the_reusable_files_not_hardcoded(tmp_path, monkeypatch):
    """Point the env seam at a temp library and the standard value follows it."""
    carb_doses = tmp_path / "tidepool_risk_v2" / "reusable" / "carb_doses"
    carb_doses.mkdir(parents=True)
    (carb_doses / "median_profile_v1.json").write_text(
        json.dumps([{"type": "carb", "start_time": "8/15/2019 12:00:00", "value": 99.0}])
    )
    monkeypatch.setenv(meal_config.SCENARIO_CONFIGS_ROOT_ENV, str(tmp_path))

    assert meal_config.standard_carb_grams(meal_config.PROFILES[0]) == 99.0


def test_multiplier_scales_each_profiles_own_baseline():
    resolved = [
        meal_config.resolve_carb_grams(meal_config.MODE_MULTIPLIER, profile, 2.0)
        for profile in meal_config.PROFILES
    ]
    assert resolved == [62.0, 66.0, 120.0, 50.0]


def test_multiplier_below_one_is_allowed():
    assert meal_config.resolve_carb_grams(
        meal_config.MODE_MULTIPLIER, meal_config.PROFILES[1], 0.25
    ) == pytest.approx(8.25)


@pytest.mark.parametrize("multiplier", [0.3, 1.1, 0.1, 2.4])
def test_multiplier_off_the_quarter_step_is_rejected(multiplier):
    with pytest.raises(meal_config.MealConfigError, match="multiple of 0.25"):
        meal_config.resolve_carb_grams(
            meal_config.MODE_MULTIPLIER, meal_config.PROFILES[0], multiplier
        )


@pytest.mark.parametrize("multiplier", [0, -1.0])
def test_non_positive_multiplier_is_rejected(multiplier):
    with pytest.raises(meal_config.MealConfigError, match="greater than 0"):
        meal_config.resolve_carb_grams(
            meal_config.MODE_MULTIPLIER, meal_config.PROFILES[0], multiplier
        )


def test_multiplier_mode_needs_a_multiplier():
    with pytest.raises(meal_config.MealConfigError, match="needs a multiplier"):
        meal_config.resolve_carb_grams(meal_config.MODE_MULTIPLIER, meal_config.PROFILES[0], None)


def test_custom_value_applies_identically_to_every_profile():
    resolved = [
        meal_config.resolve_carb_grams(meal_config.MODE_CUSTOM, profile, 45.0)
        for profile in meal_config.PROFILES
    ]
    assert resolved == [45.0, 45.0, 45.0, 45.0]


def test_custom_mode_needs_a_value():
    with pytest.raises(meal_config.MealConfigError, match="needs a grams value"):
        meal_config.resolve_carb_grams(meal_config.MODE_CUSTOM, meal_config.PROFILES[0], None)


def test_unknown_mode_is_rejected():
    with pytest.raises(meal_config.MealConfigError, match="Unknown meal mode"):
        meal_config.resolve_carb_grams("brunch", meal_config.PROFILES[0], 1.0)


@pytest.mark.parametrize("grams", [0.0, -5.0, 501.0])
def test_carb_values_outside_the_validators_bounds_are_rejected(grams):
    with pytest.raises(meal_config.MealConfigError, match="outside the allowed"):
        meal_config.resolve_carb_grams(meal_config.MODE_CUSTOM, meal_config.PROFILES[0], grams)


# ---------------------------------------------------------------------------
# Carb entry shape
# ---------------------------------------------------------------------------


def test_absorption_duration_is_omitted_when_not_given():
    """Omitting the key is what selects the parser's own 180-minute default."""
    entry = _pm(_stage(_median_config(_spec()), 0))["carb_entries"][0]
    assert "duration" not in entry
    assert entry["value"] == 31.0
    assert entry["start_time"] == "8/15/2019 12:00:00"
    assert entry["type"] == "carb"


def test_absorption_duration_is_written_when_given():
    spec = _spec(meals=[meal_config.MealEntry(AFTERNOON, duration_minutes=45)])
    entry = _pm(_stage(_median_config(spec), 0))["carb_entries"][0]
    assert entry["duration"] == 45
    assert entry["start_time"] == "8/15/2019 15:30:00"


@pytest.mark.parametrize("duration", [0, 601])
def test_absorption_duration_outside_the_validators_bounds_is_rejected(duration):
    spec = _spec(meals=[meal_config.MealEntry(NOON, duration_minutes=duration)])
    with pytest.raises(meal_config.MealConfigError, match="between 0 and 600"):
        _median_config(spec)


def test_multiple_meal_entries_are_all_written():
    spec = _spec(
        mode=meal_config.MODE_CUSTOM,
        meals=[
            meal_config.MealEntry(NOON, value_input=20.0),
            meal_config.MealEntry(AFTERNOON, duration_minutes=45, value_input=60.0),
        ],
    )
    entries = _pm(_stage(_median_config(spec), 0))["carb_entries"]
    assert [entry["value"] for entry in entries] == [20.0, 60.0]


@pytest.mark.parametrize("when", [BEFORE_WINDOW, AFTER_WINDOW])
def test_entry_times_outside_the_simulation_window_are_rejected(when):
    with pytest.raises(meal_config.MealConfigError, match="outside the simulation window"):
        _median_config(_spec(meals=[meal_config.MealEntry(when)]))


def test_a_spec_with_no_entries_at_all_is_rejected():
    with pytest.raises(meal_config.MealConfigError, match="at least one meal or bolus"):
        _median_config(_spec(meals=[], boluses=[]))


# ---------------------------------------------------------------------------
# Simulation duration and the window it sets (TRSET-13)
# ---------------------------------------------------------------------------


def test_the_default_duration_is_the_base_configs_own_not_a_constant_restating_it():
    """None means "whatever the library says", so there is one source for it."""
    base_start, base_hours = meal_config.base_window()
    start, end, hours = meal_config.simulation_window(None)
    assert (start, hours) == (base_start, base_hours)
    assert end == base_start + datetime.timedelta(hours=base_hours)


@pytest.mark.parametrize(
    "duration_hours, expected_end",
    [
        (meal_config.DURATION_OVERDELIVERY_HOURS, _at(20)),
        (meal_config.DURATION_UNDERDELIVERY_HOURS, _at(11, day=datetime.date(2019, 8, 16))),
        (meal_config.DURATION_FULL_DAY_HOURS, _at(12, day=datetime.date(2019, 8, 16))),
        (2.0, _at(14)),
        (7.5, _at(19, 30)),
    ],
)
def test_the_window_end_is_start_plus_duration_and_never_wraps(duration_hours, expected_end):
    """The defect this fixes: (12:00 + 23h).time() reads back as 11:00, so every
    ``start <= when <= end`` check silently became false."""
    start, end, hours = meal_config.simulation_window(duration_hours)
    assert (start, end, hours) == (NOON, expected_end, duration_hours)
    assert end > start, "the window must never wrap"


def test_an_entry_late_in_a_midnight_crossing_window_is_accepted():
    """At 8 hours this same entry is out of the window -- at 24 it is not."""
    spec = _spec(
        meals=[meal_config.MealEntry(_at(23, 30))],
        duration_hours=meal_config.DURATION_FULL_DAY_HOURS,
    )
    entry = _pm(_stage(_median_config(spec), 0))["carb_entries"][0]
    assert entry["start_time"] == "8/15/2019 23:30:00"


def test_an_entry_beyond_a_shortened_window_is_rejected_never_moved():
    spec = _spec(meals=[meal_config.MealEntry(AFTERNOON)], duration_hours=2.0)
    with pytest.raises(meal_config.MealConfigError) as excinfo:
        _median_config(spec)
    message = str(excinfo.value)
    assert "8/15/2019 15:30:00" in message, message      # names the entry
    assert "12:00:00-14:00:00" in message, message       # names the window


def test_an_entry_on_a_later_day_takes_its_own_date_token():
    """The token comes from the entry's own datetime, not one module-level token --
    so widening the authoring bound to a second day needs no change here."""
    spec = _spec(
        meals=[meal_config.MealEntry(_at(3, 15, day=datetime.date(2019, 8, 16)))],
        duration_hours=meal_config.DURATION_FULL_DAY_HOURS,
    )
    entry = _pm(_stage(_median_config(spec), 0))["carb_entries"][0]
    assert entry["start_time"] == "8/16/2019 03:15:00"


def test_the_date_token_keeps_the_librarys_unpadded_spelling():
    assert meal_config.date_token(_at(12)) == "8/15/2019"
    assert meal_config.entry_token(_at(9, 5)) == "8/15/2019 09:05:00"


@pytest.mark.parametrize("duration_hours", meal_config.PRESET_DURATION_HOURS)
def test_every_preset_duration_is_accepted(duration_hours):
    assert meal_config.validate_duration_hours(duration_hours) == duration_hours


@pytest.mark.parametrize("duration_hours", [2.0, 2.5, 5.0, 7.5])
def test_short_term_durations_on_the_half_hour_are_accepted(duration_hours):
    assert meal_config.validate_duration_hours(duration_hours) == duration_hours


@pytest.mark.parametrize("duration_hours", [1.5, 0.0, 7.75, 9.0, 25.0])
def test_short_term_durations_outside_the_bounds_are_rejected_naming_the_bound(
    duration_hours
):
    with pytest.raises(meal_config.MealConfigError, match="between 2 and 7.5 hours"):
        meal_config.validate_duration_hours(duration_hours)


@pytest.mark.parametrize("duration_hours", [2.25, 3.1, 6.75])
def test_short_term_durations_off_the_half_hour_step_are_rejected(duration_hours):
    with pytest.raises(meal_config.MealConfigError, match="multiple of 0.5 hours"):
        meal_config.validate_duration_hours(duration_hours)


def test_a_missing_duration_is_rejected_rather_than_guessed():
    with pytest.raises(meal_config.MealConfigError, match="duration is required"):
        meal_config.validate_duration_hours(None)


def test_an_invalid_duration_is_rejected_before_any_config_is_generated():
    spec = _spec(duration_hours=3.1)
    with pytest.raises(meal_config.MealConfigError, match="multiple of 0.5 hours"):
        _median_config(spec)


@pytest.mark.parametrize(
    "duration_hours",
    [None, 2.0, meal_config.DURATION_UNDERDELIVERY_HOURS, meal_config.DURATION_FULL_DAY_HOURS],
)
def test_every_override_entry_of_every_profile_carries_the_duration(duration_hours):
    """One duration for the whole configuration: three stages x four profiles."""
    expected = meal_config.simulation_window(duration_hours)[2]
    configs = meal_config.generate_configs(_spec(duration_hours=duration_hours), RISK_ID)

    assert len(configs) == len(meal_config.PROFILES)
    for filename, config in configs.items():
        durations = [override["duration_hours"] for override in config["override_config"]]
        assert durations == [expected] * len(meal_config.STAGES), filename


def test_the_authoring_window_stops_at_the_end_of_the_start_day_but_the_run_does_not():
    """The clamp is a bound on what the editor can offer, not a rule about the run."""
    _, sim_end, _ = meal_config.simulation_window(meal_config.DURATION_FULL_DAY_HOURS)
    start, latest = meal_config.authoring_window(meal_config.DURATION_FULL_DAY_HOURS)

    assert start == NOON
    assert latest.date() == START_DAY
    assert latest < sim_end, "the simulated tail must extend past the authoring bound"


@pytest.mark.parametrize("duration_hours", [2.0, 7.5, meal_config.DURATION_OVERDELIVERY_HOURS])
def test_a_window_inside_one_day_is_not_clamped_at_all(duration_hours):
    _, sim_end, _ = meal_config.simulation_window(duration_hours)
    assert meal_config.authoring_window(duration_hours)[1] == sim_end


@pytest.mark.parametrize(
    "duration_hours, valid",
    [(2.0, False), (7.5, False), (8.0, True), (23.0, True), (24.0, True)],
)
def test_metrics_are_valid_only_from_eight_hours_up(duration_hours, valid):
    assert meal_config.metrics_are_valid(duration_hours) is valid


# ---------------------------------------------------------------------------
# Bolus entries, the sentinel, and the No Loop stage
# ---------------------------------------------------------------------------


def test_numeric_bolus_goes_to_both_the_patient_model_and_the_pump():
    spec = _spec(boluses=[meal_config.BolusEntry(NOON, 4.5)])
    for index in range(len(meal_config.STAGES)):
        stage = _stage(_median_config(spec), index)
        assert _pm(stage)["bolus_entries"] == [{"time": "8/15/2019 12:00:00", "value": 4.5}]
        assert _pump(stage)["bolus_entries"] == [{"time": "8/15/2019 12:00:00", "value": 4.5}]


def test_a_zero_unit_bolus_is_a_legitimate_value():
    spec = _spec(boluses=[meal_config.BolusEntry(NOON, 0)])
    assert _pm(_stage(_median_config(spec), 0))["bolus_entries"][0]["value"] == 0.0


def test_sentinel_reaches_the_patient_model_but_never_the_pump():
    """ConfigValidator rejects the sentinel under patient.pump.bolus_entries."""
    spec = _spec(
        boluses=[
            meal_config.BolusEntry(NOON, meal_config.ACCEPT_RECOMMENDATION, no_loop_units=3.3)
        ]
    )
    config = _median_config(spec)
    for index in (0, 2):  # the two Loop stages
        stage = _stage(config, index)
        assert _pm(stage)["bolus_entries"][0]["value"] == meal_config.ACCEPT_RECOMMENDATION
        assert _pump(stage)["bolus_entries"] == []


def test_the_no_loop_stage_replaces_the_sentinel_with_its_numeric_value():
    """With controller: null nothing resolves the sentinel, so it must not survive."""
    spec = _spec(
        boluses=[
            meal_config.BolusEntry(NOON, meal_config.ACCEPT_RECOMMENDATION, no_loop_units=3.3)
        ]
    )
    no_loop = _stage(_median_config(spec), 1)
    assert no_loop["sim_id"].startswith("pre-noLoop_")
    assert no_loop["controller"] is None
    assert _pm(no_loop)["bolus_entries"] == [{"time": "8/15/2019 12:00:00", "value": 3.3}]
    assert _pump(no_loop)["bolus_entries"] == [{"time": "8/15/2019 12:00:00", "value": 3.3}]


def test_a_sentinel_bolus_without_a_no_loop_value_is_rejected():
    spec = _spec(boluses=[meal_config.BolusEntry(NOON, meal_config.ACCEPT_RECOMMENDATION)])
    with pytest.raises(meal_config.MealConfigError, match="No Loop stage's bolus"):
        _median_config(spec)


@pytest.mark.parametrize("units", [-0.1, 50.1])
def test_bolus_values_outside_the_validators_bounds_are_rejected(units):
    spec = _spec(boluses=[meal_config.BolusEntry(NOON, units)])
    with pytest.raises(meal_config.MealConfigError, match="between 0 and 50"):
        _median_config(spec)


# ---------------------------------------------------------------------------
# Aligned vs independent timelines
# ---------------------------------------------------------------------------


def test_aligned_writes_the_same_entry_lists_to_both_timelines():
    spec = _spec(
        mode=meal_config.MODE_CUSTOM,
        meals=[meal_config.MealEntry(NOON, value_input=40.0)],
        boluses=[meal_config.BolusEntry(NOON, 2.0)],
    )
    stage = _stage(_median_config(spec), 0)
    assert _pm(stage)["carb_entries"] == _pump(stage)["carb_entries"]
    assert _pm(stage)["bolus_entries"] == _pump(stage)["bolus_entries"]


def test_independent_writes_each_timeline_its_own_entries():
    spec = meal_config.MealConfigSpec(
        mode=meal_config.MODE_CUSTOM,
        patient_model=meal_config.EntrySet(
            meals=[meal_config.MealEntry(NOON, value_input=40.0)],
            boluses=[meal_config.BolusEntry(NOON, 2.0)],
        ),
        pump=meal_config.EntrySet(
            meals=[meal_config.MealEntry(AFTERNOON, value_input=10.0)],
            boluses=[meal_config.BolusEntry(AFTERNOON, 1.0)],
        ),
    )
    stage = _stage(_median_config(spec), 0)
    assert [entry["value"] for entry in _pm(stage)["carb_entries"]] == [40.0]
    assert [entry["value"] for entry in _pump(stage)["carb_entries"]] == [10.0]
    assert _pm(stage)["bolus_entries"][0]["time"] == "8/15/2019 12:00:00"
    assert _pump(stage)["bolus_entries"][0]["time"] == "8/15/2019 15:30:00"


# ---------------------------------------------------------------------------
# Config identity and overall shape
# ---------------------------------------------------------------------------


def test_risk_id_is_the_generation_timestamp():
    assert meal_config.risk_id(datetime.datetime(2026, 8, 6, 14, 30, 0)) == RISK_ID


def test_filenames_follow_the_library_convention_and_read_back_as_profiles():
    """The app's own _profile_label must recover the profile from the filename."""
    from streamlit_app import _profile_label

    for profile in meal_config.PROFILES:
        filename = meal_config.config_filename(RISK_ID, profile)
        assert filename == f"Simulation-Configuration-{RISK_ID}_{profile.display}_Profile.json"
        assert _profile_label(filename, RISK_ID) == f"{profile.display} profile"


def test_one_config_per_t1_profile_is_generated():
    configs = meal_config.generate_configs(_spec(), RISK_ID)
    assert len(configs) == 4
    assert sorted(configs) == sorted(
        meal_config.config_filename(RISK_ID, profile) for profile in meal_config.PROFILES
    )


def test_metadata_carries_the_risk_id_and_the_profiles_base_config():
    configs = meal_config.generate_configs(_spec(), RISK_ID)
    for profile in meal_config.PROFILES:
        config = configs[meal_config.config_filename(RISK_ID, profile)]
        assert config["metadata"]["risk_id"] == RISK_ID
        assert config["metadata"]["simulation_id"] == f"{RISK_ID}-{profile.token}"
        assert config["base_config"] == f"reusable.simulations.base_{profile.token}_2_0_v1"


def test_every_generated_sim_id_classifies_to_a_stage():
    """Otherwise the results grid and the export show 'No data' for that stage."""
    from tidepool_data_science_simulator.projects.risk.gui_runner import (
        STAGE_ORDER,
        classify_sim_id,
    )

    configs = meal_config.generate_configs(_spec(), RISK_ID)
    for config in configs.values():
        stages = [classify_sim_id(override["sim_id"]) for override in config["override_config"]]
        assert stages == STAGE_ORDER


def test_the_mitigated_stage_carries_the_profiles_guardrails():
    config = _median_config(_spec())
    post = _stage(config, 2)
    assert _pump(post)["target_range"] == "reusable.mitigations.guardrails.target_range_median_v1"
    assert post["controller"] == {
        "settings": "reusable.mitigations.guardrails.controller_settings_median_swift"
    }


def test_the_unmitigated_loop_stage_has_no_guardrails_and_no_controller_override():
    pre = _stage(_median_config(_spec()), 0)
    assert "target_range" not in _pump(pre)
    assert "controller" not in pre


# ---------------------------------------------------------------------------
# The throwaway config library
# ---------------------------------------------------------------------------


def test_write_config_library_builds_a_runnable_layout(tmp_path):
    configs = meal_config.generate_configs(_spec(), RISK_ID)
    config_dir = meal_config.write_config_library(configs, RISK_ID, str(tmp_path))

    risk_dir = os.path.join(config_dir, RISK_ID)
    assert sorted(os.listdir(risk_dir)) == sorted(configs)
    # reusable/ must resolve one level up from the collection's parent, the way
    # gui_runner._find_pointer_object_dir walks for a real collection.
    reusable = os.path.join(tmp_path, "tidepool_risk_v2", "reusable")
    assert os.path.islink(reusable)
    assert os.path.isdir(os.path.join(reusable, "carb_doses"))


def test_written_configs_are_byte_identical_to_config_bytes(tmp_path):
    configs = meal_config.generate_configs(_spec(), RISK_ID)
    config_dir = meal_config.write_config_library(configs, RISK_ID, str(tmp_path))

    for filename, config in configs.items():
        with open(os.path.join(config_dir, RISK_ID, filename), "rb") as handle:
            assert handle.read() == meal_config.config_bytes(config)


def test_regenerating_replaces_the_previous_configs(tmp_path):
    meal_config.write_config_library(
        {"Simulation-Configuration-stale_Median_Profile.json": {}}, RISK_ID, str(tmp_path)
    )
    configs = meal_config.generate_configs(_spec(), RISK_ID)
    config_dir = meal_config.write_config_library(configs, RISK_ID, str(tmp_path))

    assert "Simulation-Configuration-stale_Median_Profile.json" not in os.listdir(
        os.path.join(config_dir, RISK_ID)
    )


def test_nothing_is_written_into_the_real_library(tmp_path):
    library = meal_config.scenario_configs_root()
    before = os.stat(library).st_mtime_ns
    meal_config.write_config_library(
        meal_config.generate_configs(_spec(), RISK_ID), RISK_ID, str(tmp_path)
    )
    assert os.stat(library).st_mtime_ns == before


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
