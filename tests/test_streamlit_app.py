"""
Smoke/rendering tests for streamlit_app.py, driven by streamlit's own
AppTest harness against the real scenario_configs/ library.

These check the view layer renders correctly in isolation (library listing,
scope selector, results rendering for happy/no-data/cancelled cases). The
deeper end-to-end behaviors (errors actually blocking a run, warnings from a
real bad config, a real background run completing) are covered by the
separately-approved Phase 3 integration test plan, not duplicated here.
"""

import os
import sys

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

sys.path.insert(0, "post_processing")
# Project root (holds streamlit_app.py) so its pure helpers can be imported
# directly for unit testing, independent of the AppTest exec harness.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tidepool_data_science_simulator.projects.risk.gui_runner import RunResult, RiskDirRunResult  # noqa: E402
from severity_model import SeverityAssessment, StageResult  # noqa: E402
from streamlit_app import _plot_caption  # noqa: E402


def _make_fake_assessment():
    stage = StageResult(
        stage="pre", harm_type="Hypoglycemia", severity="3", tir="70.0", tbr="10.0", tar="5.0",
        lbgi_score_avg=3, dka_score_avg=1, hyperglycemia_score=0, n_sims=4,
        lbgi_value_avg="2.5", dka_index_value_avg="21.91",
    )
    return SeverityAssessment(
        simulation_id="TLR-TEST", subdirectory_name="TLR-TEST", timestamp="2026-07-21T00:00:00",
        profile_count=4, stages={"pre": stage, "no_loop": stage, "post": stage},
    )


def test_app_loads_and_lists_real_collections():
    at = AppTest.from_file("streamlit_app.py", default_timeout=30)
    at.run()
    assert not at.exception
    collection_selectbox = at.selectbox[0]
    assert collection_selectbox.label == "Config collection"
    assert len(collection_selectbox.options) > 0


def test_logo_renders_with_numeric_width_and_alt_text():
    # TRSET-3: the logo moved off st.logo (no numeric size / no alt) to an
    # explicitly-sized HTML <img>. Guard the width (1.5x baseline = 240px) and
    # the exact alt-text against regressions. AppTest sees the element tree, not
    # pixels, so we assert on the rendered <img> markup.
    at = AppTest.from_file("streamlit_app.py", default_timeout=30)
    at.run()
    assert not at.exception
    logo_imgs = [m for m in at.markdown if "<img" in m.value and 'alt="Tidepool logo"' in m.value]
    assert len(logo_imgs) == 1
    assert 'width="240"' in logo_imgs[0].value


def test_brand_css_restores_material_icon_font():
    # TRSET-3: the DM Sans `!important` rule was clobbering Streamlit's Material
    # icon font, so expander toggle glyphs rendered as the literal ligature text
    # "keyboard_arrow_down" and overlapped the header label. Guard the override
    # that restores the icon font.
    at = AppTest.from_file("streamlit_app.py", default_timeout=30)
    at.run()
    assert not at.exception
    css_blocks = [
        m.value for m in at.markdown
        if '[data-testid="stIconMaterial"]' in m.value
    ]
    assert len(css_blocks) == 1
    assert "Material Symbols Rounded" in css_blocks[0]
def test_header_description_and_run_button_copy():
    # TRSET-2 presentation copy: retitled header, a short description near it,
    # and the run button relabeled to "Run Tool".
    at = AppTest.from_file("streamlit_app.py", default_timeout=30)
    at.run()
    assert not at.exception

    assert at.title[0].value == "Tidepool Loop Risk Severity Estimation Tool"

    markdown_text = " ".join(m.value for m in at.markdown)
    assert "virtual-patient scenarios" in markdown_text

    assert any(b.label == "Run Tool" for b in at.button)


def test_single_directory_scope_populates_tlr_selectbox():
    at = AppTest.from_file("streamlit_app.py", default_timeout=30)
    at.run()
    at.selectbox[0].select("loop_risk_v2_2_0_full").run()
    at.radio[0].set_value("One specific directory").run()

    assert not at.exception
    labels = [sb.label for sb in at.selectbox]
    assert "TLR-* directory" in labels
    tlr_selectbox = [sb for sb in at.selectbox if sb.label == "TLR-* directory"][0]
    assert all("TLR-" in opt for opt in tlr_selectbox.options)


