"""
Unit tests for packaging/build_bundle.py -- the Phase 4 bundle builder.

Tests the packaging logic in isolation: env-spec rendering (pin resolution +
Swift-line swap), version stamping, app-code staging, publish-command format,
and full assembly with the git boundary (resolve_ref / extract_tree_paths)
mocked so no network, git, or real simulator checkout is needed.

Also covers the TRSET-47 artifact-drift guard, which parses the app's imports and
fails the build when a local module is missing from APP_ARTIFACTS.
"""

import json
import os
import re
import sys
import tarfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "packaging"))
import build_bundle  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_SOURCE_ENV = """\
name: loop-risk-simulator-gui
dependencies:
  - python=3.12.7
  - pip:
    - streamlit==1.59.2
    - git+https://github.com/tidepool-org/data-science-models@sf/incorporate_pa
    - git+https://github.com/tidepool-org/data-science-simulator@gui-bundle-v0.1.0
    - -e ../LoopAlgorithmToPython
"""


def _write_source_env(tmp_path):
    p = tmp_path / "conda-environment.yml"
    p.write_text(_SOURCE_ENV)
    return str(p)


# Mirrors how the real streamlit_app.py imports its four local modules (both
# `import x` and `from x import y` forms), so a synthetic repo exercises the
# drift guard the same way the real one does.
_ENTRY_SOURCE = """\
import meal_config
import start_page
from export_bundle import build_export_zip
from loop_home_renderer import render_loop_home_screen
"""


def _make_app_repo(tmp_path, entry_source=_ENTRY_SOURCE, extra_modules=None):
    """A synthetic GUI repo carrying every APP_ARTIFACTS entry, plus the files the
    builder stages from packaging/. Driven off APP_ARTIFACTS itself so a future
    addition to the list does not silently leave this fixture behind."""
    app = tmp_path / "gui"
    (app / "packaging" / "templates").mkdir(parents=True)
    (app / ".streamlit").mkdir()
    (app / "tests").mkdir()
    (app / "streamlit_app.py").write_text(entry_source)
    for artifact in build_bundle.APP_ARTIFACTS:
        target = app / artifact
        if not target.exists():
            target.write_text(f"# {artifact}\n")
    (app / "conda-environment.yml").write_text(_SOURCE_ENV)
    (app / "packaging" / "templates" / "run_simulator_gui.command").write_text("#!/bin/bash\n")
    (app / "packaging" / "launcher.py").write_text("# launcher helper\n")
    for name, source in (extra_modules or {}).items():
        (app / name).write_text(source)
    return app


def _mock_git_boundary(monkeypatch):
    """resolve_ref returns a fake SHA; extract_tree_paths drops a marker file."""
    monkeypatch.setattr(build_bundle, "resolve_ref", lambda repo, ref: f"sha-{ref}")

    def fake_extract(repo, ref, paths, dest):
        os.makedirs(dest, exist_ok=True)
        marker = "swift" if "LoopAlgorithmToPython" in dest else "sim"
        with open(os.path.join(dest, f"_{marker}_extracted"), "w") as fh:
            fh.write(ref)

    monkeypatch.setattr(build_bundle, "extract_tree_paths", fake_extract)


# --- render_env_spec -------------------------------------------------------

def test_render_env_spec_pins_requested_ref_and_swaps_swift(tmp_path):
    src = _write_source_env(tmp_path)
    out = build_bundle.render_env_spec(src, simulator_ref="gui-bundle-v9.9.9")

    assert "data-science-simulator@gui-bundle-v9.9.9" in out
    # old pin fully replaced, not duplicated
    assert "gui-bundle-v0.1.0" not in out
    # Swift editable-sibling swapped for the vendored copy
    assert build_bundle.SWIFT_VENDOR_RELPATH in out
    assert "-e ../LoopAlgorithmToPython" not in out
    # unrelated pins carried through untouched
    assert "data-science-models@sf/incorporate_pa" in out
    assert "streamlit==1.59.2" in out


