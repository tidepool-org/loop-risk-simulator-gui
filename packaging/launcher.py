#!/usr/bin/env python3
"""
Phase 5 -- colleague-facing launcher helper for loop-risk-simulator-gui.

The double-clickable ``run_simulator_gui.command`` is a thin shell over this
module: it handles only the pre-Python precondition (Xcode CLT) and the final
Streamlit launch. Everything non-trivial -- and therefore everything worth
testing -- lives here, mirroring how ``build_bundle.py`` owns the packaging
logic (see CodeBot "Simulator GUI - Design Decisions", Phase 5).

Why stdlib-only: this runs *before* the conda env it provisions exists, on the
system ``/usr/bin/python3``. It must import nothing outside the standard library.

What ``provision()`` does, in order (idempotent + resumable):

  1. Reconcile the bundle's ``BUNDLE_VERSION.json`` against a per-user state
     marker -- reuse an intact, version-matching install; otherwise rebuild.
  2. Resolve an arm64 conda (ARCH-MATCH: must match the arm64 ``.dylib``):
     discover a verified-arm64 one, never the ambient Intel default; if none is
     found, bootstrap a pinned, checksum-verified private Miniconda arm64.
  3. Build the Swift ``.dylib`` (first run only) -- BEFORE env creation, so the
     non-editable Swift install copies it into site-packages (setup.py ships it
     as package_data).
  4. Create the pinned conda env at an explicit ``-p`` prefix in a durable
     per-user location (survives the colleague moving the unzipped bundle).
  5. Copy the vendored orphan paths into that durable location and symlink them
     beside the installed package (the load-bearing seam: ``ScenarioParserV2``'s
     ``POINTER_OBJ_DIR`` is hardcoded relative to its own module and honors no
     env var, and ``gui_runner`` does ``import severity_model``).
  6. Record the state marker (written last, so ``complete`` is meaningful after
     an interrupted run).

Failures raise ``LauncherError`` with a plain, actionable message; the CLI
prints it and exits non-zero. No raw tracebacks reach the colleague.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from typing import Callable, Dict, List, Optional

# --- constants -------------------------------------------------------------

APP_NAME = "loop-risk-simulator-gui"
ENV_DIRNAME = "env"                  # conda prefix under the durable root
PRIVATE_CONDA_DIRNAME = "miniconda"  # bootstrapped conda under the durable root
VENDOR_SIM_DIRNAME = "vendor_sim"    # durable copy of the bundle's orphan paths
STATE_FILENAME = "provision_state.json"
BUNDLE_STAMP_FILENAME = "BUNDLE_VERSION.json"

# Pinned Miniconda arm64 installer -- the bootstrap fallback when no arm64 conda
# is found. First-party (repo.anaconda.com), URL-pinned, and checksum-verified
# BEFORE execution. py312 matches the env's pinned python 3.12.
MINICONDA_INSTALLER = "Miniconda3-py312_26.5.3-1-MacOSX-arm64.sh"
MINICONDA_URL = f"https://repo.anaconda.com/miniconda/{MINICONDA_INSTALLER}"
MINICONDA_SHA256 = "f5767f038d5aa8299254b96b7e0db4f086c29e4b3a620fc38fea51974f66abdd"

# Bundle-relative paths (staged by build_bundle.py).
SWIFT_RELPATH = os.path.join("vendor", "LoopAlgorithmToPython")
DYLIB_NAME = "libLoopAlgorithmToPython.dylib"
VENDOR_SIM_RELPATH = os.path.join("vendor", "sim")
ENV_SPEC_RELPATH = "conda-environment.yml"

# Reconciliation outcomes.
PROVISION = "provision"
REUSE = "reuse"

RunFn = Callable[..., "subprocess.CompletedProcess"]
DownloadFn = Callable[[str, str], None]


class LauncherError(Exception):
    """A precondition/provisioning failure carrying a colleague-facing message.

    The CLI prints ``str(error)`` as a plain, actionable line and exits non-zero;
    no traceback reaches the user (the workflow's no-silent/no-raw-traceback rule).
    """


def _progress(message: str) -> None:
    """Emit a colleague-facing progress line to stderr.

    stderr, not stdout: the shell captures stdout as the sole return value (the
    env python path), while progress streams live into the Terminal window.
    """
    print(message, file=sys.stderr, flush=True)


# --- arm64 conda discovery -------------------------------------------------

def _conda_base_python(conda_path: str) -> str:
    """Map a conda executable (.../bin/conda) to its base python (.../bin/python)."""
    return os.path.join(os.path.dirname(os.path.abspath(conda_path)), "python")


def verify_conda_arch(conda_path: str, run: RunFn = subprocess.run) -> Optional[str]:
    """Return the machine arch reported by ``conda_path``'s base python, else None.

    Never raises -- a broken or missing candidate is simply not usable (None), so
    discovery can move on to the next candidate.
    """
    base_python = _conda_base_python(conda_path)
    if not os.path.exists(base_python):
        return None
    try:
        result = run(
            [base_python, "-c", "import platform; print(platform.machine())"],
            check=True, capture_output=True, text=True,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return result.stdout.strip()


def _candidate_conda_paths(home: Optional[str] = None, environ: Optional[dict] = None) -> List[str]:
    """Ordered candidate conda executables to probe on an unknown Mac.

    Explicit ``$CONDA_EXE`` first, then common arm64 install locations. The
    ambient Intel default (/opt/anaconda3) is intentionally NOT listed --
    ARCH-MATCH requires arm64, and verify_conda_arch rejects anything non-arm64
    that a candidate path happens to point at.
    """
    home = home if home is not None else os.path.expanduser("~")
    environ = environ if environ is not None else os.environ
    candidates: List[str] = []
    conda_exe = environ.get("CONDA_EXE")
    if conda_exe:
        candidates.append(conda_exe)
    candidates += [
        os.path.join(home, "miniconda3", "bin", "conda"),
        os.path.join(home, "miniforge3", "bin", "conda"),
        os.path.join(home, "mambaforge", "bin", "conda"),
        os.path.join(home, "anaconda3", "bin", "conda"),
        "/opt/homebrew/bin/conda",
    ]
    seen: set = set()
    ordered: List[str] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


def find_arm64_conda(
    candidates: Optional[List[str]] = None,
    home: Optional[str] = None,
    environ: Optional[dict] = None,
    run: RunFn = subprocess.run,
) -> Optional[str]:
    """Return the first candidate conda whose base python is arm64, else None.

    Discovery only -- never assumes a path and never accepts a non-arm64 (Intel)
    conda, so the env it later builds matches the committed arm64 ``.dylib``.
    """
    if candidates is None:
        candidates = _candidate_conda_paths(home=home, environ=environ)
    for conda_path in candidates:
        if not os.path.isfile(conda_path) or not os.access(conda_path, os.X_OK):
            continue
        if verify_conda_arch(conda_path, run=run) == "arm64":
            return conda_path
    return None


# --- Miniconda bootstrap (fallback) ----------------------------------------

def _default_download(url: str, dest: str) -> None:
    """Stream ``url`` to ``dest`` via stdlib urllib. Raises on network failure."""
    with urllib.request.urlopen(url) as resp, open(dest, "wb") as fh:  # noqa: S310 (pinned https)
        shutil.copyfileobj(resp, fh)


def _sha256(path: str) -> str:
    """Return the hex SHA-256 of the file at ``path``."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bootstrap_miniconda(
    prefix: str,
    download: DownloadFn = _default_download,
    run: RunFn = subprocess.run,
) -> str:
    """Download the pinned Miniconda arm64 installer, verify it, install into
    ``prefix``, and return the installed conda executable path.

    The one download+execute path in the launcher: first-party, URL-pinned, and
    checksum-verified BEFORE execution. Raises ``LauncherError`` (offline,
    checksum mismatch, or install failure) with an actionable message.
    """
    parent = os.path.dirname(os.path.abspath(prefix))
    os.makedirs(parent, exist_ok=True)
    installer_path = os.path.join(parent, MINICONDA_INSTALLER)

    _progress("No arm64 conda found. Downloading Miniconda (about 100 MB, one time only)...")
    try:
        download(MINICONDA_URL, installer_path)
    except Exception as exc:  # network/URL errors -> actionable bail
        raise LauncherError(
            "Could not download the Miniconda installer (no internet connection?). "
            "Connect to the internet and try again, or ask IT to install arm64 "
            f"Miniconda from {MINICONDA_URL}."
        ) from exc

    actual = _sha256(installer_path)
    if actual != MINICONDA_SHA256:
        os.remove(installer_path)
        raise LauncherError(
            "The downloaded Miniconda installer failed its integrity check "
            f"(expected {MINICONDA_SHA256[:12]}..., got {actual[:12]}...). "
            "It was not run. Please try again."
        )

    _progress("Installing Miniconda...")
    try:
        run(["bash", installer_path, "-b", "-p", prefix], check=True)
    except subprocess.SubprocessError as exc:
        raise LauncherError("Miniconda installation failed. See the output above.") from exc
    finally:
        if os.path.exists(installer_path):
            os.remove(installer_path)

    conda_path = os.path.join(prefix, "bin", "conda")
    if not os.path.isfile(conda_path):
        raise LauncherError(f"Miniconda installed but conda was not found at {conda_path}.")
    return conda_path


