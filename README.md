# loop-risk-simulator-gui

Streamlit MVP for running a Tidepool Loop risk assessment without a terminal.
This is the view layer only — all simulator/validation logic lives in
`gui_runner.py`, which stays in the
[`data-science-simulator`](https://github.com/tidepool-org/data-science-simulator)
repo as the sanctioned in-process service layer.

## Load-bearing assumption: sibling directory layout

This repo depends on `data-science-simulator` as an **editable local-path
install**, not a pinned version:

```
some-parent-dir/
├── data-science-simulator/
└── loop-risk-simulator-gui/   <- this repo
```

`conda-environment.yml` installs it as `-e ../data-science-simulator` — that
relative path only resolves if the two repos are cloned as siblings. This is
an MVP simplification (Phase 3); a pinned git ref / vendored dependency is
planned for Phase 4, at which point this constraint goes away.

## Setup

Requires an **arm64** conda (matches the committed arm64 `.dylib` in the
simulator repo — never use ambient `conda` or `uv run`):

```bash
conda env create -f conda-environment.yml
conda activate loop-risk-simulator-gui
```

## Running the app

```bash
streamlit run streamlit_app.py
```

## Running tests

```bash
python -m pytest tests/
```

(Run with the conda env's own interpreter, not `uv run` — the simulator and
its dependencies, e.g. pandas, are only available there.)
