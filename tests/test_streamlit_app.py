"""
Smoke/rendering tests for streamlit_app.py, driven by streamlit's own
AppTest harness against the real scenario_configs/ library.

These check the view layer renders correctly in isolation (library listing,
scope selector, results rendering for happy/no-data/cancelled cases). The
deeper end-to-end behaviors (errors actually blocking a run, warnings from a
real bad config, a real background run completing) are covered by the
separately-approved Phase 3 integration test plan, not duplicated here.
"""

import datetime
import io
import os
import sys
import zipfile

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

sys.path.insert(0, "post_processing")
# Project root (holds streamlit_app.py) so its pure helpers can be imported
# directly for unit testing, independent of the AppTest exec harness.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tidepool_data_science_simulator.projects.risk.gui_runner import RunResult, RiskDirRunResult  # noqa: E402
from severity_model import SeverityAssessment, StageResult  # noqa: E402
from streamlit_app import _export_chart_files, _profile_label, _profile_stage_rows  # noqa: E402
import meal_config  # noqa: E402
import streamlit_app  # noqa: E402

_TEST_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_data")
# The two real captured run TSVs committed for TRSET-22: a 'pre' stage and a
# 'no_loop' stage of the same profile. 'post' is deliberately absent, so these
# also exercise the missing-stage placeholder.
_PRE_TSV = os.path.join(_TEST_DATA_DIR, "pre-Loop_NoMitigations_t1_median.tsv")
_NO_LOOP_TSV = os.path.join(_TEST_DATA_DIR, "pre-noLoop_t1_median.tsv")


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
    at.radio(key="run_scope").set_value("One specific directory").run()

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
# _profile_label: VP-profile label read back from a profile-bearing filename
# (TRSET-2, generalized in TRSET-23). Presentation-only -- derives the profile
# from names the run already produces: the figure name gui_runner writes,
# "{risk_dir}_{scenario_json_name}_{ts}.png", or the scenario config filename
# itself, which is what labels the Loop-home chart rows.
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
def test_profile_label_extracts_profile_from_figure_name(risk_dir_name, png_name, expected):
    assert _profile_label(f"/some/dir/{png_name}", risk_dir_name) == expected


@pytest.mark.parametrize("risk_dir_name, scenario_config_name, expected", [
    (
        "TLR-553",
        "Simulation-Configuration-TLR-553_Adolescent_profile.json",
        "Adolescent profile",
    ),
    (
        "TLR-909_02_05",
        "Simulation-Configuration-TLR-909_02_05_t2_sensitive_profile_v1.json",
        "T2 sensitive profile",
    ),
    (
        # Lowercase "_profile" and a version suffix normalize the same way.
        "TLR-620",
        "Simulation-Configuration-TLR-620_median_profile_v1.json",
        "Median profile",
    ),
])
def test_profile_label_extracts_profile_from_scenario_config_name(
    risk_dir_name, scenario_config_name, expected
):
    # TRSET-23: the same parser labels the Loop-home chart rows straight from the
    # scenario config filename -- one config file is one VP profile.
    assert _profile_label(scenario_config_name, risk_dir_name) == expected


def test_profile_label_returns_none_for_unexpected_name():
    # An off-pattern name must not raise -- callers fall back to the raw name.
    assert _profile_label("/some/dir/unexpected.png", "TLR-909") is None


# ---------------------------------------------------------------------------
# _profile_stage_rows: (profile -> {stage -> trace path}) grouping (TRSET-23)
# ---------------------------------------------------------------------------