def test_happy_path_result_renders_table():
    fake_result = RunResult(
        save_dir="/tmp/fake",
        risk_dir_results=[RiskDirRunResult("TLR-TEST", _make_fake_assessment(), [])],
        cancelled=False,
    )
    at = AppTest.from_file("streamlit_app.py", default_timeout=30)
    at.session_state["run_result"] = fake_result
    at.run()

    assert not at.exception
    assert [e.label for e in at.expander] == ["TLR-TEST"]
    assert len(at.dataframe) == 1

    # LBGI/DKAI surface as their own columns, positioned between TBR % and TAR %
    # (mirroring the RTF table order), carrying the truncated raw-index strings.
    stage_df = at.dataframe[0].value
    cols = list(stage_df.columns)
    assert "LBGI" in cols and "DKAI" in cols
    assert cols.index("LBGI") == cols.index("TBR %") + 1
    assert cols.index("DKAI") == cols.index("TAR %") - 1
    assert stage_df["LBGI"].iloc[0] == "2.5"
    assert stage_df["DKAI"].iloc[0] == "21.91"


def test_which_phase_dropdown_is_removed():
    # TRSET-2: the inert "which phase" pre-mitigation-figure selectbox was
    # removed. No selectbox referencing the pre-mitigation figure should render.
    fake_result = RunResult(
        save_dir="/tmp/fake",
        risk_dir_results=[RiskDirRunResult("TLR-TEST", _make_fake_assessment(), [])],
        cancelled=False,
    )
    at = AppTest.from_file("streamlit_app.py", default_timeout=30)
    at.session_state["run_result"] = fake_result
    at.run()

    assert not at.exception
    assert not [sb for sb in at.selectbox if "pre-mitigation" in sb.label.lower()]
    assert not [sb for sb in at.selectbox if "phase" in sb.label.lower()]


def test_no_usable_data_renders_warning_not_crash():
    fake_result = RunResult(
        save_dir="/tmp/fake",
        risk_dir_results=[RiskDirRunResult("TLR-EMPTY", None, [])],
        cancelled=False,
    )
    at = AppTest.from_file("streamlit_app.py", default_timeout=30)
    at.session_state["run_result"] = fake_result
    at.run()

    assert not at.exception
    assert len(at.warning) >= 1
    assert any("no usable data" in w.value.lower() for w in at.warning)


def test_integration_full_app_run_renders_header_and_logo():
    # TRSET-3 Feature requirement: end-to-end via AppTest against the real
    # config library, asserting the header surface (title + collections) and the
    # reworked logo both render correctly in a single full app run -- not with a
    # mocked result, but the real import-time library listing exercised.
    at = AppTest.from_file("streamlit_app.py", default_timeout=30)
    at.run()
    assert not at.exception

    # Header: title present and the real library populated the collection selector.
    assert any(t.value == "Tidepool Loop Risk Severity Estimation Tool" for t in at.title)
    collection_selectbox = [sb for sb in at.selectbox if sb.label == "Config collection"][0]
    assert len(collection_selectbox.options) > 0

    # Logo: explicitly-sized HTML <img> with 1.5x-baseline width and exact alt-text.
    logo_imgs = [m for m in at.markdown if "<img" in m.value and 'alt="Tidepool logo"' in m.value]
    assert len(logo_imgs) == 1
    assert 'width="240"' in logo_imgs[0].value

    # Header overlap guard: the icon-font override ships in the full run.
    assert any(
        '[data-testid="stIconMaterial"]' in m.value and "Material Symbols Rounded" in m.value
        for m in at.markdown
    )


def _banner_markdowns(at):
    # The disclaimer is the app's only role="alert" markdown box.
    return [m for m in at.markdown if 'role="alert"' in m.value]


