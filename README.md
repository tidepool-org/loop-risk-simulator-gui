# loop-risk-simulator-gui

Streamlit MVP for running a Tidepool Loop risk assessment without a terminal.
This is the view layer only — all simulator/validation logic lives in
`gui_runner.py`, which stays in the
[`data-science-simulator`](https://github.com/tidepool-org/data-science-simulator)
repo as the sanctioned in-process service layer.

## Dependency model (Phase 4): pinned, not sibling

The simulator is consumed as a **pinned git ref**, not an editable sibling
checkout — the two repos no longer need to be cloned side by side.
`conda-environment.yml` installs
`git+https://github.com/tidepool-org/data-science-simulator@main`
and is the single source of truth for the pins. The simulator is tracked on
`main` (not a fixed tag) because this GUI is an exploration wrapper only —
updates flow in on the next env rebuild with no re-tag or re-pin.

Two simulator paths — `post_processing/severity_model.py` and
`scenario_configs/` — are **not** part of the installed package. `gui_runner`
does `import severity_model`, and `ScenarioParserV2` resolves `reusable.*`
pointers from a path hardcoded relative to its own module
(`<package>/../../scenario_configs/…`, not any env var), so both paths must sit
**beside the installed package**. The packaged bundle vendors them from the same
pinned tag and its launcher symlinks them into `site-packages`
(`scenario_configs/` and a top-level `severity_model.py`), making every
resolution native. For a dev checkout, an editable install already puts them in
the right place; `LOOP_RISK_GUI_SCENARIO_CONFIGS_ROOT` /
`LOOP_RISK_GUI_POST_PROCESSING_DIR` can redirect browsing/tests if needed.

## Setup (development)

Requires an **arm64** conda (matches the committed arm64 `.dylib` in the
simulator — never use ambient `conda` or `uv run`):

```bash
conda env create -f conda-environment.yml
conda activate loop-risk-simulator-gui
```

## Running the app

```bash
streamlit run streamlit_app.py
```

## Packaging a release bundle (maintainer)

Build a versioned, self-contained macOS bundle (pinned env spec + vendored
`LoopAlgorithmToPython` + vendored simulator orphan paths + app code + launcher),
then publish it as a GitHub Release asset:

```bash
python packaging/build_bundle.py build \
  --version 0.1.0 \
  --simulator-ref main \
  --simulator-repo ../data-science-simulator \
  --swift-repo ../LoopAlgorithmToPython \
  --output-dir dist/
```

The builder prints the exact `gh release create …` command to publish the
archive — publishing is a deliberate, separate step, never run automatically.
The bundle's `run_simulator_gui.command` establishes the arm64 env from the
pinned spec, builds the Swift `.dylib` on first run, symlinks the vendored paths
beside the package, and launches the app. (The polished colleague-facing
launcher UX is Phase 5.)

## Running tests

```bash
python -m pytest tests/                 # unit + Phase-3 integration (arm64 env)
```

The Phase-4 bundle-boundary test (`tests/test_phase4_bundle_integration.py`) is
opt-in — it needs a built, extracted bundle. Run it with the **bundle env's**
interpreter and `LOOP_RISK_GUI_BUNDLE_DIR` set to the extracted bundle (see the
module docstring). Run everything with the conda env's own interpreter, never
`uv run` — the simulator and its deps are only available there.

## Phase 4 change summary

**What changed (≤100 words):** Replaced the Phase-3 `-e ../data-science-simulator`
editable-sibling install with a **pinned git tag**, removing the "clone as
siblings" constraint. Added `packaging/build_bundle.py`, which produces a
versioned macOS bundle: it renders the pinned env spec from this repo's
`conda-environment.yml`, extracts the two non-packaged simulator paths
(`severity_model.py`, `scenario_configs/` incl. `reusable/`) from the same tag,
vendors `LoopAlgorithmToPython` source, stamps provenance, and emits the publish
command. The launcher symlinks the vendored paths beside the package so
resolution is native. Phase-3 integration fixtures moved to a self-contained
temp library.

**Validation (≤100 words):** Packaging logic covered by unit tests (pin
rendering, version stamp, staging assembly — git boundary mocked). All six
Phase-3 integration cases pass from the relocated temp-library fixtures, writing
nothing into the installed library. A bundle-boundary integration test builds a
bundle, installs it into a fresh arm64 env with **no sibling checkout**, and
asserts the pinned (non-editable) simulator imports, `severity_model` resolves,
`scenario_configs`/`reusable` resolve beside the package, the version stamp
matches the built tag, and a real `TLR-QAE-482-test` run completes with a
populated assessment and non-blank PNGs.

**Cautions / limitations:** arm64 only (matches the committed `.dylib`); never
`uv run`. First run builds the Swift `.dylib` and a full conda env (minutes) and
needs GitHub connectivity to resolve the pinned deps. The bundle vendors the
whole `reusable/` subtree (~18 MB). The launcher writes two symlinks into the
env's `site-packages`. `.app` freeze/sign/notarization and the colleague-facing
launcher UX are out of scope (later phases).

**Breaking change + migration:** The install/dev-setup contract changed —
siblings are no longer required and the env-spec format moved from `-e ../…` to
`git+…@tag`. To migrate: recreate the conda env from the updated
`conda-environment.yml`. For a live-checkout dev loop, use an editable install or
set `LOOP_RISK_GUI_SCENARIO_CONFIGS_ROOT` / `LOOP_RISK_GUI_POST_PROCESSING_DIR`.

**Rollback (High regression risk):** Revert the pinned
`data-science-simulator` line in `conda-environment.yml` back to
`- -e ../data-science-simulator`, re-clone the two repos as siblings, and
recreate the env. That restores the exact Phase-3 editable-sibling behavior.

## Accessibility tests (TRSET-4)

`tests/test_accessibility.py` adds a regression-guarding layer for three basic
WCAG requirements against the app's own tokens and emitted markup — no browser,
Playwright, Selenium, or axe-core:

- **1.4.3 Contrast (unit):** a small pure-Python `contrast_ratio()` helper
  computes WCAG ratios for the text pairs the app renders. Tokens are read from
  their real sources (`.streamlit/config.toml` and `streamlit_app._BRAND_CSS`),
  so the ratios re-derive if a token changes. Gated pairs — `textColor` on
  `backgroundColor` (~15.9:1) and the CSS-override text on
  `secondaryBackgroundColor` (~14.7:1) — must clear 4.5:1 (normal text).
- **2.1 Keyboard & 1.3 Adaptable (rendered smoke):** via the existing
  `AppTest.from_file` harness — interactive widgets carry accessible labels,
  emitted `<img>`s keep alt text (guards the TRSET-3 logo), the app's
  `unsafe_allow_html` blocks add no positive `tabindex`, and severity info is
  conveyed as text, not color alone. A full-run integration test asserts all
  three markers at the rendered-tree boundary against the real config library.

Example:

```bash
python -m pytest tests/test_accessibility.py     # arm64 conda env, never `uv run`
```

**Known finding (not a gate):** brand `primaryColor` `#627CFF` is ~3.6:1 on
white — below the 4.5:1 normal-text minimum. It is **excluded** from the
pass/fail gate per adjudication (brand color; remediation is a separate ticket);
one test documents the known value and flags loudly only if it shifts out of
band. Regression risk for this change is Low (additive, GUI-repo test layer;
no simulator or `gui_runner` change), so no rollback note applies.

## Disclaimer banner (TRSET-5)

**What changed (≤100 words):** Added a persistent regulatory disclaimer at the
top of the main page — a custom-styled `role="alert"` caution box (rendered via
`st.markdown(..., unsafe_allow_html=True)`, above the logo/header on every load)
stating the tool is not medical software and must not drive insulin dosing
decisions. The wording lives in a single `DISCLAIMER_TEXT` constant. Colors
reuse the existing Tidepool palette — no new brand colors: background is the
theme's `secondaryBackgroundColor` (`#281946`), text is the `_BRAND_CSS`
override color (`#F5F5FA`). Styling is inline on the element, leaving `_BRAND_CSS`
untouched. It is a distinct box, not `st.warning`.

Example:

```bash
streamlit run streamlit_app.py     # banner shows at the top of the page
```

**Validation (≤100 words):** Extends the existing `AppTest.from_file` harness —
no browser/Playwright/Selenium. `test_streamlit_app.py` adds a full-run
integration test asserting the banner renders once with the exact verbatim text,
carries the ⚠ icon and `role="alert"`, and precedes the logo `<img>`; plus a
guard that it does not collide with `at.warning`. `test_accessibility.py` adds
the banner's `#F5F5FA`-on-`#281946` pair to the contrast gate (~14.7:1 ≥ 4.5:1),
asserts it reuses existing palette tokens (never `#627CFF`), and checks the alert
is conveyed by text + semantics, not color alone. Full suite green (22 tests).

**Cautions / limitations:** Presentation layer only — no change to `gui_runner`,
the Loop algorithm interface, the scenario-config schema, or RTF output. Banner
is on the main page only, not exported artifacts. Regression risk Low–Medium
(shares the render path and interacts with the TRSET-4 contrast gate, but reuses
already-gated tokens); not a breaking change, so no rollback note applies.

## Loop home-screen charts in the results pane (TRSET-23)

**What changed (≤100 words):** The results pane no longer renders the generic
three-panel simulator PNGs. Each TLR-* expander now shows **one row per VP
profile**, each row three co-equal columns — Pre-mitigation | No Loop |
Post-mitigation — of TRSET-22 Loop-home-screen charts, read from that sim's
`<sim_id>.tsv` via TRSET-21's `read_trace`. `gui_runner.RiskDirRunResult` gains
an additive `trace_paths` field (`scenario_config_filename -> {sim_id: tsv}`)
that supplies them. Stage identity comes from `severity_model.classify_sim_id`
and the order/labels from `STAGE_ORDER`/`STAGE_DISPLAY`, re-exported through
`gui_runner` so the view layer no longer redeclares them.

Example:

```bash
streamlit run streamlit_app.py     # run a TLR directory; charts appear under its metrics table
```

**Validation (≤100 words):** An integration test runs the real
`run_risk_assessment` on the real `test/TLR-QAE-482-test` config directory
(copied into a temp library), then drives the returned trace paths through the
real `read_trace` → `render_loop_home_screen`, asserting non-blank 900×1100 PNGs,
and re-renders the resulting `RunResult` through `AppTest` to assert one chart per
present stage. That directory genuinely defines no `no_loop` stage, so the "No
data" placeholder is exercised against real data. Unit tests cover the profile
label parser, the (profile → stage) grouping, and the placeholder/error branches.
Full GUI suite: 108 passed, 7 skipped.

**Cautions / limitations:** The three stages are always drawn side by side as
equals with no selector and no auto-collapsing (TWI-0006 §2.g.ii). A missing
stage renders an explicit "No data"; an unreadable trace renders its own message
so a failed read stays distinguishable from an absent stage. PNG bytes are
`st.cache_data`-cached by trace path, since Streamlit re-executes the script on
every interaction and one directory is three charts per profile. `png_paths` and
`plot_sim_results` plumbing remains in `gui_runner` but is no longer rendered —
removing it is a follow-up. **Known limitation:** `classify_sim_id` does not
match three post-stage prefix spellings present in the library
(`post-Loop_withMitigations_`, `post-Loop-withMitigations_`,
`post_Loop_WithMitigations_`), so those directories show "No data" in the
Post-mitigation column — the same pre-existing gap their metrics-table row
already has; filed separately, not fixed here. Regression risk Medium (shared
runner entry point plus the app's main render path), contained by the
`gui_runner` change being purely additive: no existing field, signature, or
schema changed, so this is not a breaking change and no rollback note applies.

## Exportable results (TRSET-7)

**What changed (≤100 words):** A completed run now offers an **Export results**
control that produces one zip and hands it over as a browser download. The zip
nests everything under a single `risk_run_<timestamp>/` folder: every raw output
the run wrote (summary CSVs, `<sim_id>.tsv` traces, the simulator figures,
`loop_algo_io/`, `metadata.json`), the `risk_summary_<sim_id>.rtf` severity
summaries — generated at export time by the unmodified
`create_severity_summary.process_results_directory` — and a `charts/` folder of
the Loop home-screen PNGs, one per VP profile × stage. Assembly lives in the new
streamlit-free `export_bundle.py`; `gui_runner` gains a `metadata.json` write and
re-exports the RTF renderer.

Example:

```bash
streamlit run streamlit_app.py     # run a directory, then: Export results -> Download export (.zip)
```

Chart files are named `<TLR dir>_<profile>_<stage>.png` (e.g.
`TLR-909_Adolescent_profile_Pre-mitigation.png`), sanitized to
`[A-Za-z0-9._-]`; a stage with no trace, or an unreadable one, is skipped and
listed in an on-screen warning rather than exported as a blank chart.

**Validation (≤100 words):** An integration test runs the real
`run_risk_assessment` on the real `test/TLR-QAE-482-test` config (copied into a
temp library), exports it for real, and asserts **at the zip boundary**:
`metadata.json` dated like the displayed assessment, an RTF whose bytes are
identical to the one on disk, `charts/` holding exactly the two PNGs (900×1100)
for the stages that directory genuinely defines and none for the `no_loop` stage
it lacks, the raw outputs present, and nothing from outside `save_dir`. It then
drives the whole thing through `AppTest`. Unit tests cover naming/sanitization,
archive layout, the guard rails, and the control's states. GUI suite: 138
passed, 7 skipped. Simulator `test_gui_runner` 16/16, RTF renderer suite 32/32
unchanged.

**Cautions / limitations:** Export is two clicks by necessity —
`st.download_button` materializes its payload at render time, so a one-click
version would rebuild the zip on every rerun. The zip is built to a
session-scoped `tempfile` directory and written file-by-file (never assembled in
memory); its bytes are read once for the download and cached by path. A
cancelled run is not exportable, and a run directory missing `metadata.json` or
any `TLR-*` dir raises rather than shipping a summary-free zip — the two cases
`process_results_directory` otherwise only prints and returns on. Charts are
loose files: the filename carries their identity, not the app's profile × stage
grid. The `classify_sim_id` prefix gap noted under TRSET-23 applies here too — a
stage it cannot classify has no chart in the export, listed as skipped.
Regression risk Medium (GUI run path plus run-directory contents, which now also
carry `metadata.json` and the RTFs); additive only — no existing field,
signature, schema, or RTF byte changed — so this is not a breaking change and no
rollback note applies.

## Configurable meal entries (TRSET-9)

**What changed (≤100 words):** The app gained a second config source. Alongside
*Choose from the config library*, **Configure meals & boluses** lets a user define
meal and bolus entries directly and generate four runnable scenario configs — one per
T1 profile (Median, Resistant, Adolescent, Sensitive) — with `TLR-<YYYYMMDD-HHMMSS>`
as the risk id. Meal values come from one of three modes: the per-profile standard,
a 0.25-step multiplier of it, or one custom grams value. Generation lives in the new
streamlit-free `meal_config.py`; `export_bundle` gained an optional
`generated_configs` parameter. `gui_runner` is unchanged.

Example:

```bash
streamlit run streamlit_app.py     # Config source -> Configure meals & boluses -> Generate configs -> Run Tool
```

The generated configs are written to a session temp directory laid out like the
library (with `reusable/` symlinked so pointer resolution works as it does for a real
collection) and handed to `run_risk_assessment` as its `config_dir` — the
"configure parameters directly" mode `streamlit_app.py`'s docstring reserves.
**Nothing is ever written into `scenario_configs/`.** They are downloadable on their
own (`<risk_id>_configs.zip`, nested under one `<risk_id>/` folder) and ride along in
the run export under `generated_configs/`.

Generated configs use the library's own baseline template — the 2_0/swift base
configs (`reusable.simulations.base_<profile>_2_0_v1`), `flat_110_12hr` glucose, and
the post stage's `target_range_<profile>_v1` + `controller_settings_<profile>_swift`
guardrails. Only `carb_entries` / `bolus_entries` come from the user. The three
sim_ids (`pre-Loop_NoMitigations_`, `pre-noLoop_`, `post-Loop_WithMitigations_`) all
classify through `severity_model.classify_sim_id`, so every stage reaches the results
grid and the export.

**Validation (≤100 words):** A single integration test drives the whole feature
through the real app with no mocks: configure → generate → `validate_config_dir`
(zero errors) → a real four-profile Swift run → export. It asserts at the boundaries —
the JSON on disk, the `<sim_id>_override_config.json` files the run itself wrote, and
the zip. Multiplier 2.0 really produced 62/66/120/50 g in the simulations; the
duration-less meal really resolved to 180 minutes in the real parser; the No Loop
stage really ran on its numeric bolus. 20 integration + 45 unit tests, ~53 s.
Full GUI suite: 213 passed, 7 skipped.

**Cautions / limitations:** Entries must fall inside the base configs' simulation
window (`8/15/2019 12:00` + 8 h) — that window and the standard baselines
(31/33/60/25 g) are **read** from the library at generation time, never hardcoded, so
a library change flows straight through. `accept_recommendation` is written to
`patient.patient_model` only: `ConfigValidator` rejects it under
`patient.pump.bolus_entries`, where it would reach the Loop input JSON verbatim and
crash the Swift bridge's `Double` decode. Because the No Loop stage has no controller
to make a recommendation, any sentinel bolus must carry a numeric units value for
that stage; a blank dose is an error, never a silent 0 U. The mode is per
configuration but the value input is per meal row. Only settings/targets/schedules
and the t2 profiles remain out of scope (later stages). The scenario-config schema,
`scenario_json_parser_v2.py` and the Loop algorithm interface are untouched.
Regression risk Medium (shares `validate_config_dir` / `run_risk_assessment` / the
export control, and adds a second way to reach them); additive only — `gui_runner` is
unchanged and `build_export_zip`'s new parameter is keyword-defaulted — so this is
not a breaking change and no rollback note applies.

**One TRSET-4 follow-through:** the editor introduced `st.time_input`, whose value
box sits on `secondaryBackgroundColor` but was not in `_BRAND_CSS`'s text-color
override — the entered time rendered `#281946` on `#281946`, an invisible field
(found in a running app, not by inspection). It needed its own selector rather than
the existing `role="group"`/`input` ones, because its value is a `div` inside a
baseweb select. `test_accessibility.py` gained a guard that derives the widget types
the app actually renders and fails if one is absent from the override; the guard was
confirmed to fail without the fix. It checks presence, not cascade — resolving the
cascade needs a browser, which that suite deliberately avoids.

**Post-release fix — results never outlive their selection:** as first shipped, the
results pane rendered unconditionally at the bottom of the page, so a previous run's
expanders and charts stayed on screen after switching config source or generating a
new config set. That misled in practice: a library physical-activity directory
(`TLR-1117_bike` — no meals, so an empty Active Carbohydrates panel by construction)
was still displayed under a freshly generated meal configuration and read as that
configuration's output. The displayed run is now cleared whenever the
selection behind it changes — switching config source, generating a new config set,
or picking another collection or TLR-* directory. One check (`_sync_selection`) covers
every case, keyed on `(config source, config dir, target TLR dir)`, so the results pane
always matches the current selection or is empty. It is deliberately keyed on the
selection changing rather than on any rerun happening: the rerun that follows a run
completing must not wipe the run that just finished. Skipped while a run is in flight,
since that run owns the state and has nothing displayed yet.

## Empty carb panel on a No Loop stage (TRSET-9 follow-up, renderer)

**What changed (≤100 words):** Two fixes in `loop_home_renderer.py`. (1) `loop_cob`
is the **Loop algorithm's** carbs-on-board estimate, so it is absent on a stage that
runs no controller (`pre-noLoop_*`, `controller: null`); the renderer drew an
invisible all-NaN line and still legended it "Carbs on board", which reads as *this
scenario had no carbs*. The line and its legend entry are now omitted there and an
in-panel note says why. (2) x-limits are pinned to the data span, so an event at
simulation t=0 sat **on** the left spine with half its glyph outside the axes; carb
and dose markers now draw unclipped.

Example:

```bash
python -m pytest tests/test_loop_home_renderer.py     # arm64 conda env, never `uv run`
```

**Validation (≤100 words):** Diagnosed against a real run: the No Loop trace carries
the same carb events as the Loop stages (`true_carb_value` populated on all three;
`loop_cob` 97 points on the Loop stages, 0 on No Loop), and its glucose runs
110 → ~475 mg/dL — the meal absorbing unopposed. Marker position measured at fraction
`0.0000` across the axis. Seven unit tests added, each mutation-checked: reverting
either fix fails exactly the tests that assert it. A populated-but-all-zero `loop_cob`
is deliberately still treated as computed. Full GUI suite: 228 passed, 7 skipped.

**Cautions / limitations:** There is no non-Loop COB series to substitute —
`SimulationTrace` exposes only `loop_cob`, and the TSV's other carb columns are the
discrete entry values/durations. So the No Loop panel shows carb-entry markers and the
note, never a curve. Deriving a patient-side COB would mean modelling absorption in
the view layer, which this deliberately does not do. `clip_on=False` lets a boundary
marker overhang the axes by half a glyph; that is the intent (the alternative, padding
the x-limits, would reintroduce the empty forward gutter TRSET-22 removed). This
touches the TRSET-22/23 renderer, not TRSET-9 code — the clipping predates this
branch; TRSET-9 only made it prominent by defaulting meals to simulation start.