def test_profile_stage_rows_groups_by_profile_and_classifies_stages():
    trace_paths = {
        "Simulation-Configuration-TLR-553_Median_profile.json": {
            "pre-Loop_NoMitigations_t1_median": "/runs/pre_median.tsv",
            "pre-noLoop_t1_median": "/runs/noloop_median.tsv",
            "post-Loop-WithMitigations_t1_median": "/runs/post_median.tsv",
        },
        "Simulation-Configuration-TLR-553_Adolescent_profile.json": {
            "pre-Loop_NoMitigations_t1_adolescent": "/runs/pre_adolescent.tsv",
        },
    }
    rows = _profile_stage_rows(trace_paths, "TLR-553")

    # Sorted by label, so render order is deterministic regardless of the
    # filesystem order build_risk_sim_generator happened to yield.
    assert [label for label, _ in rows] == ["Adolescent profile", "Median profile"]
    assert rows[0][1] == {"pre": "/runs/pre_adolescent.tsv"}
    assert rows[1][1] == {
        "pre": "/runs/pre_median.tsv",
        "no_loop": "/runs/noloop_median.tsv",
        "post": "/runs/post_median.tsv",
    }


def test_profile_stage_rows_omits_unclassifiable_sim_ids():
    # An unrecognized sim_id prefix must not be forced into a stage -- it drops
    # out, and that column then renders the explicit "No data" placeholder.
    trace_paths = {
        "Simulation-Configuration-TLR-590_Median_profile.json": {
            "pre-Loop_NoMitigations_t1_median": "/runs/pre.tsv",
            # Real spelling from the library that severity_model does not match.
            "post-Loop_withMitigations_t1_median": "/runs/post.tsv",
        },
    }
    rows = _profile_stage_rows(trace_paths, "TLR-590")
    assert rows[0][1] == {"pre": "/runs/pre.tsv"}


def test_profile_stage_rows_falls_back_to_raw_name_when_unparseable():
    rows = _profile_stage_rows({"weird_name.json": {"pre-noLoop_x": "/runs/x.tsv"}}, "TLR-1")
    assert rows == [("weird_name.json", {"no_loop": "/runs/x.tsv"})]


def test_profile_stage_rows_empty_for_no_traces():
    assert _profile_stage_rows({}, "TLR-1") == []


# ---------------------------------------------------------------------------
# Loop-home chart rendering in the results pane (TRSET-23)
# ---------------------------------------------------------------------------

def _chart_result(trace_paths, risk_dir_name="TLR-TEST"):
    return RunResult(
        save_dir="/tmp/fake",
        risk_dir_results=[
            RiskDirRunResult(risk_dir_name, _make_fake_assessment(), [], trace_paths)
        ],
        cancelled=False,
    )


def test_loop_home_charts_render_three_coequal_stage_columns():
    # Real captured run TSVs for 'pre' and 'no_loop'; 'post' is absent. All three
    # stage columns must still render (TWI-0006 2.g.ii -- co-equal, never
    # collapsed), with the missing one carrying an explicit "No data".
    fake_result = _chart_result({
        "Simulation-Configuration-TLR-TEST_Median_profile.json": {
            "pre-Loop_NoMitigations_t1_median": _PRE_TSV,
            "pre-noLoop_t1_median": _NO_LOOP_TSV,
        },
    })
    at = AppTest.from_file("streamlit_app.py", default_timeout=120)
    at.session_state["run_result"] = fake_result
    at.run()

    assert not at.exception
    assert len(at.image) == 2
    assert [i.value for i in at.info] == ["No data"]

    markdown_values = [m.value for m in at.markdown]
    assert "**Median profile**" in markdown_values
    # Every stage header is present, in severity_model's order.
    stage_headers = [v for v in markdown_values if v in ("*Pre-mitigation*", "*No Loop*", "*Post-mitigation*")]
    assert stage_headers == ["*Pre-mitigation*", "*No Loop*", "*Post-mitigation*"]
    # No selector gates the stages.
    assert not [sb for sb in at.selectbox if "stage" in sb.label.lower()]


def test_generic_simulator_pngs_are_no_longer_rendered():
    # TRSET-23 replaces the generic three-panel PNGs. png_paths may still be
    # populated by gui_runner, but nothing renders them.
    fake_result = RunResult(
        save_dir="/tmp/fake",
        risk_dir_results=[
            RiskDirRunResult("TLR-TEST", _make_fake_assessment(), [_PRE_TSV], {})
        ],
        cancelled=False,
    )
    at = AppTest.from_file("streamlit_app.py", default_timeout=30)
    at.session_state["run_result"] = fake_result
    at.run()

    assert not at.exception
    assert len(at.image) == 0


