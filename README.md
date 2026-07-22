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
`git+https://github.com/tidepool-org/data-science-simulator@gui-bundle-v0.1.0`
and is the single source of truth for the pins.

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
  --simulator-ref gui-bundle-v0.1.0 \
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
