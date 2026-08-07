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


NOON = datetime.time(12, 0, 0)
AFTERNOON = datetime.time(15, 30, 0)
# The base configs run 8/15/2019 12:00 for 8 hours, so this is outside the window.
BEFORE_WINDOW = datetime.time(11, 59, 0)
AFTER_WINDOW = datetime.time(20, 1, 0)

RISK_ID = "TLR-20260806-143000"


def _spec(mode=meal_config.MODE_STANDARD, meals=None, boluses=None):
    return meal_config.MealConfigSpec.aligned(
        mode,
        meal_config.EntrySet(
            meals=meals if meals is not None else [meal_config.MealEntry(NOON)],
            boluses=boluses if boluses is not None else [],
        ),
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