def test_unreadable_trace_is_surfaced_not_swallowed():
    fake_result = _chart_result({
        "Simulation-Configuration-TLR-TEST_Median_profile.json": {
            "pre-Loop_NoMitigations_t1_median": "/does/not/exist.tsv",
        },
    })
    at = AppTest.from_file("streamlit_app.py", default_timeout=30)
    at.session_state["run_result"] = fake_result
    at.run()

    assert not at.exception
    assert any("could not render" in w.value.lower() for w in at.warning)
    # A failed read is distinguishable from an absent stage: the other two
    # columns still say "No data", this one does not.
    assert [i.value for i in at.info] == ["No data", "No data"]


def test_no_traces_reports_it_rather_than_rendering_nothing():
    at = AppTest.from_file("streamlit_app.py", default_timeout=30)
    at.session_state["run_result"] = _chart_result({})
    at.run()

    assert not at.exception
    assert any("no simulation traces" in c.value.lower() for c in at.caption)


# ---------------------------------------------------------------------------
# Export control (TRSET-7)
# ---------------------------------------------------------------------------

def _export_buttons(at):
    return [b for b in at.button if b.label == "Export results"]


def _fake_save_dir(tmp_path, tlr_dir_name="TLR-TEST"):
    """A minimal completed-run save_dir: metadata.json plus one TLR dir.

    Enough for the real export_bundle to archive; the RTF renderer itself needs a
    real run's summary CSVs, so it is stubbed in the tests that get that far and
    exercised for real in test_trset7_integration.py.
    """
    save_dir = tmp_path / "Risk_Run_2026-08-05T09:15:00.123456"
    (save_dir / tlr_dir_name).mkdir(parents=True)
    (save_dir / "metadata.json").write_text('{"timestamp": "2026-08-05T09:15:00.123456"}')
    (save_dir / tlr_dir_name / "summary_results_Simulation-Configuration-TLR-TEST.csv").write_text("sim_id\n")
    return str(save_dir)


@pytest.fixture
def stub_summary_writer(monkeypatch):
    """Stub the RTF renderer at its export_bundle seam.

    AppTest re-execs streamlit_app.py per run, and its `from export_bundle import
    build_export_zip` resolves through the already-imported export_bundle module,
    so patching the attribute there does reach the app's copy.
    """
    import export_bundle

    monkeypatch.setattr(export_bundle, "process_results_directory", lambda results_dir: None)


def test_no_export_control_before_a_run():
    # Nothing to export until a run has produced results.
    at = AppTest.from_file("streamlit_app.py", default_timeout=30)
    at.run()

    assert not at.exception
    assert not _export_buttons(at)
    assert not at.download_button


def test_no_export_control_for_a_cancelled_run():
    # A cancelled run has no complete set of outputs, so it is not exportable.
    fake_result = RunResult(
        save_dir="/tmp/fake",
        risk_dir_results=[RiskDirRunResult("TLR-TEST", _make_fake_assessment(), [])],
        cancelled=True,
    )
    at = AppTest.from_file("streamlit_app.py", default_timeout=30)
    at.session_state["run_result"] = fake_result
    at.run()

    assert not at.exception
    assert not _export_buttons(at)
    assert not at.download_button


def test_completed_run_offers_the_export_control_but_no_download_yet():
    at = AppTest.from_file("streamlit_app.py", default_timeout=30)
    at.session_state["run_result"] = _chart_result({})
    at.run()

    assert not at.exception
    assert len(_export_buttons(at)) == 1
    # The zip is built on activation, not speculatively on every rerun.
    assert not at.download_button


