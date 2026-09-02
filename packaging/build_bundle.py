#!/usr/bin/env python3
"""
Phase 4 -- macOS setup/package bundle builder for loop-risk-simulator-gui.

Produces a versioned, self-contained bundle that pins data-science-simulator by
git ref instead of the Phase 3 editable-sibling install, and publishes it as a
GitHub Release asset.

Design (see CodeBot "Simulator GUI - Design Decisions", Phase 4 approved plan):

  * The pip dependency stays a PINNED GIT REF (strong, legible provenance).
  * `post_processing/severity_model.py` and the browsed `scenario_configs/`
    subtree are NOT part of the installed simulator package, so they are
    extracted ("vendored") FROM THE SAME PINNED TAG at build time -- one tag
    governs the pip pin AND both extracted paths. data-science-simulator is
    never modified.
  * The env spec is rendered from the repo's own conda-environment.yml (single
    source of pins); only the LoopAlgorithmToPython line is swapped for the
    vendored copy. No duplicated pins.

Extraction reads the TAG'S TREE via `git archive <ref>`, never the working
directory, so a dirty checkout cannot leak into the bundle.

Publishing is NOT performed here: `build` prints the exact `gh release create`
command for a maintainer to run (an outward, irreversible action).

Usage:
    python packaging/build_bundle.py build \\
        --version 0.1.0 \\
        --simulator-ref main \\
        --simulator-repo ../data-science-simulator \\
        --swift-repo ../LoopAlgorithmToPython \\
        --output-dir dist/
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import glob
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from typing import Dict, List, Optional, Set

# Paths (relative to the simulator repo root) that the installed simulator
# package does NOT carry but the GUI needs at runtime -- extracted from the pin.
# reusable/ sits at the tidepool_risk_v2 level (one above loop_risk_v2_0) and is
# where the browsed configs resolve their `reusable.*` pointers -- so it must be
# vendored too, or every config fails reference resolution at runtime.
SIMULATOR_VENDOR_PATHS: List[str] = [
    "post_processing/severity_model.py",
    "scenario_configs/tidepool_risk_v2/loop_risk_v2_0",
    "scenario_configs/tidepool_risk_v2/reusable",
]

# Files/dirs copied verbatim from the GUI repo into the bundle.
# Every local module streamlit_app.py imports must appear here or the bundle
# raises ImportError on first run -- verify_app_artifacts_complete() below is the
# guard that makes that a build-time failure instead of a first-double-click one.
# pytest.ini rides along with tests/ so the bundled suite runs under the same
# marker config as the repo (its `-m "not slow"` deselects the real 24h run).
APP_ARTIFACTS: List[str] = [
    "streamlit_app.py",
    "export_bundle.py",
    "loop_home_renderer.py",
    "meal_config.py",
    "start_page.py",
    "Tidepool_Logo_Light_Large_3000.jpg",
    "README.md",
    ".streamlit",
    "pytest.ini",
    "tests",
]

# The app module the guard starts from: the bundle's entry point.
APP_ENTRY_POINT = "streamlit_app.py"

BUNDLE_ENV_FILENAME = "conda-environment.yml"
SWIFT_VENDOR_RELPATH = "./vendor/LoopAlgorithmToPython"


def _run_git(repo: str, *args: str) -> str:
    """Run a git command in `repo`, returning stripped stdout. Raises on failure."""
    result = subprocess.run(
        ["git", "-C", repo, *args],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def resolve_ref(repo: str, ref: str) -> str:
    """Resolve a git ref (tag/branch/SHA) to its full commit SHA in `repo`."""
    return _run_git(repo, "rev-list", "-n", "1", ref)


def extract_tree_paths(repo: str, ref: str, paths: List[str], dest: str) -> None:
    """Extract `paths` from `ref`'s tree in `repo` into `dest`, preserving layout.

    Uses `git archive` so the extracted content is exactly the pinned commit's
    tree, independent of the working directory's state.
    """
    os.makedirs(dest, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
        tar_path = tmp.name
    try:
        with open(tar_path, "wb") as fh:
            subprocess.run(
                ["git", "-C", repo, "archive", "--format=tar", ref, "--", *paths],
                check=True, stdout=fh,
            )
        with tarfile.open(tar_path) as tar:
            # filter="data" -- forward-compatible with Python 3.14's default and
            # rejects unsafe member paths (git archive content is trusted, but
            # this is the documented safe extraction mode).
            tar.extractall(dest, filter="data")
    finally:
        os.remove(tar_path)


def render_env_spec(source_yaml: str, simulator_ref: str) -> str:
    """Render the bundle's env spec from the repo's conda-environment.yml.

    Pins the data-science-simulator line to `simulator_ref` and swaps the
    editable-sibling LoopAlgorithmToPython line for the vendored copy. All other
    pins are carried through unchanged (single source of truth).
    """
    with open(source_yaml) as fh:
        lines = fh.readlines()

    rendered: List[str] = []
    swapped_sim = swapped_swift = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- git+") and "data-science-simulator" in stripped:
            indent = line[: len(line) - len(line.lstrip())]
            rendered.append(
                f"{indent}- git+https://github.com/tidepool-org/"
                f"data-science-simulator@{simulator_ref}\n"
            )
            swapped_sim = True
        elif "LoopAlgorithmToPython" in stripped and stripped.startswith("-"):
            indent = line[: len(line) - len(line.lstrip())]
            rendered.append(f"{indent}- {SWIFT_VENDOR_RELPATH}\n")
            swapped_swift = True
        else:
            rendered.append(line)

    if not swapped_sim:
        raise ValueError(
            f"No data-science-simulator git+ line found in {source_yaml}; "
            "cannot render a pinned bundle spec."
        )
    if not swapped_swift:
        raise ValueError(
            f"No LoopAlgorithmToPython line found in {source_yaml}; "
            "cannot render the vendored bundle spec."
        )
    return "".join(rendered)


def stage_app_code(app_repo: str, dest: str, artifacts: List[str]) -> None:
    """Copy the GUI app artifacts from `app_repo` into `dest`."""
    os.makedirs(dest, exist_ok=True)
    for name in artifacts:
        src = os.path.join(app_repo, name)
        if not os.path.exists(src):
            raise FileNotFoundError(f"Expected app artifact missing: {src}")
        target = os.path.join(dest, name)
        if os.path.isdir(src):
            shutil.copytree(src, target, dirs_exist_ok=True)
        else:
            shutil.copy2(src, target)


def _module_level_imports(source_path: str) -> Set[str]:
    """Top-level module names imported at module level by the file at `source_path`.

    Parsed with `ast`, never executed and never imported: the builder is
    stdlib-only, and importing streamlit_app here would pull streamlit, pandas and
    the simulator into the build. Only statements in the module body are read --
    an import nested inside a function or an `if` is not seen (bounded on purpose;
    the failure this guards is the module-level import that makes the bundle die
    before it renders).
    """
    with open(source_path) as fh:
        tree = ast.parse(fh.read(), filename=source_path)

    names: Set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def _local_module_artifact(app_repo: str, module: str) -> Optional[str]:
    """The artifact name that stages `module`, or None if it is not a local module.

    "Local" is decided by what exists in the app repo -- a second hardcoded list of
    module names would be the same drift defect one level up.
    """
    if os.path.isfile(os.path.join(app_repo, f"{module}.py")):
        return f"{module}.py"
    if os.path.isfile(os.path.join(app_repo, module, "__init__.py")):
        return module
    return None


def _local_imports(app_repo: str, source_path: str) -> Dict[str, str]:
    """{module name: artifact name} for the local modules `source_path` imports."""
    found: Dict[str, str] = {}
    for name in _module_level_imports(source_path):
        artifact = _local_module_artifact(app_repo, name)
        if artifact is not None:
            found[name] = artifact
    return found


def verify_app_artifacts_complete(
    app_repo: str, artifacts: List[str], entry_point: str = APP_ENTRY_POINT
) -> None:
    """Raise if a local module the app imports would not be staged into the bundle.

    `stage_app_code` only raises for artifacts it was TOLD to copy, so a module
    missing from `artifacts` produces a bundle that builds cleanly and then dies
    with ImportError on the colleague's first double-click. This turns that into a
    build-time failure. TRSET-7, TRSET-9, TRSET-22 and TRSET-34 each added a module
    without updating the list; adding their four names fixes today, this stops the
    next one.

    Scope is one level of indirection, explicitly bounded: the entry point's own
    local imports, plus the local imports of each module it imports. Not a full
    dependency walk.
    """
    staged = set(artifacts)
    missing: Dict[str, str] = {}  # artifact -> the file that imports it

    if entry_point not in staged:
        missing[entry_point] = "the bundle entry point"

    direct = _local_imports(app_repo, os.path.join(app_repo, entry_point))
    for artifact in direct.values():
        if artifact not in staged:
            missing.setdefault(artifact, entry_point)

    for module, artifact in sorted(direct.items()):
        module_path = os.path.join(app_repo, f"{module}.py")
        if not os.path.isfile(module_path):
            continue  # a package: its own modules ship inside the staged directory
        for sub_artifact in _local_imports(app_repo, module_path).values():
            if sub_artifact not in staged:
                missing.setdefault(sub_artifact, f"{module}.py")

    if missing:
        detail = "; ".join(
            f"{artifact} (imported by {importer})" for artifact, importer in sorted(missing.items())
        )
        raise ValueError(
            f"App artifacts incomplete -- the bundle would raise ImportError on "
            f"first run: {detail}. Add the missing name(s) to APP_ARTIFACTS in "
            f"packaging/build_bundle.py."
        )


def _strip_prebuilt_dylibs(root: str) -> List[str]:
    """Remove every ``*.dylib`` under ``root``; return the removed paths (sorted).

    Enforces the "no pre-built binary in the bundle" contract regardless of what
    is committed in the Swift repo at the vendored ref.
    """
    removed = sorted(glob.glob(os.path.join(root, "**", "*.dylib"), recursive=True))
    for path in removed:
        os.remove(path)
    return removed


def vendor_swift(swift_repo: str, swift_ref: str, dest: str) -> None:
    """Vendor LoopAlgorithmToPython source at `swift_ref` into `dest`.

    Source only -- the launcher builds the .dylib on first run (the settled
    small-asset / heavy-first-run tradeoff). `git archive` carries whatever is
    committed at `swift_ref`, which has historically INCLUDED a stale committed
    .dylib, so any pre-built binary is stripped after extraction. Trusting the
    committed one shipped a 2024 build missing a symbol the current api.py calls
    (getLoopRecommendations -> runtime dlsym failure); stripping it guarantees the
    launcher always builds a fresh, matching library.
    """
    extract_tree_paths(swift_repo, swift_ref, ["."], dest)
    _strip_prebuilt_dylibs(dest)


def write_version_stamp(dest: str, stamp: dict) -> None:
    """Write BUNDLE_VERSION.json -- the bundle's provenance record."""
    with open(os.path.join(dest, "BUNDLE_VERSION.json"), "w") as fh:
        json.dump(stamp, fh, indent=2, sort_keys=True)
        fh.write("\n")


