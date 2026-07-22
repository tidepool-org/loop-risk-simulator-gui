#!/bin/bash
# ===========================================================================
# run_simulator_gui.command  --  Phase 4 bundle launcher (minimal)
#
# Scope: establish the arm64 conda env from the bundle's pinned spec, build the
# Swift .dylib on first run, wire the two runtime path seams (post_processing /
# scenario_configs are vendored, not part of the pinned pip install), and
# launch the app. The polished colleague-facing UX (Gatekeeper guidance,
# auto-open browser, version-aware env recreate) is Phase 5, not here.
#
# Adapted from the Phase 0 spike launcher (data-science-simulator/spike/).
# ===========================================================================
set -eo pipefail

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
info() { printf "  %s\n" "$1"; }
die()  { printf "\033[31m[ERROR]\033[0m %s\n" "$1"; echo; read -r -p "Press Return to close."; exit 1; }

echo
bold "Tidepool Loop Risk Simulator GUI -- bundle launcher (arm64)"
echo

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_NAME="loop-risk-simulator-gui"
ARM64_CONDA="$HOME/miniconda3/bin/conda"
SWIFT_DIR="$BUNDLE_DIR/vendor/LoopAlgorithmToPython"
DYLIB="$SWIFT_DIR/loop_to_python_api/libLoopAlgorithmToPython.dylib"

[ -x "$ARM64_CONDA" ] || die "arm64 conda not found at $ARM64_CONDA. Install arm64 Miniconda first."

# --- 1. arm64 conda (must match the arm64 .dylib) --------------------------
bold "[1/4] arm64 conda"
CONDA_BASE="$("$ARM64_CONDA" info --base)"
CONDA_ARCH="$("$CONDA_BASE/bin/python" -c 'import platform; print(platform.machine())')"
[ "$CONDA_ARCH" = "arm64" ] || die "conda at $ARM64_CONDA is $CONDA_ARCH, not arm64."
info "conda base : $CONDA_BASE"
echo

# --- 2. Swift .dylib (built first run) -------------------------------------
bold "[2/4] Swift native library"
if [ -f "$DYLIB" ]; then
    info ".dylib present -- skipping build."
else
    info ".dylib missing -- building from vendored source (first run)."
    ( cd "$SWIFT_DIR" && ./build.sh ) || die "Swift build failed. Ensure Xcode CLT installed."
    [ -f "$DYLIB" ] || die "build.sh ran but .dylib not at $DYLIB"
fi
echo

# --- 3. conda env from the bundle's pinned spec ----------------------------
bold "[3/4] Conda environment: $ENV_NAME"
if [ ! -d "$CONDA_BASE/envs/$ENV_NAME" ]; then
    info "Creating environment (several minutes on first run)."
    # env-file relative paths resolve against the yml's location, so run from
    # the bundle dir so ./vendor/LoopAlgorithmToPython resolves.
    ( cd "$BUNDLE_DIR" && "$ARM64_CONDA" env create -n "$ENV_NAME" -f "$BUNDLE_DIR/$ENV_NAME.yml" 2>/dev/null \
      || "$ARM64_CONDA" env create -n "$ENV_NAME" -f "$BUNDLE_DIR/conda-environment.yml" ) \
      || die "conda env create failed -- see solver output above."
else
    info "Environment exists -- reusing."
fi
ENV_PYTHON="$CONDA_BASE/envs/$ENV_NAME/bin/python"
[ -x "$ENV_PYTHON" ] || die "Env python not found at $ENV_PYTHON."
echo

# --- 4. place vendored orphan paths beside the installed package -----------
# The pinned pip install does NOT carry post_processing/severity_model.py or
# scenario_configs/. gui_runner imports `severity_model`, and ScenarioParserV2
# resolves `reusable.*` pointers from a path hardcoded relative to its own module
# (<package>/../../scenario_configs/tidepool_risk_v2) -- NOT from any env var. So
# both must sit beside the installed package. Symlink the vendored copies into
# site-packages so every native resolution (gui_runner import, streamlit_app
# LIBRARY_ROOT, and the parser's pointer dir) finds them.
bold "[4/5] Wire vendored simulator paths"
SITE_PACKAGES="$("$ENV_PYTHON" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
ln -sfn "$BUNDLE_DIR/vendor/sim/scenario_configs" "$SITE_PACKAGES/scenario_configs"
ln -sfn "$BUNDLE_DIR/vendor/sim/post_processing/severity_model.py" "$SITE_PACKAGES/severity_model.py"
info "Linked scenario_configs + severity_model into $SITE_PACKAGES"
echo

# --- 5. launch -------------------------------------------------------------
bold "[5/5] Launch"
info "Starting Streamlit. Press Ctrl-C in this window to stop."
( cd "$BUNDLE_DIR" && "$ENV_PYTHON" -m streamlit run "$BUNDLE_DIR/streamlit_app.py" )