def test_export_click_builds_a_zip_and_offers_it_for_download(tmp_path, stub_summary_writer):
    save_dir = _fake_save_dir(tmp_path)
    fake_result = RunResult(
        save_dir=save_dir,
        risk_dir_results=[
            RiskDirRunResult("TLR-TEST", _make_fake_assessment(), [], {
                "Simulation-Configuration-TLR-TEST_Median_profile.json": {
                    "pre-Loop_NoMitigations_t1_median": _PRE_TSV,
                    "pre-noLoop_t1_median": _NO_LOOP_TSV,
                },
            })
        ],
        cancelled=False,
    )
    at = AppTest.from_file("streamlit_app.py", default_timeout=120)
    at.session_state["run_result"] = fake_result
    at.run()
    _export_buttons(at)[0].click().run()

    assert not at.exception
    assert not at.error
    assert len(at.download_button) == 1
    assert at.download_button[0].label == "Download export (.zip)"
    zip_path = at.session_state["export_zip_path"]
    # Named for the run it came from, in a temp dir -- no hardcoded location.
    assert os.path.basename(zip_path) == "risk_run_2026-08-05T09_15_00.123456.zip"
    assert os.path.isfile(zip_path)
    assert os.path.dirname(zip_path) == at.session_state["export_temp_dir"]

    # The offered file is the real archive, carrying the run's files and the
    # charts named for the two stages this profile has.
    import zipfile

    with zipfile.ZipFile(at.session_state["export_zip_path"]) as archive:
        names = archive.namelist()
    root = "risk_run_2026-08-05T09_15_00.123456"
    assert f"{root}/metadata.json" in names
    assert f"{root}/charts/TLR-TEST_Median_profile_Pre-mitigation.png" in names
    assert f"{root}/charts/TLR-TEST_Median_profile_No_Loop.png" in names
    assert f"{root}/charts/TLR-TEST_Median_profile_Post-mitigation.png" not in names


def test_export_failure_surfaces_as_an_error_and_offers_no_download():
    # save_dir has no metadata.json (and does not exist at all), so the export
    # must fail loudly rather than hand over a summary-free zip.
    at = AppTest.from_file("streamlit_app.py", default_timeout=30)
    at.session_state["run_result"] = _chart_result({})
    at.run()
    _export_buttons(at)[0].click().run()

    assert not at.exception
    assert any("export failed" in e.value.lower() for e in at.error)
    assert not at.download_button


def test_export_reports_the_charts_it_skipped(tmp_path, stub_summary_writer):
    save_dir = _fake_save_dir(tmp_path)
    fake_result = RunResult(
        save_dir=save_dir,
        risk_dir_results=[
            RiskDirRunResult("TLR-TEST", _make_fake_assessment(), [], {
                "Simulation-Configuration-TLR-TEST_Median_profile.json": {
                    "pre-Loop_NoMitigations_t1_median": "/does/not/exist.tsv",
                },
            })
        ],
        cancelled=False,
    )
    at = AppTest.from_file("streamlit_app.py", default_timeout=120)
    at.session_state["run_result"] = fake_result
    at.run()
    _export_buttons(at)[0].click().run()

    assert not at.exception
    skipped_warnings = [w.value for w in at.warning if "not exported" in w.value]
    assert len(skipped_warnings) == 1
    # The unreadable stage names the file and stays distinguishable from the two
    # stages that simply have no trace.
    assert "could not render exist.tsv" in skipped_warnings[0]
    assert skipped_warnings[0].count("no simulation trace for this stage") == 2
    # Skipped charts do not block the export of everything else.
    assert len(at.download_button) == 1


def test_export_chart_files_names_every_rendered_chart_by_tlr_profile_and_stage():
    """Unit-level: the (filename, bytes) pairs handed to export_bundle.

    Absent and unreadable stages are reported, never emitted as blank or
    fabricated charts.
    """
    result = RiskDirRunResult("TLR-TEST", _make_fake_assessment(), [], {
        "Simulation-Configuration-TLR-TEST_Median_profile.json": {
            "pre-Loop_NoMitigations_t1_median": _PRE_TSV,
            "pre-noLoop_t1_median": _NO_LOOP_TSV,
            "post-Loop-WithMitigations_t1_median": "/does/not/exist.tsv",
        },
    })

    charts, skipped = _export_chart_files(result)

    assert [name for name, _ in charts] == [
        "TLR-TEST_Median_profile_Pre-mitigation.png",
        "TLR-TEST_Median_profile_No_Loop.png",
    ]
    assert all(png.startswith(b"\x89PNG\r\n\x1a\n") for _, png in charts)
    assert len(skipped) == 1
    assert "TLR-TEST / Median profile / Post-mitigation" in skipped[0]
    assert "could not render exist.tsv" in skipped[0]


