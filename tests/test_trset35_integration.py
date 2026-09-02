"""End-to-end integration test for TRSET-35 version numbering (Feature gate).

One file proves the whole feature: the number the user sees and the number the
bundle is published under come from the same constant, and that constant ships.

Four phases, matching the approved plan:

  1. Both pages -- the version renders on the unacknowledged start page and is
     still there after a real ``Got it`` click advances to the tool. Driven
     through a bare ``AppTest.from_file``, not the shared ``make_app_test``
     factory, so the unacknowledged first render is real (as TRSET-34 does).
  2. Derived, not duplicated -- what is on screen is asserted against
     ``version.APP_VERSION``, never against a literal copied into this file,
     which would only prove the copy matches itself.
  3. Builder/app agreement -- the archive name, ``BUNDLE_VERSION.json``'s
     ``bundle_version`` and the release tag all carry ``APP_VERSION``, and a
     disagreeing explicit ``--version`` raises instead of quietly winning.
  4. version.py ships -- the assembled bundle tree contains it, so the bundled
     app can import the constant it displays.

Documented deviation (workflow §3): phases 3-4 mock ``resolve_ref`` /
``extract_tree_paths``, the same git boundary ``test_build_bundle.py`` mocks, so
they are not unmocked-at-the-boundary in the strict sense. Unmocking would need
real data-science-simulator and LoopAlgorithmToPython checkouts, which that suite
deliberately avoids; the assertions here concern version-string plumbing, not git.
The app repo is NOT mocked -- phases 3-4 build from this repo itself, so
"version.py ships" is a statement about the real tree.
"""

import json
import os
import sys
import tarfile

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

import start_page  # noqa: E402
import streamlit_app  # noqa: E402
import version  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "packaging"))
import build_bundle  # noqa: E402

# No simulation runs here -- the ordinary app-render timeout, as TRSET-34 uses.
RENDER_TIMEOUT_SECONDS = 60

EXPECTED_VERSION_LINE = f"Version {version.APP_VERSION}"


def _unacknowledged_app():
    """The app as a brand-new session sees it: the start-page gate untouched."""
    return AppTest.from_file("streamlit_app.py", default_timeout=RENDER_TIMEOUT_SECONDS)


def _markdown_values(at):
    return [m.value for m in at.markdown]


def _version_lines(at):
    return [v for v in _markdown_values(at) if v.startswith("Version ")]


# ---------------------------------------------------------------------------
# Phase 1 -- the version is on both pages
# ---------------------------------------------------------------------------

def test_phase1_the_start_page_render_carries_the_version():
    at = _unacknowledged_app()
    at.run()
    assert not at.exception

    # Really the gated render, not the tool: this is the page a colleague opening
    # the app for the first time actually sees.
    assert at.session_state[streamlit_app.START_PAGE_ACK_KEY] is False
    assert at.title[0].value == start_page.PAGE_TITLE

    assert _version_lines(at) == [EXPECTED_VERSION_LINE]


def test_phase1_the_version_survives_the_click_through_to_the_tool():
    at = _unacknowledged_app()
    at.run()
    assert _version_lines(at) == [EXPECTED_VERSION_LINE]

    # A real click on the real button, then the explicit run() that renders the
    # page the gate's st.rerun() asked for.
    at.button[0].click().run()
    at.run()
    assert not at.exception
    assert at.session_state[streamlit_app.START_PAGE_ACK_KEY] is True

    # Same single line on the tool page -- one insertion point serving both, not
    # one per page and not two on either.
    assert _version_lines(at) == [EXPECTED_VERSION_LINE]


def test_phase1_the_version_renders_above_the_gate_not_inside_a_page():
    """AC 3: rendered once in main(), so no page module owns it.

    Asserted structurally: the line is present on a render where the start page
    module is the only page rendered, AND on the render where it is gone.
    """
    at = _unacknowledged_app()
    at.run()
    assert start_page.PURPOSE_TEXT in _markdown_values(at)
    assert EXPECTED_VERSION_LINE in _markdown_values(at)

    at.button[0].click().run()
    at.run()
    assert start_page.PURPOSE_TEXT not in _markdown_values(at)
    assert EXPECTED_VERSION_LINE in _markdown_values(at)


def test_phase1_the_version_line_adds_no_raw_html():
    # TRSET-4 invariant: the app's own HTML is the shared chrome only (brand CSS,
    # disclaimer banner, logo). The version line must not have added a fourth.
    at = _unacknowledged_app()
    at.run()
    assert "<" not in EXPECTED_VERSION_LINE
    html_blocks = [v for v in _markdown_values(at) if "<" in v]
    assert len(html_blocks) == 3, f"unexpected raw HTML blocks: {html_blocks!r}"
    assert sum(1 for v in html_blocks if 'role="alert"' in v) == 1
    assert sum(1 for v in html_blocks if f'alt="{streamlit_app.LOGO_ALT_TEXT}"' in v) == 1


# ---------------------------------------------------------------------------
# Phase 2 -- derived from the constant, not duplicated
# ---------------------------------------------------------------------------

def test_phase2_the_rendered_string_is_derived_from_the_constant():
    at = _unacknowledged_app()
    at.run()

    (line,) = _version_lines(at)
    assert line.endswith(version.APP_VERSION)
    # The app imports the constant rather than restating the number.
    assert streamlit_app.APP_VERSION is version.APP_VERSION


