"""Generate runnable scenario configs from GUI-authored meal/bolus entries (TRSET-9).

Deliberately streamlit-free, for the same reason ``export_bundle.py`` is: turning a
user's meal and bolus entries into schema-conformant JSON is not presentation, and
keeping it here means the generated configs can be asserted directly rather than
through ``AppTest``.

Nothing here writes into the scenario-config library. Configs are written to a
caller-supplied directory laid out like the library (with ``reusable/`` symlinked so
``reusable.*`` pointer resolution and ``gui_runner._find_pointer_object_dir`` behave
exactly as they do for a real collection), which the GUI then hands to
``run_risk_assessment`` as its ``config_dir``. That is the "configure parameters
directly" mode ``streamlit_app.py``'s docstring reserves, and it needs no change in
``gui_runner``.

The generated shape is the library's own baseline template
(``loop_risk_v2_2_0_full/TLR-1005/Simulation-Configuration-TLR-000-base_<profile>_profile_v1.json``):
three stages whose sim_ids all classify through ``severity_model.classify_sim_id``,
on the 2_0/swift base configs. Only ``carb_entries`` / ``bolus_entries`` come from the
user -- glucose history, target range, controller settings and everything reached
through ``base_config`` are the baseline's.

``scenario_json_parser_v2.py`` remains the schema authority and is not touched. The
bounds mirrored in ``_validate_*`` below are the ones
``validation.value_validators.ValueValidators`` enforces, checked here so bad input
fails in the editor instead of at run time.
"""

import datetime
import json
import os
import shutil
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

from tidepool_data_science_simulator.utils import PROJECT_ROOT_DIR

# Same env seam streamlit_app.py uses to point at a vendored (bundle) or temp
# (test) scenario_configs root. Defined here so the app and this module cannot
# drift apart about where the library is.
SCENARIO_CONFIGS_ROOT_ENV = "LOOP_RISK_GUI_SCENARIO_CONFIGS_ROOT"

# The sentinel bolus value meaning "take whatever Loop recommends". It is a
# PATIENT-side construct: ConfigValidator rejects it under
# patient.pump.bolus_entries, because the pump timeline is dumped verbatim into
# the Loop input JSON and the string crashes the Swift bridge's Double decode.
ACCEPT_RECOMMENDATION = "accept_recommendation"

# Meal value modes (AC 2). Mutually exclusive, chosen once per configuration.
MODE_STANDARD = "standard"
MODE_MULTIPLIER = "multiplier"
MODE_CUSTOM = "custom"
MEAL_MODES = (MODE_STANDARD, MODE_MULTIPLIER, MODE_CUSTOM)

# Multiplier precision is fixed at 0.25 increments (a stated constraint), so a
# multiplier is validated against this rather than free-form.
MULTIPLIER_STEP = 0.25

# Bounds ValueValidators enforces. Mirrored so generation fails loudly rather than
# emitting a config the validator would reject at run time.
CARB_GRAMS_MAX = 500.0
CARB_DURATION_MINUTES_MAX = 600.0
BOLUS_UNITS_MAX = 50.0

# The parser's default absorption duration, applied when an entry omits "duration".
# Named so the UI can say what omitting it means without repeating the number.
DEFAULT_CARB_DURATION_MINUTES = 180

RISK_ID_PREFIX = "TLR-"
RISK_ID_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
CONFIG_FORMAT_VERSION = "v1.0"
RISK_DESCRIPTION = "GUI-configured meal and bolus entries"

# The glucose history the baseline template uses for every stage, patient and
# sensor alike. Not user-configurable this stage (settings/targets/schedules are
# explicitly out of scope), so it is a constant rather than a spec field.
GLUCOSE_HISTORY_POINTER = "reusable.glucose.flat_110_12hr"


@dataclass(frozen=True)
class Profile:
    """One T1 virtual-patient profile: its display name and its pointer token."""
    display: str
    token: str


# The four T1 profiles this stage generates, in the library's own order.
PROFILES: Tuple[Profile, ...] = (
    Profile("Median", "median"),
    Profile("Resistant", "resistant"),
    Profile("Adolescent", "adolescent"),
    Profile("Sensitive", "sensitive"),
)