def test_render_env_spec_raises_without_simulator_line(tmp_path):
    p = tmp_path / "conda-environment.yml"
    p.write_text("dependencies:\n  - pip:\n    - -e ../LoopAlgorithmToPython\n")
    with pytest.raises(ValueError, match="data-science-simulator"):
        build_bundle.render_env_spec(str(p), simulator_ref="x")


def test_render_env_spec_raises_without_swift_line(tmp_path):
    p = tmp_path / "conda-environment.yml"
    p.write_text(
        "dependencies:\n  - pip:\n"
        "    - git+https://github.com/tidepool-org/data-science-simulator@t\n"
    )
    with pytest.raises(ValueError, match="LoopAlgorithmToPython"):
        build_bundle.render_env_spec(str(p), simulator_ref="x")


# --- version stamp ---------------------------------------------------------

def test_write_version_stamp_roundtrip(tmp_path):
    stamp = {
        "bundle_version": "0.1.0",
        "built_at": "2026-07-22T00:00:00+00:00",
        "simulator_ref": "gui-bundle-v0.1.0",
        "simulator_sha": "abc123",
        "swift_ref": "HEAD",
        "swift_sha": "def456",
    }
    build_bundle.write_version_stamp(str(tmp_path), stamp)
    loaded = json.loads((tmp_path / "BUNDLE_VERSION.json").read_text())
    assert loaded == stamp


# --- stage_app_code --------------------------------------------------------

def test_stage_app_code_copies_files_and_dirs(tmp_path):
    app = tmp_path / "app"
    (app / "tests").mkdir(parents=True)
    (app / "streamlit_app.py").write_text("# app")
    (app / "tests" / "t.py").write_text("# test")
    dest = tmp_path / "staging"

    build_bundle.stage_app_code(str(app), str(dest), ["streamlit_app.py", "tests"])

    assert (dest / "streamlit_app.py").read_text() == "# app"
    assert (dest / "tests" / "t.py").read_text() == "# test"