def resolve_conda(
    root: str,
    home: Optional[str] = None,
    environ: Optional[dict] = None,
    download: DownloadFn = _default_download,
    run: RunFn = subprocess.run,
) -> str:
    """Return a usable arm64 conda: discover one, else bootstrap a private one.

    Discovery-first (prefer an existing verified-arm64 conda, never the Intel
    default), reuse a previously bootstrapped private conda if present, and only
    then download a pinned private Miniconda into ``<root>/miniconda``.
    """
    found = find_arm64_conda(home=home, environ=environ, run=run)
    if found:
        _progress(f"Using arm64 conda: {found}")
        return found

    private_prefix = os.path.join(root, PRIVATE_CONDA_DIRNAME)
    private_conda = os.path.join(private_prefix, "bin", "conda")
    if os.path.isfile(private_conda) and verify_conda_arch(private_conda, run=run) == "arm64":
        _progress(f"Using previously installed conda: {private_conda}")
        return private_conda

    return bootstrap_miniconda(private_prefix, download=download, run=run)


# --- durable per-user location + state -------------------------------------

def app_support_root(home: Optional[str] = None) -> str:
    """Return the durable per-user provisioning root under Application Support.

    Everything provisioned (private conda, env prefix, vendored orphan paths,
    state marker) lives here so it survives the colleague moving or replacing the
    unzipped bundle folder.
    """
    home = home if home is not None else os.path.expanduser("~")
    return os.path.join(home, "Library", "Application Support", APP_NAME)


