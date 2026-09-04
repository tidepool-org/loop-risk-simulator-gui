"""One-off: capture the pre-TRSET-15 generated-config baseline for AC 4.

Run on unmodified main. Writes four JSON files that later become the
comparison target proving TRSET-15's default path did not change output.
"""
import datetime
import os

import meal_config

# Fixed, not datetime.now(): the risk id lands in metadata.simulation_id, so a
# generated-at-runtime value would differ on every run and make comparison useless.
RISK_ID = "TLR-20260904-120000"

# The base configs run 8/15/2019 12:00 for 8 hours. One meal an hour in.
MEAL_TIME = datetime.datetime(2019, 8, 15, 13, 0)

OUT = os.path.join("tests", "test_data", "trset15_pre_change")

spec = meal_config.MealConfigSpec.aligned(
    meal_config.MODE_STANDARD,
    meal_config.EntrySet(meals=[meal_config.MealEntry(MEAL_TIME)]),
    duration_hours=meal_config.DURATION_OVERDELIVERY_HOURS,
)

configs = meal_config.generate_configs(spec, RISK_ID)

os.makedirs(OUT, exist_ok=True)
for filename, config in configs.items():
    with open(os.path.join(OUT, filename), "wb") as handle:
        handle.write(meal_config.config_bytes(config))
    print("wrote", filename)