@dataclass(frozen=True)
class Stage:
    """One of the three simulation stages a risk config defines.

    ``sim_id_prefix`` values are spellings ``severity_model.classify_sim_id``
    matches, so every generated stage lands in the results grid and the export.
    """
    sim_id_prefix: str
    loop_enabled: bool
    mitigated: bool


STAGES: Tuple[Stage, ...] = (
    Stage("pre-Loop_NoMitigations_t1_", loop_enabled=True, mitigated=False),
    Stage("pre-noLoop_t1_", loop_enabled=False, mitigated=False),
    Stage("post-Loop_WithMitigations_t1_", loop_enabled=True, mitigated=True),
)


@dataclass
class MealEntry:
    """One carb entry: when it starts, how long it absorbs, and its value input.

    ``value_input`` means whatever the configuration's mode says it means -- unused
    in standard mode, a multiplier of the profile baseline in multiplier mode, and a
    grams value in custom mode. ``duration_minutes`` of None omits ``duration`` from
    the JSON entirely, so the parser's own 180-minute default applies.
    """
    start_time: datetime.time
    duration_minutes: Optional[int] = None
    value_input: Optional[float] = None


@dataclass
class BolusEntry:
    """One bolus entry: when it is given and how much.

    ``value`` is either a numeric units dose or ``ACCEPT_RECOMMENDATION``.
    ``no_loop_units`` is the numeric dose the No Loop stage uses instead, and is
    required when ``value`` is the sentinel: that stage runs with ``controller: null``,
    so there is no recommendation to accept and the unresolved placeholder would
    silently deliver nothing.
    """
    time: datetime.time
    value: Union[float, str]
    no_loop_units: Optional[float] = None


@dataclass
class EntrySet:
    """The meal and bolus entries for one timeline (patient model or pump)."""
    meals: List[MealEntry] = field(default_factory=list)
    boluses: List[BolusEntry] = field(default_factory=list)


@dataclass
class MealConfigSpec:
    """A complete GUI-authored configuration, ready to generate configs from.

    ``pump`` is the same object as ``patient_model`` when the aligned toggle is on,
    so aligned mode cannot drift; independent mode supplies a second EntrySet.
    """
    mode: str
    patient_model: EntrySet
    pump: EntrySet

    @classmethod
    def aligned(cls, mode: str, entries: EntrySet) -> "MealConfigSpec":
        """Spec whose pump timeline is the same entry set as the patient model's."""
        return cls(mode=mode, patient_model=entries, pump=entries)


class MealConfigError(ValueError):
    """Raised when a spec cannot produce a schema-conformant config."""


# ---------------------------------------------------------------------------
# Library lookups -- every baseline number is READ, never hardcoded
# ---------------------------------------------------------------------------


def scenario_configs_root() -> str:
    """The scenario_configs/ root in effect: the env seam if set, else in-tree."""
    override = os.environ.get(SCENARIO_CONFIGS_ROOT_ENV)
    if override:
        return override
    return os.path.join(PROJECT_ROOT_DIR, "scenario_configs")


def _reusable_dir() -> str:
    return os.path.join(scenario_configs_root(), "tidepool_risk_v2", "reusable")


def _load_json(path: str) -> dict:
    with open(path) as handle:
        return json.load(handle)


def standard_carb_grams(profile: Profile) -> float:
    """The profile's standard-meal baseline, read from its reusable carb-dose file.

    Read rather than hardcoded so a change to the baseline library flows straight
    through to standard mode (and to multiplier mode, which is a factor of it).
    Raises MealConfigError naming the file if it is missing or empty.
    """
    path = os.path.join(_reusable_dir(), "carb_doses", f"{profile.token}_profile_v1.json")
    if not os.path.isfile(path):
        raise MealConfigError(f"Standard-meal baseline for {profile.display} not found: {path}")
    entries = _load_json(path)
    if not entries:
        raise MealConfigError(f"Standard-meal baseline for {profile.display} is empty: {path}")
    return float(entries[0]["value"])


def base_config_pointer(profile: Profile) -> str:
    """The ``reusable.*`` pointer for this profile's 2_0/swift base simulation."""
    return f"reusable.simulations.base_{profile.token}_2_0_v1"