def assemble_archive(staging: str, output_dir: str, bundle_name: str) -> str:
    """Archive `staging` into a single .tar.gz under `output_dir`. Returns path."""
    os.makedirs(output_dir, exist_ok=True)
    base = os.path.join(output_dir, bundle_name)
    # Single archive operation over the whole staging tree (no manual walk).
    archive_path = shutil.make_archive(base, "gztar", root_dir=staging)
    return archive_path


def publish_command(asset_path: str, bundle_version: str, gui_repo_slug: str) -> str:
    """Return the `gh` command a maintainer runs to publish the asset (not run here)."""
    tag = f"gui-bundle-v{bundle_version}"
    return (
        f"gh release create {tag} {asset_path} "
        f"--repo {gui_repo_slug} "
        f'--title "loop-risk-simulator-gui bundle v{bundle_version}" '
        f'--notes "macOS setup bundle. See README for install/first-run."'
    )


def build_bundle(
    *,
    version: str,
    simulator_ref: str,
    simulator_repo: str,
    swift_repo: str,
    swift_ref: str,
    app_repo: str,
    output_dir: str,
    built_at: str,
) -> dict:
    """Build the full bundle. Returns the version stamp dict.

    Orchestration only -- each step is a single-responsibility helper above.
    """
    # Fail before any git or staging work: a build that would produce a bundle
    # dead on first run should not produce an archive at all.
    verify_app_artifacts_complete(app_repo, APP_ARTIFACTS)

    bundle_name = f"loop-risk-simulator-gui-{version}"
    simulator_sha = resolve_ref(simulator_repo, simulator_ref)
    swift_sha = resolve_ref(swift_repo, swift_ref)

    with tempfile.TemporaryDirectory() as staging:
        # 1. App code.
        stage_app_code(app_repo, staging, APP_ARTIFACTS)

        # 2. Rendered pinned env spec (from the repo's single-source-of-pins file).
        env_text = render_env_spec(
            os.path.join(app_repo, "conda-environment.yml"), simulator_ref
        )
        with open(os.path.join(staging, BUNDLE_ENV_FILENAME), "w") as fh:
            fh.write(env_text)

        # 3. Orphan simulator paths, extracted from the SAME pinned tag.
        extract_tree_paths(
            simulator_repo, simulator_ref, SIMULATOR_VENDOR_PATHS,
            os.path.join(staging, "vendor", "sim"),
        )

        # 4. Vendored Swift source (built to .dylib on first run).
        vendor_swift(swift_repo, swift_ref, os.path.join(staging, "vendor", "LoopAlgorithmToPython"))

        # 5. Launcher: the thin .command entry point + its stdlib-only helper.
        # Both are staged from packaging/ -- the .command is only a shell over
        # launcher.py, which owns the real provisioning logic (single source).
        launcher_src = os.path.join(app_repo, "packaging", "templates", "run_simulator_gui.command")
        launcher_dst = os.path.join(staging, "run_simulator_gui.command")
        shutil.copy2(launcher_src, launcher_dst)
        os.chmod(launcher_dst, 0o755)
        helper_src = os.path.join(app_repo, "packaging", "launcher.py")
        if not os.path.isfile(helper_src):
            raise FileNotFoundError(f"Launcher helper missing: {helper_src}")
        shutil.copy2(helper_src, os.path.join(staging, "launcher.py"))

        # 6. Provenance stamp.
        stamp = {
            "bundle_version": version,
            "built_at": built_at,
            "simulator_ref": simulator_ref,
            "simulator_sha": simulator_sha,
            "swift_ref": swift_ref,
            "swift_sha": swift_sha,
        }
        write_version_stamp(staging, stamp)

        # 7. Single archive op.
        archive_path = assemble_archive(staging, output_dir, bundle_name)

    stamp["archive_path"] = archive_path
    return stamp