def read_stamp(bundle_dir: str) -> dict:
    """Read the bundle's BUNDLE_VERSION.json provenance stamp. Raises if absent/corrupt."""
    stamp_path = os.path.join(bundle_dir, BUNDLE_STAMP_FILENAME)
    try:
        with open(stamp_path) as fh:
            return json.load(fh)
    except FileNotFoundError as exc:
        raise LauncherError(
            f"The bundle is missing {BUNDLE_STAMP_FILENAME} (expected at {stamp_path}); "
            "the download may be incomplete. Please re-download and unzip the bundle."
        ) from exc
    except json.JSONDecodeError as exc:
        raise LauncherError(f"The bundle's {BUNDLE_STAMP_FILENAME} is corrupt: {exc}.") from exc


def read_state(root: str) -> Optional[dict]:
    """Return the provisioning state marker, or None if not yet provisioned/corrupt."""
    state_path = os.path.join(root, STATE_FILENAME)
    try:
        with open(state_path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_state(root: str, state: dict) -> None:
    """Persist the provisioning state marker via an atomic replace (written last)."""
    os.makedirs(root, exist_ok=True)
    tmp = os.path.join(root, STATE_FILENAME + ".tmp")
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, os.path.join(root, STATE_FILENAME))


def reconcile(stamp: dict, state: Optional[dict]) -> str:
    """Decide REUSE vs (re)PROVISION under the version-aware rule.

    Absent or incomplete state -> PROVISION. A completed state whose simulator
    AND swift SHAs both match the bundle stamp -> REUSE. Any SHA mismatch ->
    PROVISION (recreate, not an in-place upgrade). Keyed on the immutable SHAs,
    not the mutable version string, so a newer bundle correctly rebuilds.
    """
    if not state or not state.get("complete"):
        return PROVISION
    if (
        state.get("simulator_sha") == stamp.get("simulator_sha")
        and state.get("swift_sha") == stamp.get("swift_sha")
    ):
        return REUSE
    return PROVISION


# --- symlink seam ----------------------------------------------------------

def plan_symlinks(site_packages: str, vendor_sim_root: str) -> Dict[str, str]:
    """Map each site-packages symlink to its vendored target for the orphan paths.

    ``scenario_configs`` (the parser's hardcoded POINTER_OBJ_DIR + streamlit's
    LIBRARY_ROOT) and a top-level ``severity_model.py`` (gui_runner's
    ``import severity_model``) must sit beside the installed package. Mirrors the
    Phase 4 Group-A fixture, which validated this exact mapping end-to-end.
    """
    return {
        os.path.join(site_packages, "scenario_configs"):
            os.path.join(vendor_sim_root, "scenario_configs"),
        os.path.join(site_packages, "severity_model.py"):
            os.path.join(vendor_sim_root, "post_processing", "severity_model.py"),
    }


