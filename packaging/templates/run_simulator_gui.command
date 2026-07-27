#!/bin/bash
# ===========================================================================
# run_simulator_gui.command  --  Phase 5 colleague-facing launcher (thin shell)
#
# The double-clickable entry point. All non-trivial logic -- arm64-conda
# discovery / bootstrap, version-aware env provisioning, the load-bearing
# site-packages symlink seam, and the first-run Swift .dylib build -- lives in
# the tested, stdlib-only launcher.py invoked below. This shell handles only the
# pre-Python precondition (Xcode CLT, which must be checked before running
# python3) and the final launch.
#
# FIRST RUN (colleague notes -- see README):
#   * Gatekeeper: the first time, right-click this file and choose "Open"
#     (double-click alone is blocked because the bundle is not code-signed).
#   * First run sets up the environment and builds components -- several
#     minutes, with progress shown in this window. Later runs start quickly.
# ===========================================================================
set -eo pipefail

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
info() { printf "  %s\n" "$1"; }
die()  { printf "\033[31m[ERROR]\033[0m %s\n" "$1"; echo; read -r -p "Press Return to close. "; exit 1; }

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo
bold "Tidepool Loop Risk Simulator GUI"
echo

# --- pre-Python precondition: Xcode Command Line Tools ---------------------
# Needed for the Swift build, AND running /usr/bin/python3 without the tools can
# pop a confusing install dialog -- so check here, before invoking Python.
if ! xcode-select -p >/dev/null 2>&1; then
    die "Xcode Command Line Tools are required but not installed. Open the Terminal app, run:  xcode-select --install  , complete the install, then open this launcher again."
fi

# --- find a python3 to run the launcher helper -----------------------------
PYTHON3="/usr/bin/python3"
if [ ! -x "$PYTHON3" ]; then
    PYTHON3="$(command -v python3 || true)"
fi
[ -n "$PYTHON3" ] || die "No python3 was found to start the launcher. Install the Xcode Command Line Tools:  xcode-select --install"

# --- provision (first run builds everything; later runs reuse) -------------
# launcher.py streams progress to this window (stderr) and prints ONLY the env
# python path to stdout, which we capture. It exits non-zero with a plain
# message on any failure.
info "Preparing to launch (the first run sets things up and can take several minutes)..."
echo
if ! ENV_PYTHON="$("$PYTHON3" "$BUNDLE_DIR/launcher.py" provision --bundle-dir "$BUNDLE_DIR")"; then
    echo
    die "Setup did not complete. See the messages above."
fi
[ -x "$ENV_PYTHON" ] || die "The launcher did not return a usable environment (got: '$ENV_PYTHON')."

# --- launch ----------------------------------------------------------------
echo
bold "Starting the app..."
info "A browser tab will open automatically. Keep this window open while you work."
info "To stop: press Ctrl-C here, or simply close this window."
echo
trap 'echo; echo "Shutting down..."; exit 0' INT TERM
# Streamlit opens the browser itself (config.toml does not set headless) and
# runs in the foreground; Ctrl-C or closing the window stops it.
( cd "$BUNDLE_DIR" && "$ENV_PYTHON" -m streamlit run "$BUNDLE_DIR/streamlit_app.py" )
