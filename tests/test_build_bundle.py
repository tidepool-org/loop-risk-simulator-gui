"""
Unit tests for packaging/build_bundle.py -- the Phase 4 bundle builder.

Tests the packaging logic in isolation: env-spec rendering (pin resolution +
Swift-line swap), version stamping, app-code staging, publish-command format,
and full assembly with the git boundary (resolve_ref / extract_tree_paths)
mocked so no network, git, or real simulator checkout is needed.
"""

import json
import os
import sys
import tarfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "packaging"))
import build_bundle  # noqa: E402


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


# --- full assembly with the git boundary mocked ----------------------------

def test_build_bundle_assembles_expected_tree(tmp_path, monkeypatch):
    # Fake GUI repo
    app = tmp_path / "gui"
    (app / "packaging" / "templates").mkdir(parents=True)
    (app / ".streamlit").mkdir()
    (app / "tests").mkdir()
    (app / "streamlit_app.py").write_text("# app")
    (app / "Tidepool_Logo_Light_Large_3000.jpg").write_text("jpg")
    (app / "README.md").write_text("# readme")
    (app / "conda-environment.yml").write_text(_SOURCE_ENV)
    (app / "packaging" / "templates" / "run_simulator_gui.command").write_text("#!/bin/bash\n")

    # Mock the git boundary: resolve_ref returns a fake SHA; extract_tree_paths
    # drops a marker file so we can assert vendoring happened.
    monkeypatch.setattr(build_bundle, "resolve_ref", lambda repo, ref: f"sha-{ref}")

    def fake_extract(repo, ref, paths, dest):
        os.makedirs(dest, exist_ok=True)
        marker = "swift" if "LoopAlgorithmToPython" in dest else "sim"
        with open(os.path.join(dest, f"_{marker}_extracted"), "w") as fh:
            fh.write(ref)

    monkeypatch.setattr(build_bundle, "extract_tree_paths", fake_extract)

    out_dir = tmp_path / "dist"
    stamp = build_bundle.build_bundle(
        version="0.1.0",
        simulator_ref="gui-bundle-v0.1.0",
        simulator_repo="/fake/sim",
        swift_repo="/fake/swift",
        swift_ref="HEAD",
        app_repo=str(app),
        output_dir=str(out_dir),
        built_at="2026-07-22T00:00:00+00:00",
    )

    assert stamp["simulator_sha"] == "sha-gui-bundle-v0.1.0"
    assert stamp["swift_sha"] == "sha-HEAD"

    archive = stamp["archive_path"]
    assert os.path.isfile(archive)
    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
    assert "./streamlit_app.py" in names
    assert "./conda-environment.yml" in names
    assert "./run_simulator_gui.command" in names
    assert "./BUNDLE_VERSION.json" in names
    assert "./vendor/sim/_sim_extracted" in names
    assert "./vendor/LoopAlgorithmToPython/_swift_extracted" in names

    # rendered spec inside the archive carries the pin
    with tarfile.open(archive) as tar:
        member = tar.extractfile("./conda-environment.yml").read().decode()
    assert "data-science-simulator@gui-bundle-v0.1.0" in member
    assert build_bundle.SWIFT_VENDOR_RELPATH in member