def apply_symlinks(links: Dict[str, str]) -> None:
    """Create/refresh each ``link -> target``, replacing any existing entry.

    Raises ``LauncherError`` if a target is missing (vendoring incomplete) rather
    than leaving a dangling link that would fail obscurely at import time.
    """
    for link, target in links.items():
        if not os.path.exists(target):
            raise LauncherError(
                f"A required simulator path is missing from the bundle: {target}. "
                "The download may be incomplete; please re-download the bundle."
            )
        if os.path.islink(link):
            os.remove(link)
        elif os.path.isdir(link):
            shutil.rmtree(link)
        elif os.path.exists(link):
            os.remove(link)
        os.symlink(target, link)


# --- Swift .dylib + Xcode CLT ----------------------------------------------

def _dylib_path(swift_dir: str) -> str:
    """Path to the built Swift ``.dylib`` inside a vendored LoopAlgorithmToPython dir."""
    return os.path.join(swift_dir, "loop_to_python_api", DYLIB_NAME)


def needs_dylib_build(swift_dir: str) -> bool:
    """Return True if the Swift ``.dylib`` is absent and must be built."""
    return not os.path.isfile(_dylib_path(swift_dir))


def ensure_xcode_clt(run: RunFn = subprocess.run) -> None:
    """Raise ``LauncherError`` if the Xcode Command Line Tools are not installed."""
    try:
        run(["xcode-select", "-p"], check=True, capture_output=True, text=True)
    except (subprocess.SubprocessError, OSError) as exc:
        raise LauncherError(
            "Xcode Command Line Tools are required (for the one-time Swift build) "
            "but are not installed. Open the Terminal app and run:  "
            "xcode-select --install"
        ) from exc


def build_dylib(swift_dir: str, run: RunFn = subprocess.run) -> None:
    """Build the Swift ``.dylib`` in ``swift_dir`` via its build.sh. Raises on failure.

    Runs BEFORE env creation so the subsequent non-editable Swift install copies
    the freshly built ``.dylib`` into the env's site-packages (setup.py declares
    it as ``package_data`` with ``include_package_data``).
    """
    build_sh = os.path.join(swift_dir, "build.sh")
    if not os.path.isfile(build_sh):
        raise LauncherError(f"The Swift build script was not found at {build_sh}; bundle incomplete.")
    _progress("Building components (first run only, a few minutes)...")
    try:
        run(["bash", build_sh], cwd=swift_dir, check=True)
    except subprocess.SubprocessError as exc:
        raise LauncherError(
            "The Swift build failed. See the output above and confirm the Xcode "
            "Command Line Tools are installed (xcode-select --install)."
        ) from exc
    if needs_dylib_build(swift_dir):
        raise LauncherError("The Swift build ran but did not produce the .dylib.")


# --- conda env -------------------------------------------------------------

def env_python_path(env_prefix: str) -> str:
    """Return the python interpreter path inside a conda prefix env."""
    return os.path.join(env_prefix, "bin", "python")


def create_env(conda_path: str, env_prefix: str, bundle_dir: str, run: RunFn = subprocess.run) -> None:
    """Create the conda env at ``env_prefix`` from the bundle's pinned spec.

    Runs from ``bundle_dir`` so the spec's ``./vendor/LoopAlgorithmToPython``
    relative path resolves, and uses an explicit ``-p`` prefix (decoupled from
    which conda built it). A stale/partial prefix is removed first, so an
    interrupted first run recovers cleanly on the next double-click.
    """
    if os.path.exists(env_prefix):
        _progress("Removing an incomplete previous environment...")
        shutil.rmtree(env_prefix)
    spec = os.path.join(bundle_dir, ENV_SPEC_RELPATH)
    if not os.path.isfile(spec):
        raise LauncherError(f"The environment spec was not found at {spec}; bundle incomplete.")
    _progress("Setting up the environment (first run only, several minutes)...")
    try:
        run([conda_path, "env", "create", "-p", env_prefix, "-f", spec], cwd=bundle_dir, check=True)
    except subprocess.SubprocessError as exc:
        raise LauncherError("Environment setup failed. See the solver output above.") from exc
    if not os.path.isfile(env_python_path(env_prefix)):
        raise LauncherError(
            f"The environment was created but its python was not found at "
            f"{env_python_path(env_prefix)}."
        )


