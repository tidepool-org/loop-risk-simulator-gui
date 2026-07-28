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