# ---------------------------------------------------------------------------
# The meal/bolus editor (TRSET-9)
# ---------------------------------------------------------------------------

def _editor_app():
    at = AppTest.from_file("streamlit_app.py", default_timeout=60)
    at.run()
    at.radio(key="config_source").set_value(streamlit_app.SOURCE_CONFIGURE).run()
    return at


def test_config_source_defaults_to_the_library_so_the_existing_flow_is_unchanged():
    at = AppTest.from_file("streamlit_app.py", default_timeout=60)
    at.run()

    assert at.radio(key="config_source").value == streamlit_app.SOURCE_LIBRARY
    assert at.selectbox(key="config_collection") is not None
    assert not [b for b in at.button if b.label == "Generate configs"]


def test_the_editor_offers_no_run_until_configs_are_generated():
    at = _editor_app()

    assert [b.label for b in at.button] == ["Generate configs"]
    assert any("Generate configs to validate and run" in i.value for i in at.info)


def test_a_bolus_with_no_dose_reports_the_error_and_stays_unrunnable():
    """A dose is never guessed: blank means blank, and generation says so."""
    at = _editor_app()
    at.button[0].click().run()

    assert not at.exception
    assert len(at.error) == 1
    assert "needs a numeric units value" in at.error[0].value
    assert not [b for b in at.button if b.label == "Run Tool"]
    assert at.session_state["generated_configs"] is None


def test_generating_configs_makes_them_downloadable_and_runnable():
    at = _editor_app()
    at.number_input(key="pm_bolus_units_0").set_value(3.3).run()
    at.button[0].click().run()

    assert not at.error, [e.value for e in at.error]
    assert len(at.session_state["generated_configs"]) == 4
    assert [d.label for d in at.download_button] == ["Download configs (.zip)"]
    assert [b for b in at.button if b.label == "Run Tool"]


def test_the_independent_pump_timeline_does_not_offer_the_patient_side_sentinel():
    """It is invalid under patient.pump.bolus_entries, so it is never offered."""
    at = _editor_app()
    at.toggle(key="timelines_aligned").set_value(False).run()

    kind_selectboxes = [sb for sb in at.selectbox if sb.label.endswith("value type")]
    assert len(kind_selectboxes) == 1, [sb.label for sb in kind_selectboxes]
    assert kind_selectboxes[0].key == "pm_bolus_kind_0"
    assert any(label.startswith("Pump:") for label in
               [n.label for n in at.number_input])


def test_custom_mode_offers_a_grams_input_and_multiplier_mode_a_multiplier():
    at = _editor_app()
    at.radio(key="meal_mode").set_value("Custom carb value").run()
    assert at.number_input(key="pm_meal_grams_0") is not None

    at.radio(key="meal_mode").set_value("Multiplier of standard").run()
    assert at.number_input(key="pm_meal_multiplier_0").step == meal_config.MULTIPLIER_STEP


def test_the_config_download_zip_nests_under_one_folder():
    configs = meal_config.generate_configs(
        meal_config.MealConfigSpec.aligned(
            meal_config.MODE_STANDARD,
            meal_config.EntrySet(meals=[meal_config.MealEntry(datetime.time(12, 0))]),
        ),
        "TLR-20260806-143000",
    )

    payload = streamlit_app.generated_configs_zip(configs, "TLR-20260806-143000")

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
    assert len(names) == 4
    assert all(name.startswith("TLR-20260806-143000/") for name in names)