def _base_config_path(profile: Profile) -> str:
    return os.path.join(
        _reusable_dir(), "simulations", "base", f"base_{profile.token}_2_0_v1.json"
    )


def simulation_window(profile: Profile = PROFILES[0]) -> Tuple[str, datetime.time, float]:
    """``(date_token, start_time, duration_hours)`` read from the base config.

    The date token is reproduced verbatim (the library writes "8/15/2019", not a
    zero-padded form) so generated entries look exactly like hand-written ones. All
    four T1 base configs share one window; the profile argument exists so that stays
    checkable rather than assumed.
    """
    path = _base_config_path(profile)
    if not os.path.isfile(path):
        raise MealConfigError(f"Base config for {profile.display} not found: {path}")
    base = _load_json(path)
    date_token, _, time_token = base["time_to_calculate_at"].partition(" ")
    return date_token, datetime.datetime.strptime(time_token, "%H:%M:%S").time(), float(base["duration_hours"])


# ---------------------------------------------------------------------------
# Validation -- mirrors ValueValidators, so bad input fails in the editor
# ---------------------------------------------------------------------------


def _window_bounds() -> Tuple[datetime.time, datetime.time]:
    _, start, duration_hours = simulation_window()
    start_dt = datetime.datetime.combine(datetime.date.today(), start)
    return start, (start_dt + datetime.timedelta(hours=duration_hours)).time()


def _validate_within_window(when: datetime.time, label: str) -> None:
    start, end = _window_bounds()
    if not start <= when <= end:
        raise MealConfigError(
            f"{label} {when.strftime('%H:%M:%S')} is outside the simulation window "
            f"{start.strftime('%H:%M:%S')}-{end.strftime('%H:%M:%S')}."
        )


def _validate_multiplier(multiplier: Optional[float]) -> float:
    if multiplier is None:
        raise MealConfigError("Multiplier mode needs a multiplier for every meal entry.")
    multiplier = float(multiplier)
    if multiplier <= 0:
        raise MealConfigError(f"Multiplier must be greater than 0, got {multiplier}.")
    if abs(multiplier / MULTIPLIER_STEP - round(multiplier / MULTIPLIER_STEP)) > 1e-9:
        raise MealConfigError(
            f"Multiplier must be a multiple of {MULTIPLIER_STEP}, got {multiplier}."
        )
    return multiplier


def _validate_grams(grams: float, label: str) -> float:
    if not 0 < grams <= CARB_GRAMS_MAX:
        raise MealConfigError(
            f"{label} resolves to {grams} g, outside the allowed 0-{CARB_GRAMS_MAX:g} g."
        )
    return grams


def _validate_duration(duration_minutes: Optional[int]) -> Optional[int]:
    if duration_minutes is None:
        return None
    if not 0 < float(duration_minutes) <= CARB_DURATION_MINUTES_MAX:
        raise MealConfigError(
            f"Absorption duration must be between 0 and {CARB_DURATION_MINUTES_MAX:g} "
            f"minutes, got {duration_minutes}."
        )
    return int(duration_minutes)


def _validate_bolus_units(units: Optional[float], label: str) -> float:
    if units is None:
        raise MealConfigError(f"{label} needs a numeric units value.")
    units = float(units)
    if not 0 <= units <= BOLUS_UNITS_MAX:
        raise MealConfigError(
            f"{label} must be between 0 and {BOLUS_UNITS_MAX:g} units, got {units}."
        )
    return units


def _validate_mode(mode: str) -> str:
    if mode not in MEAL_MODES:
        raise MealConfigError(f"Unknown meal mode {mode!r}; expected one of {MEAL_MODES}.")
    return mode


# ---------------------------------------------------------------------------
# Entry resolution
# ---------------------------------------------------------------------------


