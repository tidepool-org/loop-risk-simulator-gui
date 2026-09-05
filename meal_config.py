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

Simulation length is the user's too (TRSET-13): one ``duration_hours`` chosen for
the whole configuration and written into every ``override_config`` entry, where it
replaces the base config's own. ``OverrideItem`` already declares the field and the
key already exists in base, so ``resolve_override`` applies it with no parser or
schema change. Window arithmetic here is datetime-aware throughout -- at 23 or 24
hours a run passes midnight, and a time-of-day end wraps to before its own start.

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

# The library's own datetime spelling, mirrored from scenario_json_parser_v2's
# DATETIME_FORMAT rather than imported: that module is the schema authority and
# importing it here would drag the whole simulator into a streamlit-free helper.
LIBRARY_DATETIME_FORMAT = "%m/%d/%Y %H:%M:%S"

# Simulation duration (TRSET-13). One duration applies to the whole configuration --
# all three stages, all four profiles -- and is written into each override_config
# entry, where it replaces the base config's own duration_hours. The three presets
# are the investigative questions the ticket names; short-term exploration takes a
# free value inside the bounds below.
#
# These constants are the single source: streamlit_app builds its picker labels from
# them, so the app and the generator cannot disagree about what an option means (the
# same DRY reasoning as scenario_configs_root()).
DURATION_OVERDELIVERY_HOURS = 8.0
DURATION_UNDERDELIVERY_HOURS = 23.0
DURATION_FULL_DAY_HOURS = 24.0
PRESET_DURATION_HOURS: Tuple[float, ...] = (
    DURATION_OVERDELIVERY_HOURS,
    DURATION_UNDERDELIVERY_HOURS,
    DURATION_FULL_DAY_HOURS,
)

# Short-term exploration bounds. The maximum stops below the 8-hour preset rather
# than meeting it, so "short term" always means shorter than the default.
SHORT_TERM_MIN_HOURS = 2.0
SHORT_TERM_MAX_HOURS = 7.5
SHORT_TERM_STEP_HOURS = 0.5

# Below this a run is too short for LBGI and DKAI to mean anything: both are averages
# over the run, and the severity model reads them as if a full 8 hours produced them.
METRICS_VALID_MIN_HOURS = 8.0

# Stated verbatim wherever a sub-8-hour duration is offered or its results are shown,
# so the editor and the results pane cannot word the caveat differently.
SHORT_DURATION_CAVEAT = (
    "With a duration of less than 8 hours, LBGI and DKAI results are not valid. "
    "Use this shorter duration for discovering resulting glucose trace and dosing "
    "decisions only."
)

RISK_ID_PREFIX = "TLR-"
RISK_ID_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
CONFIG_FORMAT_VERSION = "v1.0"
RISK_DESCRIPTION = "GUI-configured meal and bolus entries"

# The author's own description is bounded here and nowhere else: the editor's
# text_area reads this for its max_chars, so the widget and the generator cannot
# disagree about the limit. Counted in code points, which is what both max_chars and
# len() count -- an emoji or a combining accent may cost more than one, so 250 is not
# a grapheme guarantee.
RISK_DESCRIPTION_MAX_CHARS = 250

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
class ControllerSettingsGroup:
    """One selectable Loop controller settings group (TRSET-15).

    ``token`` is the base-simulation filename suffix -- ``base_<profile>_<token>``
    -- so choosing a group is choosing which ``base_config`` pointer is written.
    ``subdir`` is where those files live under ``reusable/simulations/``; the
    pointer itself needs no subdirectory because ``load_pointer`` searches both,
    but reading the base window off disk does.

    ``temp_basal_only`` records that a group ships ``partial_application_factor``
    0.0 in its own settings file and therefore has no autobolus mode at all. It
    is a property of the released Loop version, not a UI preference, which is why
    it lives here rather than in the app.
    """
    display: str
    token: str
    subdir: str
    temp_basal_only: bool


