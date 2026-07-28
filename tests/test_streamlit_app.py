"""
Smoke/rendering tests for streamlit_app.py, driven by streamlit's own
AppTest harness against the real scenario_configs/ library.

These check the view layer renders correctly in isolation (library listing,
scope selector, results rendering for happy/no-data/cancelled cases). The
deeper end-to-end behaviors (errors actually blocking a run, warnings from a
real bad config, a real background run completing) are covered by the
separately-approved Phase 3 integration test plan, not duplicated here.
"""

import sys

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

sys.path.insert(0, "post_processing")
from tidepool_data_science_simulator.projects.risk.gui_runner import RunResult, RiskDirRunResult  # noqa: E402
from severity_model import SeverityAssessment, StageResult  # noqa: E402


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


def test_happy_path_result_renders_table_and_ungated_premit_choice():
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

    premit_selectbox = [sb for sb in at.selectbox if "pre-mitigation" in sb.label.lower()][0]
    # TWI-0006 constraint: never auto-collapse to a single pre-mitigation figure --
    # the choice must start unselected, not defaulted to 'Pre-mitigation'.
    assert premit_selectbox.value is None


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
    assert any(t.value == "Tidepool Loop Risk Assessment" for t in at.title)
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