def resolve_carb_grams(mode: str, profile: Profile, value_input: Optional[float]) -> float:
    """The grams this meal entry becomes for this profile, under this mode.

    Standard uses the profile baseline as-is; multiplier scales that baseline per
    profile; custom applies one user-entered value to all four profiles identically.
    """
    _validate_mode(mode)
    if mode == MODE_STANDARD:
        grams = standard_carb_grams(profile)
    elif mode == MODE_MULTIPLIER:
        grams = standard_carb_grams(profile) * _validate_multiplier(value_input)
    else:
        if value_input is None:
            raise MealConfigError("Custom mode needs a grams value for every meal entry.")
        grams = float(value_input)
    # Round away binary-float noise (0.75 * 33 and friends) without altering values.
    return _validate_grams(round(grams, 4), f"Meal for {profile.display}")


def _carb_entry_json(entry: MealEntry, profile: Profile, mode: str, date_token: str) -> dict:
    _validate_within_window(entry.start_time, "Meal start time")
    carb_entry = {
        "type": "carb",
        "start_time": f"{date_token} {entry.start_time.strftime('%H:%M:%S')}",
        "value": resolve_carb_grams(mode, profile, entry.value_input),
    }
    duration = _validate_duration(entry.duration_minutes)
    if duration is not None:
        carb_entry["duration"] = duration
    return carb_entry


def _bolus_entry_json(entry: BolusEntry, date_token: str, loop_enabled: bool) -> dict:
    """One bolus entry as JSON, resolved for whether Loop is running in this stage.

    In the No Loop stage the ``accept_recommendation`` sentinel is replaced by the
    entry's ``no_loop_units``: with ``controller: null`` nothing ever resolves the
    placeholder, so leaving it in place would run that stage with no insulin at all.
    """
    _validate_within_window(entry.time, "Bolus time")
    time_token = f"{date_token} {entry.time.strftime('%H:%M:%S')}"
    if entry.value == ACCEPT_RECOMMENDATION:
        if loop_enabled:
            return {"time": time_token, "value": ACCEPT_RECOMMENDATION}
        return {
            "time": time_token,
            "value": _validate_bolus_units(
                entry.no_loop_units,
                f"The No Loop stage's bolus at {entry.time.strftime('%H:%M:%S')}",
            ),
        }
    return {
        "time": time_token,
        "value": _validate_bolus_units(
            entry.value, f"The bolus at {entry.time.strftime('%H:%M:%S')}"
        ),
    }


def _pump_bolus_entries(entries: List[dict]) -> List[dict]:
    """The subset of bolus entries that may live on the pump timeline.

    ``accept_recommendation`` is patient-side only -- ConfigValidator rejects it
    under ``patient.pump.bolus_entries`` because the pump timeline is written
    verbatim into the Loop input JSON, where the string crashes the Swift bridge's
    Double decode. This is the same split the library's own baseline template makes.
    """
    return [entry for entry in entries if entry["value"] != ACCEPT_RECOMMENDATION]


# ---------------------------------------------------------------------------
# Config generation
# ---------------------------------------------------------------------------


def risk_id(now: Optional[datetime.datetime] = None) -> str:
    """``TLR-YYYYMMDD-HHMMSS`` for the generation timestamp."""
    now = now if now is not None else datetime.datetime.now()
    return RISK_ID_PREFIX + now.strftime(RISK_ID_TIMESTAMP_FORMAT)


def config_filename(generated_risk_id: str, profile: Profile) -> str:
    """``Simulation-Configuration-<risk_id>_<Profile>_Profile.json``.

    Follows the library's convention closely enough that the app's existing
    ``_profile_label`` reads the profile straight back out of it, so the results
    grid and the exported chart names need no special case.
    """
    return f"Simulation-Configuration-{generated_risk_id}_{profile.display}_Profile.json"