def test_phase2_changing_the_constant_changes_what_renders(monkeypatch):
    """Mutation check: if the app held its own literal, this would still say 1.0.0.

    Patched on ``version`` itself, not on ``streamlit_app``: AppTest re-executes
    the script in a fresh namespace, so its ``from version import APP_VERSION``
    re-reads the (already imported, now patched) module -- which is exactly the
    dependency being asserted.
    """
    monkeypatch.setattr(version, "APP_VERSION", "42.7.3")
    at = _unacknowledged_app()
    at.run()
    assert not at.exception
    assert _version_lines(at) == ["Version 42.7.3"]


# ---------------------------------------------------------------------------
# Phase 3 -- the builder and the app agree
# ---------------------------------------------------------------------------

def _mock_git_boundary(monkeypatch):
    """The documented deviation: the git boundary only. The app repo is real."""
    monkeypatch.setattr(build_bundle, "resolve_ref", lambda repo, ref: f"sha-{ref}")

    def fake_extract(repo, ref, paths, dest):
        os.makedirs(dest, exist_ok=True)
        marker = "swift" if "LoopAlgorithmToPython" in dest else "sim"
        with open(os.path.join(dest, f"_{marker}_extracted"), "w") as fh:
            fh.write(ref)

    monkeypatch.setattr(build_bundle, "extract_tree_paths", fake_extract)


@pytest.fixture
def built_bundle(tmp_path, monkeypatch):
    """A bundle built from THIS repo with no --version given at all."""
    _mock_git_boundary(monkeypatch)
    return build_bundle.build_bundle(
        simulator_ref="main",
        simulator_repo="/fake/sim",
        swift_repo="/fake/swift",
        swift_ref="HEAD",
        app_repo=REPO_ROOT,
        output_dir=str(tmp_path / "dist"),
        built_at="2026-09-02T00:00:00+00:00",
    )


def test_phase3_all_three_release_artifacts_carry_the_app_version(built_bundle):
    expected = version.APP_VERSION

    # 1. The archive name.
    assert os.path.basename(built_bundle["archive_path"]) == (
        f"loop-risk-simulator-gui-{expected}.tar.gz"
    )
    # 2. BUNDLE_VERSION.json's bundle_version, read out of the built archive.
    with tarfile.open(built_bundle["archive_path"]) as tar:
        stamp = json.loads(tar.extractfile("./BUNDLE_VERSION.json").read().decode())
    assert stamp["bundle_version"] == expected
    # 3. The release tag in the publish command the builder prints.
    cmd = build_bundle.publish_command(
        built_bundle["archive_path"], stamp["bundle_version"], "tidepool-org/x"
    )
    assert f"gh release create gui-bundle-v{expected} " in cmd


def test_phase3_a_disagreeing_explicit_version_raises(tmp_path):
    """AC 8: no silent mismatch. Raises before any git or staging work, so no
    archive is produced to be published by mistake."""
    out_dir = tmp_path / "dist"
    with pytest.raises(ValueError, match=r"disagrees with APP_VERSION"):
        build_bundle.build_bundle(
            version="0.1.0",
            simulator_ref="main",
            simulator_repo="/fake/sim",
            swift_repo="/fake/swift",
            swift_ref="HEAD",
            app_repo=REPO_ROOT,
            output_dir=str(out_dir),
            built_at="2026-09-02T00:00:00+00:00",
        )
    assert not out_dir.exists()


def test_phase3_the_cli_rejects_a_disagreeing_version(tmp_path):
    with pytest.raises(ValueError, match=r"disagrees with APP_VERSION"):
        build_bundle.main([
            "build",
            "--version", "0.1.0",
            "--app-repo", REPO_ROOT,
            "--output-dir", str(tmp_path / "dist"),
        ])


def test_phase3_the_cli_version_argument_is_optional(tmp_path, monkeypatch, capsys):
    """AC 7 at the CLI: the documented build command no longer needs --version,
    and the archive it writes is numbered from the constant."""
    _mock_git_boundary(monkeypatch)
    out_dir = tmp_path / "dist"

    assert build_bundle.main([
        "build",
        "--app-repo", REPO_ROOT,
        "--output-dir", str(out_dir),
        "--simulator-repo", "/fake/sim",
        "--swift-repo", "/fake/swift",
    ]) == 0

    expected = version.APP_VERSION
    assert (out_dir / f"loop-risk-simulator-gui-{expected}.tar.gz").is_file()
    # The publish command it prints carries the same number, not a stale argument.
    assert f"gh release create gui-bundle-v{expected} " in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Phase 4 -- version.py ships
# ---------------------------------------------------------------------------

def test_phase4_the_assembled_bundle_contains_version_py(built_bundle):
    with tarfile.open(built_bundle["archive_path"]) as tar:
        names = set(tar.getnames())
        shipped = tar.extractfile("./version.py").read().decode()

    assert "./version.py" in names
    # ...and it is the real module, carrying the real constant -- not an empty
    # file that happens to have the right name.
    assert f'APP_VERSION = "{version.APP_VERSION}"' in shipped
    # The app that imports it ships alongside.
    assert "./streamlit_app.py" in names