def test_disclaimer_banner_renders_above_logo_with_exact_text():
    # TRSET-5 Feature integration test: full AppTest run against the REAL config
    # library (mirroring test_integration_full_app_run_renders_header_and_logo),
    # asserting the disclaimer banner renders once, with the exact verbatim text,
    # at the top of the page -- above the logo <img>.
    at = AppTest.from_file("streamlit_app.py", default_timeout=30)
    at.run()
    assert not at.exception

    banners = _banner_markdowns(at)
    assert len(banners) == 1, "expected exactly one disclaimer alert banner"
    banner = banners[0]

    # Exact wording is pinned here as a literal (the spec), independent of the
    # app's DISCLAIMER_TEXT constant, so a change to that constant is caught.
    exact_text = (
        "The Tidepool Risk Severity Evaluation Tool is not medical software. "
        "It is intended only for risk exploration and must not be used to make "
        "insulin dosing decisions."
    )
    assert exact_text in banner.value
    assert "⚠" in banner.value

    # Top-of-page ordering: the banner precedes the logo <img> in render order.
    markdown_values = [m.value for m in at.markdown]
    banner_idx = markdown_values.index(banner.value)
    logo_idx = next(i for i, v in enumerate(markdown_values) if "<img" in v)
    assert banner_idx < logo_idx, "disclaimer banner must render above the logo"


def test_disclaimer_banner_does_not_collide_with_st_warning():
    # The banner is a custom role="alert" markdown box, NOT st.warning -- so a
    # no-data run still surfaces exactly its own warning and the banner text does
    # not leak into at.warning (guards the TRSET-3 at.warning assertions).
    fake_result = RunResult(
        save_dir="/tmp/fake",
        risk_dir_results=[RiskDirRunResult("TLR-EMPTY", None, [])],
        cancelled=False,
    )
    at = AppTest.from_file("streamlit_app.py", default_timeout=30)
    at.session_state["run_result"] = fake_result
    at.run()

    assert not at.exception
    assert len(_banner_markdowns(at)) == 1
    assert not any("not medical software" in w.value.lower() for w in at.warning)


def test_cancelled_run_renders_cancellation_warning():
    fake_result = RunResult(
        save_dir="/tmp/fake",
        risk_dir_results=[RiskDirRunResult("TLR-TEST", _make_fake_assessment(), [])],
        cancelled=True,
    )
    at = AppTest.from_file("streamlit_app.py", default_timeout=30)
    at.session_state["run_result"] = fake_result
    at.run()

    assert not at.exception
    assert any("cancelled" in w.value.lower() for w in at.warning)


# ---------------------------------------------------------------------------
# _plot_caption: VP-profile label read back from the figure filename
# (TRSET-2). Presentation-only -- derives the profile from the name gui_runner
# already writes, "{risk_dir}_{scenario_json_name}_{ts}.png".
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("risk_dir_name, png_name, expected", [
    (
        "TLR-909_02_05",
        "TLR-909_02_05_Simulation-Configuration-TLR-909_02_05_adolescent_profile_v1.json_2026-07-21T00:00:00.png",
        "Adolescent profile",
    ),
    (
        "TLR-909_02_05",
        "TLR-909_02_05_Simulation-Configuration-TLR-909_02_05_t2_sensitive_profile_v1.json_2026-07-21T00:00:00.png",
        "T2 sensitive profile",
    ),
    (
        # Mixed-case "_Profile" and no version suffix normalize the same way.
        "TLR-1053",
        "TLR-1053_Simulation-Configuration-TLR-1053_Sensitive_Profile.json_2026-07-21T00:00:00.png",
        "Sensitive profile",
    ),
])
def test_plot_caption_extracts_profile(risk_dir_name, png_name, expected):
    assert _plot_caption(f"/some/dir/{png_name}", risk_dir_name) == expected


def test_plot_caption_returns_none_for_unexpected_name():
    # An off-pattern filename must not raise -- the image renders uncaptioned.
    assert _plot_caption("/some/dir/unexpected.png", "TLR-909") is None