def test_stage_app_code_raises_on_missing_artifact(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    with pytest.raises(FileNotFoundError):
        build_bundle.stage_app_code(str(app), str(tmp_path / "s"), ["nope.py"])


# --- publish_command -------------------------------------------------------

def test_publish_command_shape():
    cmd = build_bundle.publish_command("/x/bundle.tar.gz", "0.1.0", "tidepool-org/loop-risk-simulator-gui")
    assert cmd.startswith("gh release create gui-bundle-v0.1.0 /x/bundle.tar.gz")
    assert "--repo tidepool-org/loop-risk-simulator-gui" in cmd


# --- vendor_swift strips the (stale) committed .dylib ----------------------

def test_strip_prebuilt_dylibs_removes_only_dylibs(tmp_path):
    (tmp_path / "loop_to_python_api").mkdir()
    dylib = tmp_path / "loop_to_python_api" / "libLoopAlgorithmToPython.dylib"
    dylib.write_text("stale-binary")
    src = tmp_path / "loop_to_python_api" / "api.py"
    src.write_text("# api")

    removed = build_bundle._strip_prebuilt_dylibs(str(tmp_path))

    assert str(dylib) in removed
    assert not dylib.exists()          # the pre-built binary is gone
    assert src.exists()                # source is untouched


def test_vendor_swift_strips_committed_dylib(tmp_path, monkeypatch):
    dest = tmp_path / "swift"

    def fake_extract(repo, ref, paths, d):
        api = os.path.join(d, "loop_to_python_api")
        os.makedirs(api, exist_ok=True)
        open(os.path.join(d, "build.sh"), "w").close()
        open(os.path.join(api, "libLoopAlgorithmToPython.dylib"), "w").close()

    monkeypatch.setattr(build_bundle, "extract_tree_paths", fake_extract)
    build_bundle.vendor_swift("/fake/swift", "HEAD", str(dest))

    assert os.path.isfile(dest / "build.sh")  # source kept
    assert not os.path.exists(dest / "loop_to_python_api" / "libLoopAlgorithmToPython.dylib")


# --- full assembly with the git boundary mocked ----------------------------

def test_build_bundle_assembles_expected_tree(tmp_path, monkeypatch):
    app = _make_app_repo(tmp_path)
    _mock_git_boundary(monkeypatch)

    out_dir = tmp_path / "dist"
    stamp = build_bundle.build_bundle(
        version=build_bundle.APP_VERSION,
        simulator_ref="gui-bundle-v0.1.0",
        simulator_repo="/fake/sim",
        swift_repo="/fake/swift",
        swift_ref="HEAD",
        app_repo=str(app),
        output_dir=str(out_dir),
        built_at="2026-07-22T00:00:00+00:00",
    )

    assert stamp["bundle_version"] == build_bundle.APP_VERSION
    assert os.path.basename(stamp["archive_path"]) == (
        f"loop-risk-simulator-gui-{build_bundle.APP_VERSION}.tar.gz"
    )
    assert stamp["simulator_sha"] == "sha-gui-bundle-v0.1.0"
    assert stamp["swift_sha"] == "sha-HEAD"

    archive = stamp["archive_path"]
    assert os.path.isfile(archive)
    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
    # Every artifact the builder claims to stage is actually in the tree. Driven
    # off APP_ARTIFACTS so the assertion cannot drift away from the list.
    for artifact in build_bundle.APP_ARTIFACTS:
        assert f"./{artifact}" in names, artifact
    # The four modules TRSET-47 found missing, named explicitly: their absence is
    # the ImportError the bundle used to die with on first run.
    for module in ("export_bundle.py", "loop_home_renderer.py", "meal_config.py", "start_page.py"):
        assert f"./{module}" in names
    assert "./pytest.ini" in names      # bundled tests run under the repo's markers
    assert "./conda-environment.yml" in names
    assert "./run_simulator_gui.command" in names
    assert "./launcher.py" in names
    assert "./BUNDLE_VERSION.json" in names
    assert "./vendor/sim/_sim_extracted" in names
    assert "./vendor/LoopAlgorithmToPython/_swift_extracted" in names

    # rendered spec inside the archive carries the pin
    with tarfile.open(archive) as tar:
        member = tar.extractfile("./conda-environment.yml").read().decode()
    assert "data-science-simulator@gui-bundle-v0.1.0" in member
    assert build_bundle.SWIFT_VENDOR_RELPATH in member


# --- TRSET-47: the app-artifact drift guard --------------------------------

def test_app_artifacts_cover_this_repos_own_imports():
    """The real list against the real app. This is the assertion that was missing
    while TRSET-7, TRSET-9, TRSET-22 and TRSET-34 each landed a module without
    touching APP_ARTIFACTS -- four bundles that would have died on first run."""
    build_bundle.verify_app_artifacts_complete(REPO_ROOT, build_bundle.APP_ARTIFACTS)


@pytest.mark.parametrize(
    "dropped",
    ["export_bundle.py", "loop_home_renderer.py", "meal_config.py", "start_page.py"],
)
def test_guard_fires_when_any_real_app_module_is_dropped(dropped):
    """Mutation check, one artifact at a time: a guard against a silent-omission
    bug is worthless if it still passes while the omission is possible."""
    trimmed = [a for a in build_bundle.APP_ARTIFACTS if a != dropped]
    with pytest.raises(ValueError, match=re.escape(dropped)):
        build_bundle.verify_app_artifacts_complete(REPO_ROOT, trimmed)


def test_guard_fires_when_the_entry_point_itself_is_unstaged():
    trimmed = [a for a in build_bundle.APP_ARTIFACTS if a != build_bundle.APP_ENTRY_POINT]
    with pytest.raises(ValueError, match=re.escape(build_bundle.APP_ENTRY_POINT)):
        build_bundle.verify_app_artifacts_complete(REPO_ROOT, trimmed)


def test_guard_ignores_stdlib_and_third_party_imports(tmp_path):
    """Only modules that exist in the app repo are required -- "local" is decided by
    the repo's contents, not by a second hardcoded name list."""
    app = _make_app_repo(
        tmp_path,
        entry_source="import os\nimport streamlit as st\nfrom pandas import DataFrame\n",
    )
    build_bundle.verify_app_artifacts_complete(str(app), build_bundle.APP_ARTIFACTS)


def test_guard_follows_one_level_of_indirection(tmp_path):
    """A staged module that itself imports an unstaged local module also raises."""
    app = _make_app_repo(
        tmp_path,
        entry_source="import meal_config\n",
        extra_modules={"meal_config.py": "import helper\n", "helper.py": "# local\n"},
    )
    with pytest.raises(ValueError, match=r"helper\.py \(imported by meal_config\.py\)"):
        build_bundle.verify_app_artifacts_complete(str(app), build_bundle.APP_ARTIFACTS)


def test_guard_does_not_walk_past_one_level(tmp_path):
    """The bound is explicit: helper.py's own local import is NOT chased. This
    documents the limit rather than leaving it to be discovered."""
    app = _make_app_repo(
        tmp_path,
        entry_source="import meal_config\n",
        extra_modules={
            "meal_config.py": "import helper\n",
            "helper.py": "import deep\n",
            "deep.py": "# third level, unstaged and unchecked\n",
        },
    )
    build_bundle.verify_app_artifacts_complete(
        str(app), build_bundle.APP_ARTIFACTS + ["helper.py"]
    )


def test_guard_parses_the_app_without_executing_it(tmp_path):
    """Parse-only, per the stdlib-only constraint: importing streamlit_app here
    would pull streamlit, pandas and the simulator into the build."""
    app = _make_app_repo(
        tmp_path,
        entry_source="import meal_config\nraise SystemExit('app code was executed')\n",
    )
    build_bundle.verify_app_artifacts_complete(str(app), build_bundle.APP_ARTIFACTS)


def test_build_bundle_raises_before_producing_an_archive(tmp_path, monkeypatch):
    """The whole point: a build that would ship a non-starting bundle fails at
    build time, and leaves no archive behind to be published by mistake."""
    app = _make_app_repo(
        tmp_path,
        entry_source=_ENTRY_SOURCE + "import unstaged_module\n",
        extra_modules={"unstaged_module.py": "# never added to APP_ARTIFACTS\n"},
    )
    _mock_git_boundary(monkeypatch)
    out_dir = tmp_path / "dist"

    with pytest.raises(ValueError, match=r"unstaged_module\.py"):
        build_bundle.build_bundle(
            version=build_bundle.APP_VERSION,
            simulator_ref="gui-bundle-v0.1.0",
            simulator_repo="/fake/sim",
            swift_repo="/fake/swift",
            swift_ref="HEAD",
            app_repo=str(app),
            output_dir=str(out_dir),
            built_at="2026-07-22T00:00:00+00:00",
        )

    assert not out_dir.exists()  # no half-built archive to publish


# --- TRSET-35: one version constant governs app and bundle alike -----------

def test_resolve_version_defaults_to_the_app_constant():
    import version

    assert build_bundle.APP_VERSION == version.APP_VERSION
    assert build_bundle.resolve_version() == version.APP_VERSION
    assert build_bundle.resolve_version(None) == version.APP_VERSION


def test_resolve_version_accepts_an_agreeing_explicit_value():
    assert build_bundle.resolve_version(build_bundle.APP_VERSION) == build_bundle.APP_VERSION


def test_resolve_version_raises_on_a_disagreeing_explicit_value():
    """No silent mismatch: a bundle numbered differently from the app inside it is
    the drift this collapses, so the build stops instead of picking a winner."""
    with pytest.raises(ValueError, match=r"disagrees with APP_VERSION"):
        build_bundle.resolve_version("9.9.9")


def test_version_py_is_staged_so_the_bundled_app_can_import_it():
    assert "version.py" in build_bundle.APP_ARTIFACTS
    # Provable rather than asserted: streamlit_app.py imports it, so the drift
    # guard is what enforces this -- dropping it from the list raises.
    with pytest.raises(ValueError, match=r"version\.py"):
        build_bundle.verify_app_artifacts_complete(
            REPO_ROOT, [a for a in build_bundle.APP_ARTIFACTS if a != "version.py"]
        )