# The selectable groups, default first. Loop 1.x is temp-basal only: 1dotX.json
# already carries partial_application_factor 0.0, and SwiftLoopController picks
# recommendationType from the truthiness of that value alone, so "1.x + autobolus"
# would mean overriding the very settings file the choice names -- and corresponds
# to no released Loop, since autobolus arrived with 2.x.
SETTINGS_GROUPS: Tuple[ControllerSettingsGroup, ...] = (
    ControllerSettingsGroup("Tidepool Loop 2.x", "2_0_v1", "base", temp_basal_only=False),
    ControllerSettingsGroup("Tidepool Loop 1.x", "1dotx", "1xComparator", temp_basal_only=True),
)
DEFAULT_SETTINGS_GROUP = SETTINGS_GROUPS[0]

# Dosing strategies, default first. These are display labels because they are also
# what lands in metadata (AC 9) -- one spelling, not a code token plus a label that
# can drift from it.
DOSING_AUTOBOLUS = "Autobolus"
DOSING_TEMP_BASAL = "Temp basal"
DOSING_STRATEGIES: Tuple[str, ...] = (DOSING_AUTOBOLUS, DOSING_TEMP_BASAL)
DEFAULT_DOSING_STRATEGY = DOSING_AUTOBOLUS

# What "temp basal" means as a config value: partial_application_factor 0.0, which
# is falsy, which is what SwiftLoopController reads to choose tempBasal. Only ever
# written for 2.x -- 1.x already has it.
TEMP_BASAL_PARTIAL_APPLICATION_FACTOR = 0.0