def site_packages_of(env_prefix: str, run: RunFn = subprocess.run) -> str:
    """Return the purelib site-packages directory of the env at ``env_prefix``."""
    result = run(
        [env_python_path(env_prefix), "-c",
         "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def copy_vendor_sim(bundle_dir: str, dest_root: str) -> str:
    """Copy the bundle's vendored orphan paths into the durable root; return the copy.

    Copying (rather than symlinking straight at the bundle) is what makes the
    provisioned install survive the colleague moving the unzipped bundle folder.
    """
    src = os.path.join(bundle_dir, VENDOR_SIM_RELPATH)
    if not os.path.isdir(src):
        raise LauncherError(f"Vendored simulator paths not found at {src}; bundle incomplete.")
    dest = os.path.join(dest_root, VENDOR_SIM_DIRNAME)
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(src, dest, symlinks=True)
    return dest


# --- orchestrator ----------------------------------------------------------

def _reuse_is_intact(state: dict, run: RunFn = subprocess.run) -> bool:
    """Return True only if a REUSE state's env python and symlinks are all present.

    Guards against a state marker that outlived its artifacts (env or vendored
    copy deleted); if anything is missing we fall through to a clean reprovision.
    """
    env_prefix = state.get("env_prefix")
    vendor_sim_root = state.get("vendor_sim_root")
    if not env_prefix or not vendor_sim_root:
        return False
    if not os.path.isfile(env_python_path(env_prefix)):
        return False
    try:
        site_packages = site_packages_of(env_prefix, run=run)
    except (subprocess.SubprocessError, OSError):
        return False
    for link in plan_symlinks(site_packages, vendor_sim_root):
        if not os.path.islink(link) or not os.path.exists(link):
            return False
    return True


def provision(
    bundle_dir: str,
    root: Optional[str] = None,
    run: RunFn = subprocess.run,
    download: DownloadFn = _default_download,
    home: Optional[str] = None,
    environ: Optional[dict] = None,
) -> str:
    """Provision (or reuse) everything the app needs; return the env python path.

    Idempotent + resumable: reuse when the bundle's SHAs match a completed state
    and its on-disk artifacts are intact; otherwise (re)build the ``.dylib``,
    recreate the env, copy + symlink the vendored orphan paths, and record the
    state marker last. See ``reconcile`` for the version-aware rule.
    """
    bundle_dir = os.path.abspath(bundle_dir)
    root = root if root is not None else app_support_root(home=home)
    os.makedirs(root, exist_ok=True)

    stamp = read_stamp(bundle_dir)
    state = read_state(root)
    if reconcile(stamp, state) == REUSE and _reuse_is_intact(state, run=run):
        _progress("Already set up -- starting.")
        return env_python_path(state["env_prefix"])

    conda_path = resolve_conda(root, home=home, environ=environ, download=download, run=run)

    swift_dir = os.path.join(bundle_dir, SWIFT_RELPATH)
    if needs_dylib_build(swift_dir):
        ensure_xcode_clt(run=run)
        build_dylib(swift_dir, run=run)

    env_prefix = os.path.join(root, ENV_DIRNAME)
    create_env(conda_path, env_prefix, bundle_dir, run=run)

    vendor_sim_root = copy_vendor_sim(bundle_dir, root)
    site_packages = site_packages_of(env_prefix, run=run)
    apply_symlinks(plan_symlinks(site_packages, vendor_sim_root))

    write_state(root, {
        "complete": True,
        "bundle_version": stamp.get("bundle_version"),
        "simulator_sha": stamp.get("simulator_sha"),
        "swift_sha": stamp.get("swift_sha"),
        "env_prefix": env_prefix,
        "vendor_sim_root": vendor_sim_root,
        "conda_path": conda_path,
    })
    _progress("Setup complete -- starting.")
    return env_python_path(env_prefix)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point. ``provision`` prints ONLY the env python path to stdout."""
    parser = argparse.ArgumentParser(description="loop-risk-simulator-gui launcher helper.")
    sub = parser.add_subparsers(dest="command", required=True)
    prov = sub.add_parser("provision", help="Provision (or reuse) the env; print the env python path.")
    prov.add_argument("--bundle-dir", required=True, help="Path to the unzipped bundle directory.")

    args = parser.parse_args(argv)

    if args.command == "provision":
        try:
            env_python = provision(args.bundle_dir)
        except LauncherError as exc:
            print(f"\n[ERROR] {exc}\n", file=sys.stderr)
            return 1
        print(env_python)  # sole stdout: the shell captures this
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