def _utc_now_iso() -> str:
    """Current UTC time as an ISO 8601 string (build timestamp)."""
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the loop-risk-simulator-gui macOS bundle.")
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="Build the versioned bundle archive.")
    b.add_argument("--version", required=True, help="Bundle version, e.g. 0.1.0")
    b.add_argument("--simulator-ref", default="main", help="data-science-simulator git ref to build against (default: main; the build still records the resolved SHA in the version stamp).")
    b.add_argument("--simulator-repo", default="../data-science-simulator", help="Local data-science-simulator checkout.")
    b.add_argument("--swift-repo", default="../LoopAlgorithmToPython", help="Local LoopAlgorithmToPython checkout.")
    b.add_argument("--swift-ref", default="HEAD", help="LoopAlgorithmToPython git ref to vendor.")
    b.add_argument("--app-repo", default=".", help="This GUI repo root.")
    b.add_argument("--output-dir", default="dist", help="Where to write the archive.")
    b.add_argument("--gui-repo-slug", default="tidepool-org/loop-risk-simulator-gui", help="For the publish command.")

    args = parser.parse_args(argv)

    if args.command == "build":
        stamp = build_bundle(
            version=args.version,
            simulator_ref=args.simulator_ref,
            simulator_repo=args.simulator_repo,
            swift_repo=args.swift_repo,
            swift_ref=args.swift_ref,
            app_repo=args.app_repo,
            output_dir=args.output_dir,
            built_at=_utc_now_iso(),
        )
        print(json.dumps(stamp, indent=2, sort_keys=True))
        print("\nBundle built. To publish (run this yourself -- not done automatically):\n")
        print("  " + publish_command(stamp["archive_path"], args.version, args.gui_repo_slug))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