# Stated wherever Loop 1.x is offered, so the editor and the generator cannot word
# the coupling differently.
LOOP_1X_TEMP_BASAL_ONLY_NOTE = (
    "Tidepool Loop 1.x is temp-basal only, so Autobolus is not available with it."
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

    ``start_time`` is a full datetime, not a time of day (TRSET-13): at 23 or 24
    hours the simulation window crosses midnight, so which day an entry falls on is
    part of when it is. The editor composes start-day datetimes today; adding a day
    control changes what it composes, not this field.

    ``value_input`` means whatever the configuration's mode says it means -- unused
    in standard mode, a multiplier of the profile baseline in multiplier mode, and a
    grams value in custom mode. ``duration_minutes`` of None omits ``duration`` from
    the JSON entirely, so the parser's own 180-minute default applies.
    """
    start_time: datetime.datetime
    duration_minutes: Optional[int] = None
    value_input: Optional[float] = None


@dataclass
class BolusEntry:
    """One bolus entry: when it is given and how much.

    ``time`` is a full datetime, for the same reason ``MealEntry.start_time`` is.

    ``value`` is either a numeric units dose or ``ACCEPT_RECOMMENDATION``.
    ``no_loop_units`` is the numeric dose the No Loop stage uses instead, and is
    required when ``value`` is the sentinel: that stage runs with ``controller: null``,
    so there is no recommendation to accept and the unresolved placeholder would
    silently deliver nothing.
    """
    time: datetime.datetime
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

    ``duration_hours`` is one value for the whole configuration -- every stage and
    every profile get the identical number. None means the base config's own
    duration, so the library stays the single source for the default rather than a
    constant here restating it.

    ``risk_description`` is the author's own narrative of the hazardous situation being
    explored, written into every generated file's ``metadata``. None, empty or
    whitespace-only falls back to ``RISK_DESCRIPTION``, so a spec that says nothing
    produces exactly the output this module produced before the field existed.
    """
    mode: str
    patient_model: EntrySet
    pump: EntrySet
    duration_hours: Optional[float] = None
    risk_description: Optional[str] = None
    settings_group: ControllerSettingsGroup = DEFAULT_SETTINGS_GROUP
    dosing_strategy: str = DEFAULT_DOSING_STRATEGY

    @property
    def resolved_dosing_strategy(self) -> str:
        """The dosing strategy this spec actually generates with.

        A temp-basal-only settings group forces Temp basal regardless of what the
        field holds. Resolving here rather than in the app means a spec built
        directly -- by a test, or by any future caller -- cannot express the
        1.x + Autobolus combination that has no config to generate, and means the
        label written to metadata is the one the config actually reflects.
        """
        if self.settings_group.temp_basal_only:
            return DOSING_TEMP_BASAL
        return self.dosing_strategy

    @property
    def writes_temp_basal_override(self) -> bool:
        """Whether generation writes a partial_application_factor override.

        True only for a group whose settings file does not already say temp basal.
        1.x carries 0.0 in its own file, so overriding would restate what the base
        already resolves to (AC 6).
        """
        return (
            self.resolved_dosing_strategy == DOSING_TEMP_BASAL
            and not self.settings_group.temp_basal_only
        )

    @classmethod
    def aligned(
        cls,
        mode: str,
        entries: EntrySet,
        duration_hours: Optional[float] = None,
        risk_description: Optional[str] = None,
        settings_group: ControllerSettingsGroup = DEFAULT_SETTINGS_GROUP,
        dosing_strategy: str = DEFAULT_DOSING_STRATEGY,
    ) -> "MealConfigSpec":
        """Spec whose pump timeline is the same entry set as the patient model's."""
        return cls(
            mode=mode,
            patient_model=entries,
            pump=entries,
            duration_hours=duration_hours,
            risk_description=risk_description,
            settings_group=settings_group,
            dosing_strategy=dosing_strategy,
        )


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


def base_config_pointer(
    profile: Profile, settings_group: ControllerSettingsGroup = DEFAULT_SETTINGS_GROUP
) -> str:
    """The ``reusable.*`` pointer for this profile's base simulation in this group.

    No subdirectory in the pointer: ``load_pointer`` searches ``simulations/`` and
    each of its known subdirectories, so one spelling reaches both the 2.x bases in
    ``base/`` and the 1.x bases in ``1xComparator/``.
    """
    return f"reusable.simulations.base_{profile.token}_{settings_group.token}"


def _base_config_path(
    profile: Profile, settings_group: ControllerSettingsGroup = DEFAULT_SETTINGS_GROUP
) -> str:
    return os.path.join(
        _reusable_dir(),
        "simulations",
        settings_group.subdir,
        f"base_{profile.token}_{settings_group.token}.json",
    )


def guardrails_settings(profile: Profile) -> dict:
    """This profile's post-mitigation guardrails controller settings, read from disk.

    Read rather than restated for the same reason ``standard_carb_grams`` is: the
    inline form written for 2.x + Temp basal (AC 5) has to be the guardrails file
    plus one key, so that a change to the guardrails library flows through instead
    of being silently overridden by a constant here.
    """
    path = os.path.join(
        _reusable_dir(),
        "mitigations",
        "guardrails",
        f"controller_settings_{profile.token}_swift.json",
    )
    if not os.path.isfile(path):
        raise MealConfigError(
            f"Guardrails controller settings for {profile.display} not found: {path}"
        )
    settings = _load_json(path)
    if not settings:
        raise MealConfigError(
            f"Guardrails controller settings for {profile.display} is empty: {path}"
        )
    return settings


def autobolus_application_factor() -> float:
    """The fraction of a correction autobolus delivers, read from the 2.x settings.

    The UI states this as a percentage (AC 12). Read from the same file the 2.x
    base config points at, so the tooltip cannot claim a number the generated
    configs do not produce.
    """
    path = os.path.join(_reusable_dir(), "loop_settings", "2_0_v1.json")
    if not os.path.isfile(path):
        raise MealConfigError(f"Loop 2.x settings not found: {path}")
    return float(_load_json(path)["partial_application_factor"])


def base_window(profile: Profile = PROFILES[0]) -> Tuple[datetime.datetime, float]:
    """``(start, duration_hours)`` read from this profile's base config.

    All four T1 base configs share one window; the profile argument exists so that
    stays checkable rather than assumed.

    Deliberately read from the default (2.x) group rather than the selected one: the
    1.x bases carry the identical window, and the authoring window must not shift
    under the user when they change controller settings. If the two groups' windows
    ever diverge, this is the line that has to learn about the group.
    """
    path = _base_config_path(profile)
    if not os.path.isfile(path):
        raise MealConfigError(f"Base config for {profile.display} not found: {path}")
    base = _load_json(path)
    start = datetime.datetime.strptime(
        base["time_to_calculate_at"], LIBRARY_DATETIME_FORMAT
    )
    return start, float(base["duration_hours"])


def date_token(when: datetime.datetime) -> str:
    """The library's own date spelling: "8/15/2019", never a zero-padded "08/15/2019".

    Built by hand rather than with strftime, which offers no portable unpadded
    directive ("%-m" is not available everywhere and "%m" pads).
    """
    return f"{when.month}/{when.day}/{when.year}"


def entry_token(when: datetime.datetime) -> str:
    """One timestamp as the library writes them: ``M/D/YYYY HH:MM:SS``."""
    return f"{date_token(when)} {when.strftime('%H:%M:%S')}"


def validate_duration_hours(duration_hours: Optional[float]) -> float:
    """The duration a run may use, or MealConfigError naming the bound it broke."""
    if duration_hours is None:
        raise MealConfigError("A simulation duration is required.")
    hours = float(duration_hours)
    if hours in PRESET_DURATION_HOURS:
        return hours
    if not SHORT_TERM_MIN_HOURS <= hours <= SHORT_TERM_MAX_HOURS:
        raise MealConfigError(
            f"A short-term duration must be between {SHORT_TERM_MIN_HOURS:g} and "
            f"{SHORT_TERM_MAX_HOURS:g} hours, got {hours:g}."
        )
    if abs(hours / SHORT_TERM_STEP_HOURS - round(hours / SHORT_TERM_STEP_HOURS)) > 1e-9:
        raise MealConfigError(
            f"A short-term duration must be a multiple of {SHORT_TERM_STEP_HOURS:g} "
            f"hours, got {hours:g}."
        )
    return hours


def simulation_window(
    duration_hours: Optional[float] = None,
    profile: Profile = PROFILES[0],
) -> Tuple[datetime.datetime, datetime.datetime, float]:
    """``(start, end, duration_hours)`` for a run of this length on this profile.

    Datetimes throughout, and ``end`` is ``start + duration``. A ``datetime.time``
    end wraps once the run passes midnight -- 12:00 + 23h reads back as 11:00, and
    every ``start <= when <= end`` check silently becomes false. That was latent
    while 8 hours was the only duration; a selectable one triggers it.

    ``duration_hours`` of None means the base config's own, so the library stays the
    single source for the default rather than a constant here restating it.
    """
    start, base_duration = base_window(profile)
    hours = (
        base_duration if duration_hours is None else validate_duration_hours(duration_hours)
    )
    return start, start + datetime.timedelta(hours=hours), hours


def authoring_window(
    duration_hours: Optional[float] = None,
    profile: Profile = PROFILES[0],
) -> Tuple[datetime.datetime, datetime.datetime]:
    """``(earliest, latest)`` an entry can be AUTHORED at -- a view bound, not a rule.

    The editor has no day control yet, so it can only place entries on the start
    calendar day. Where the window crosses midnight the simulation still runs past
    this bound; there is simply nothing authorable out there.

    Validation deliberately does not use this: entries are checked against the real
    simulation window, so adding a day control widens what the view offers without
    touching what ``generate_config`` will accept.
    """
    start, end, _ = simulation_window(duration_hours, profile)
    end_of_start_day = datetime.datetime.combine(start.date(), datetime.time.max)
    return start, min(end, end_of_start_day)


def metrics_are_valid(duration_hours: float) -> bool:
    """Whether LBGI and DKAI mean anything for a run of this length.

    The one gate behind every "not valid" marking, so the editor's caveat and the
    results pane's cannot disagree about which runs it applies to.
    """
    return float(duration_hours) >= METRICS_VALID_MIN_HOURS


# ---------------------------------------------------------------------------
# Validation -- mirrors ValueValidators, so bad input fails in the editor
# ---------------------------------------------------------------------------


def _format_window(start: datetime.datetime, end: datetime.datetime) -> str:
    """The window as message text, dated on both ends only when it spans two days."""
    if start.date() == end.date():
        return (
            f"{date_token(start)} {start.strftime('%H:%M:%S')}-{end.strftime('%H:%M:%S')}"
        )
    return f"{entry_token(start)}-{entry_token(end)}"


def _validate_within_window(
    when: datetime.datetime,
    label: str,
    start: datetime.datetime,
    end: datetime.datetime,
) -> None:
    """Reject an entry outside the run, naming the entry and the window it missed.

    The bounds are passed in rather than recomputed so one duration is read once per
    generated config, and an entry is never checked against a different window than
    the one being written.
    """
    if not start <= when <= end:
        raise MealConfigError(
            f"{label} {entry_token(when)} is outside the simulation window "
            f"{_format_window(start, end)}."
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


def _validate_risk_description(text: Optional[str]) -> str:
    """The stripped description, or MealConfigError naming the limit it broke.

    Stripped before the limit is applied, so trailing whitespace can never cost a user
    a character. Unreachable from the editor -- the widget's ``max_chars`` blocks the
    text before it can arrive here -- and that is the point: this is the same mirrored
    bound every other ``_validate_*`` above is, so ``generate_config`` stays correct
    for a caller that is not the form.
    """
    stripped = (text or "").strip()
    if len(stripped) > RISK_DESCRIPTION_MAX_CHARS:
        raise MealConfigError(
            f"A risk description must be at most {RISK_DESCRIPTION_MAX_CHARS} "
            f"characters, got {len(stripped)}."
        )
    return stripped


# ---------------------------------------------------------------------------
# Entry resolution
# ---------------------------------------------------------------------------


def resolve_risk_description(text: Optional[str]) -> str:
    """What a description field actually writes: the stripped text, or the fallback.

    Public, and the single answer to that question, because two callers need it -- the
    generator, which writes it, and the editor, which compares a generated set against
    what the field would produce now. Were the editor to compare the raw field instead,
    an unfilled field would never equal the ``RISK_DESCRIPTION`` its own configs carry,
    and every set generated without a description would invalidate itself on the render
    immediately after it was generated.

    The fallback is the constant this key held before the field existed, so it still
    says something true -- the config *was* GUI-configured -- and a spec that names no
    description produces byte-identical output to the version before TRSET-36.
    """
    return _validate_risk_description(text) or RISK_DESCRIPTION


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


def _carb_entry_json(
    entry: MealEntry,
    profile: Profile,
    mode: str,
    window: Tuple[datetime.datetime, datetime.datetime],
) -> dict:
    _validate_within_window(entry.start_time, "Meal start time", *window)
    carb_entry = {
        "type": "carb",
        "start_time": entry_token(entry.start_time),
        "value": resolve_carb_grams(mode, profile, entry.value_input),
    }
    duration = _validate_duration(entry.duration_minutes)
    if duration is not None:
        carb_entry["duration"] = duration
    return carb_entry


def _bolus_entry_json(
    entry: BolusEntry,
    window: Tuple[datetime.datetime, datetime.datetime],
    loop_enabled: bool,
) -> dict:
    """One bolus entry as JSON, resolved for whether Loop is running in this stage.

    In the No Loop stage the ``accept_recommendation`` sentinel is replaced by the
    entry's ``no_loop_units``: with ``controller: null`` nothing ever resolves the
    placeholder, so leaving it in place would run that stage with no insulin at all.
    """
    _validate_within_window(entry.time, "Bolus time", *window)
    time_token = entry_token(entry.time)
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
    window: Tuple[datetime.datetime, datetime.datetime],
    duration_hours: float,
) -> dict:
    patient_carbs = [
        _carb_entry_json(meal, profile, spec.mode, window) for meal in spec.patient_model.meals
    ]
    pump_carbs = [
        _carb_entry_json(meal, profile, spec.mode, window) for meal in spec.pump.meals
    ]
    patient_boluses = [
        _bolus_entry_json(bolus, window, stage.loop_enabled)
        for bolus in spec.patient_model.boluses
    ]
    pump_boluses = _pump_bolus_entries([
        _bolus_entry_json(bolus, window, stage.loop_enabled) for bolus in spec.pump.boluses
    ])

    pump = {"carb_entries": pump_carbs, "bolus_entries": pump_boluses}
    if stage.mitigated:
        pump["target_range"] = (
            f"reusable.mitigations.guardrails.target_range_{profile.token}_v1"
        )

    override = {
        "sim_id": stage.sim_id_prefix + profile.token,
        # Written into EVERY override entry, not once at the top: the parser reads
        # duration_hours off the merged per-sim config, and the key already exists in
        # base, so resolve_override replaces it with no schema or parser change.
        "duration_hours": duration_hours,
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
        # The No Loop stage is untouched by the controller-settings choice (AC 8):
        # there is no controller to configure.
        override["controller"] = None
    elif stage.mitigated:
        override["controller"] = {"settings": _mitigated_settings(profile, spec)}
    elif spec.writes_temp_basal_override:
        # The pre-mitigation stage has no controller block at all by default -- it
        # inherits the base config's. Temp basal on 2.x is the one selection that
        # gives it one, carrying the single overriding key.
        override["controller"] = {
            "settings": {
                "partial_application_factor": TEMP_BASAL_PARTIAL_APPLICATION_FACTOR
            }
        }
    return override


def _mitigated_settings(profile: Profile, spec: MealConfigSpec) -> Union[str, dict]:
    """The post-mitigation stage's ``controller.settings`` for this selection.

    Normally the guardrails pointer string, exactly as before this ticket. The one
    exception is 2.x + Temp basal, which needs the guardrails values AND the
    overriding factor in a single settings value -- a pointer cannot express both,
    so the file is read and inlined with the extra key (AC 5).

    Read, never restated: hardcoding the guardrails numbers would keep this passing
    after the library changed underneath it.
    """
    pointer = f"reusable.mitigations.guardrails.controller_settings_{profile.token}_swift"
    if not spec.writes_temp_basal_override:
        return pointer
    settings = dict(guardrails_settings(profile))
    settings["partial_application_factor"] = TEMP_BASAL_PARTIAL_APPLICATION_FACTOR
    return settings


def generate_config(spec: MealConfigSpec, generated_risk_id: str, profile: Profile) -> dict:
    """The complete scenario config for one profile. Pure apart from library reads."""
    _validate_mode(spec.mode)
    risk_description = resolve_risk_description(spec.risk_description)
    if not spec.patient_model.meals and not spec.patient_model.boluses:
        raise MealConfigError("Add at least one meal or bolus entry before generating configs.")
    start, end, duration_hours = simulation_window(spec.duration_hours, profile)
    return {
        "metadata": {
            "risk_id": generated_risk_id,
            "simulation_id": f"{generated_risk_id}-{profile.token}",
            "risk_description": risk_description,
            "config_format_version": CONFIG_FORMAT_VERSION,
            # The two TRSET-15 axes, as their displayed labels (AC 9), so the
            # generated file records which controller it was built for and the
            # summary can echo it back from the JSON rather than the widgets.
            # Safe to add: every schema_models model sets extra="allow", and
            # ScenarioParserV2 reads only metadata["simulation_id"].
            "controller_settings_group": spec.settings_group.display,
            "dosing_strategy": spec.resolved_dosing_strategy,
        },
        "base_config": base_config_pointer(profile, spec.settings_group),
        "override_config": [
            _stage_override(stage, profile, spec, (start, end), duration_hours)
            for stage in STAGES
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