def _stage_override(
    stage: Stage,
    profile: Profile,
    spec: MealConfigSpec,
    date_token: str,
) -> dict:
    patient_carbs = [
        _carb_entry_json(meal, profile, spec.mode, date_token) for meal in spec.patient_model.meals
    ]
    pump_carbs = [
        _carb_entry_json(meal, profile, spec.mode, date_token) for meal in spec.pump.meals
    ]
    patient_boluses = [
        _bolus_entry_json(bolus, date_token, stage.loop_enabled)
        for bolus in spec.patient_model.boluses
    ]
    pump_boluses = _pump_bolus_entries([
        _bolus_entry_json(bolus, date_token, stage.loop_enabled) for bolus in spec.pump.boluses
    ])

    pump = {"carb_entries": pump_carbs, "bolus_entries": pump_boluses}
    if stage.mitigated:
        pump["target_range"] = (
            f"reusable.mitigations.guardrails.target_range_{profile.token}_v1"
        )

    override = {
        "sim_id": stage.sim_id_prefix + profile.token,
        "patient": {
            "patient_model": {
                "glucose_history": GLUCOSE_HISTORY_POINTER,
                "carb_entries": patient_carbs,
                "bolus_entries": patient_boluses,
            },
            "pump": pump,
            "sensor": {"glucose_history": GLUCOSE_HISTORY_POINTER},
        },
    }
    if not stage.loop_enabled:
        override["controller"] = None
    elif stage.mitigated:
        override["controller"] = {
            "settings": (
                f"reusable.mitigations.guardrails.controller_settings_{profile.token}_swift"
            )
        }
    return override


def generate_config(spec: MealConfigSpec, generated_risk_id: str, profile: Profile) -> dict:
    """The complete scenario config for one profile. Pure apart from library reads."""
    _validate_mode(spec.mode)
    if not spec.patient_model.meals and not spec.patient_model.boluses:
        raise MealConfigError("Add at least one meal or bolus entry before generating configs.")
    date_token, _, _ = simulation_window(profile)
    return {
        "metadata": {
            "risk_id": generated_risk_id,
            "simulation_id": f"{generated_risk_id}-{profile.token}",
            "risk_description": RISK_DESCRIPTION,
            "config_format_version": CONFIG_FORMAT_VERSION,
        },
        "base_config": base_config_pointer(profile),
        "override_config": [
            _stage_override(stage, profile, spec, date_token) for stage in STAGES
        ],
    }


def generate_configs(spec: MealConfigSpec, generated_risk_id: str) -> Dict[str, dict]:
    """``{filename: config}`` -- one config per T1 profile, for this spec."""
    return {
        config_filename(generated_risk_id, profile): generate_config(spec, generated_risk_id, profile)
        for profile in PROFILES
    }


def config_bytes(config: dict) -> bytes:
    """A generated config serialized exactly as it is written to disk."""
    return (json.dumps(config, indent=2) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Writing a runnable, throwaway config library
# ---------------------------------------------------------------------------

# Name of the throwaway collection the generated risk directory sits in. It never
# reaches the library selector -- run_risk_assessment is handed this path directly.
GENERATED_COLLECTION_NAME = "generated"


def write_config_library(
    configs: Dict[str, dict],
    generated_risk_id: str,
    dest_root: str,
) -> str:
    """Write the configs into a library-shaped tree under dest_root; return config_dir.

    Layout mirrors the real library from the ``tidepool_risk_v2`` level down::

        <dest_root>/tidepool_risk_v2/reusable            -> symlink to the real one
        <dest_root>/tidepool_risk_v2/loop_risk_v2_0/generated/<risk_id>/*.json

    so ``reusable.*`` pointer resolution and ``gui_runner._find_pointer_object_dir``
    work exactly as they do for a real collection. The returned path is the
    collection directory -- pass it to ``run_risk_assessment`` as ``config_dir``,
    with ``generated_risk_id`` as ``target_risk_dir``.

    An existing tree at the same location is replaced, so regenerating never leaves a
    stale config behind for the run to pick up.
    """
    library_root = os.path.join(dest_root, "tidepool_risk_v2")
    collection_dir = os.path.join(library_root, "loop_risk_v2_0", GENERATED_COLLECTION_NAME)
    risk_dir = os.path.join(collection_dir, generated_risk_id)
    if os.path.exists(risk_dir):
        shutil.rmtree(risk_dir)
    os.makedirs(risk_dir)

    reusable_link = os.path.join(library_root, "reusable")
    if not os.path.exists(reusable_link):
        os.symlink(_reusable_dir(), reusable_link)

    for filename, config in configs.items():
        with open(os.path.join(risk_dir, filename), "wb") as handle:
            handle.write(config_bytes(config))
    return collection_dir
