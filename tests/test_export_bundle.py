"""Unit tests for export_bundle.py -- the TRSET-7 zip/RTF assembly.

``process_results_directory`` is monkeypatched here: these tests target the
archive's own shape (top folder, arcnames, charts folder, guard rails), and the
real renderer needs a real run's summary CSVs to produce anything. The real
chain -- real run -> real RTF renderer -> real zip -- is exercised without mocks
in test_trset7_integration.py, per the approved plan.
"""

import json
import os
import zipfile

import pytest

import export_bundle


METADATA = {"timestamp": "2026-08-05T09:15:00.123456"}
RUN_DIR_NAME = "Risk_Run_2026-08-05T09:15:00.123456"
EXPECTED_ROOT = "risk_run_2026-08-05T09_15_00.123456"


@pytest.fixture
def save_dir(tmp_path):
    """A minimal stand-in for a completed run's save_dir.

    Mirrors the real layout: metadata.json at the top, one TLR-* dir holding a
    summary CSV, a per-sim trace, the three-panel figure, and loop_algo_io/.
    """
    run_dir = tmp_path / RUN_DIR_NAME
    tlr_dir = run_dir / "TLR-909"
    (tlr_dir / "loop_algo_io").mkdir(parents=True)
    (run_dir / "metadata.json").write_text(json.dumps(METADATA))
    (tlr_dir / "summary_results_Simulation-Configuration-TLR-909.csv").write_text("sim_id\n")
    (tlr_dir / "pre-Loop_NoMitigations_t1_adolescent.tsv").write_text("time\tbg\n")
    (tlr_dir / "TLR-909_scenario_2026-08-05.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tlr_dir / "loop_algo_io" / "loop_algo_input_2019-08-15T12:00:00Z.json").write_text("{}")
    return str(run_dir)


@pytest.fixture
def stub_summary_writer(monkeypatch):
    """Stand in for the RTF renderer, recording its call and writing a marker RTF."""
    calls = []

    def _write_summaries(results_dir):
        calls.append(results_dir)
        for name in os.listdir(results_dir):
            tlr_dir = os.path.join(results_dir, name)
            if name.startswith("TLR-") and os.path.isdir(tlr_dir):
                with open(os.path.join(tlr_dir, f"risk_summary_{name}.rtf"), "w") as rtf:
                    rtf.write(r"{\rtf1 stub}")

    monkeypatch.setattr(export_bundle, "process_results_directory", _write_summaries)
    return calls


def _archived_names(zip_path):
    with zipfile.ZipFile(zip_path) as archive:
        return sorted(archive.namelist())


# ---------------------------------------------------------------------------
# Name construction
# ---------------------------------------------------------------------------

def test_chart_filename_sanitizes_the_spaces_real_profile_labels_carry():
    assert export_bundle.chart_filename(
        "TLR-909", "Adolescent profile", "Pre-mitigation"
    ) == "TLR-909_Adolescent_profile_Pre-mitigation.png"


def test_chart_filename_sanitizes_the_space_in_the_no_loop_stage_label():
    assert export_bundle.chart_filename(
        "TLR-909", "Median profile", "No Loop"
    ) == "TLR-909_Median_profile_No_Loop.png"


def test_chart_filename_keeps_all_three_identity_tokens_distinct():
    """TLR dir, profile and stage are the whole identity of a loose chart file --
    two charts differing in any one of them must not collide."""
    names = {
        export_bundle.chart_filename(risk_dir, profile, stage)
        for risk_dir in ("TLR-909", "TLR-910")
        for profile in ("Adolescent profile", "Median profile")
        for stage in ("Pre-mitigation", "No Loop", "Post-mitigation")
    }
    assert len(names) == 12


def test_chart_filename_survives_an_unparseable_profile_label():
    """_profile_label falls back to the raw config filename when a name doesn't
    parse, so the sanitizer has to cope with one."""
    name = export_bundle.chart_filename(
        "TLR-909", "Simulation-Configuration-TLR-909_odd, name.json", "No Loop"
    )
    assert name == "TLR-909_Simulation-Configuration-TLR-909_odd_name.json_No_Loop.png"
    assert " " not in name and ":" not in name


def test_export_root_name_strips_the_run_prefix_and_sanitizes_the_timestamp():
    assert export_bundle.export_root_name(f"/tmp/results/{RUN_DIR_NAME}") == EXPECTED_ROOT
    # Colons (illegal on some filesystems, awkward everywhere) are gone.
    assert ":" not in export_bundle.export_root_name(f"/tmp/results/{RUN_DIR_NAME}")


def test_export_root_name_tolerates_a_trailing_separator():
    assert export_bundle.export_root_name(f"/tmp/results/{RUN_DIR_NAME}/") == EXPECTED_ROOT


# ---------------------------------------------------------------------------
# Archive shape
# ---------------------------------------------------------------------------

def test_zip_nests_everything_under_one_top_folder(save_dir, stub_summary_writer, tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()

    zip_path = export_bundle.build_export_zip(
        save_dir, [("TLR-909_Median_profile_No_Loop.png", b"\x89PNG\r\n\x1a\n")], str(dest)
    )

    assert os.path.basename(zip_path) == f"{EXPECTED_ROOT}.zip"
    names = _archived_names(zip_path)
    assert names, "archive is empty"
    assert all(name.startswith(f"{EXPECTED_ROOT}/") for name in names), names


def test_zip_carries_every_run_file_at_its_path_relative_to_save_dir(
    save_dir, stub_summary_writer, tmp_path
):
    zip_path = export_bundle.build_export_zip(save_dir, [], str(tmp_path))

    names = _archived_names(zip_path)
    assert names == [
        f"{EXPECTED_ROOT}/TLR-909/TLR-909_scenario_2026-08-05.png",
        f"{EXPECTED_ROOT}/TLR-909/loop_algo_io/loop_algo_input_2019-08-15T12:00:00Z.json",
        f"{EXPECTED_ROOT}/TLR-909/pre-Loop_NoMitigations_t1_adolescent.tsv",
        f"{EXPECTED_ROOT}/TLR-909/risk_summary_TLR-909.rtf",
        f"{EXPECTED_ROOT}/TLR-909/summary_results_Simulation-Configuration-TLR-909.csv",
        f"{EXPECTED_ROOT}/metadata.json",
    ]


def test_summaries_are_written_into_save_dir_before_the_archive_is_built(
    save_dir, stub_summary_writer, tmp_path
):
    """Order matters: the RTFs are produced at export time, so they have to land
    in save_dir before it is walked or the zip would ship without them."""
    zip_path = export_bundle.build_export_zip(save_dir, [], str(tmp_path))

    assert stub_summary_writer == [save_dir]
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.read(f"{EXPECTED_ROOT}/TLR-909/risk_summary_TLR-909.rtf").startswith(b"{\\rtf1")


def test_charts_go_into_a_charts_folder_with_their_bytes_intact(
    save_dir, stub_summary_writer, tmp_path
):
    charts = [
        ("TLR-909_Median_profile_Pre-mitigation.png", b"\x89PNG\r\n\x1a\npre"),
        ("TLR-909_Median_profile_Post-mitigation.png", b"\x89PNG\r\n\x1a\npost"),
    ]

    zip_path = export_bundle.build_export_zip(save_dir, charts, str(tmp_path))

    with zipfile.ZipFile(zip_path) as archive:
        for filename, png_bytes in charts:
            assert archive.read(f"{EXPECTED_ROOT}/{export_bundle.CHARTS_DIR_NAME}/{filename}") == png_bytes


def test_no_charts_still_produces_the_run_export(save_dir, stub_summary_writer, tmp_path):
    """A run whose stages all failed to render is still exportable -- the raw
    outputs and summaries are the point; charts are additive."""
    zip_path = export_bundle.build_export_zip(save_dir, [], str(tmp_path))

    assert not [n for n in _archived_names(zip_path) if export_bundle.CHARTS_DIR_NAME in n]


# ---------------------------------------------------------------------------
# Guard rails -- the cases process_results_directory only prints and returns on
# ---------------------------------------------------------------------------

def test_missing_metadata_raises_instead_of_exporting_a_summary_free_zip(
    save_dir, stub_summary_writer, tmp_path
):
    os.remove(os.path.join(save_dir, "metadata.json"))

    with pytest.raises(ValueError, match="metadata.json"):
        export_bundle.build_export_zip(save_dir, [], str(tmp_path))

    assert stub_summary_writer == [], "must fail before doing any work"
    assert not [n for n in os.listdir(tmp_path) if n.endswith(".zip")], (
        "must not leave a partial zip behind"
    )


def test_a_run_directory_with_no_tlr_dirs_raises(save_dir, stub_summary_writer, tmp_path):
    import shutil

    shutil.rmtree(os.path.join(save_dir, "TLR-909"))

    with pytest.raises(ValueError, match="no TLR-\\* directories"):
        export_bundle.build_export_zip(save_dir, [], str(tmp_path))


def test_two_charts_claiming_one_filename_raise_rather_than_silently_overwrite(
    save_dir, stub_summary_writer, tmp_path
):
    duplicate = ("TLR-909_Median_profile_No_Loop.png", b"\x89PNG\r\n\x1a\n")

    with pytest.raises(ValueError, match="same filename"):
        export_bundle.build_export_zip(save_dir, [duplicate, duplicate], str(tmp_path))


# ---------------------------------------------------------------------------
# GUI-generated scenario configs (TRSET-9)
# ---------------------------------------------------------------------------

GENERATED = [
    ("Simulation-Configuration-TLR-20260806-143000_Median_Profile.json", b'{"median": 1}'),
    ("Simulation-Configuration-TLR-20260806-143000_Sensitive_Profile.json", b'{"sensitive": 1}'),
]


def test_generated_configs_go_into_their_own_folder_with_their_bytes_intact(
    save_dir, stub_summary_writer, tmp_path
):
    zip_path = export_bundle.build_export_zip(
        save_dir, [], str(tmp_path), generated_configs=GENERATED
    )

    with zipfile.ZipFile(zip_path) as archive:
        for filename, config_bytes in GENERATED:
            archived = archive.read(
                f"{EXPECTED_ROOT}/{export_bundle.GENERATED_CONFIGS_DIR_NAME}/{filename}"
            )
            assert archived == config_bytes


def test_a_library_run_exports_no_generated_configs_folder(
    save_dir, stub_summary_writer, tmp_path
):
    """The parameter defaults to empty, so today's library-run export is unchanged."""
    zip_path = export_bundle.build_export_zip(save_dir, [], str(tmp_path))

    assert not [
        name for name in _archived_names(zip_path)
        if export_bundle.GENERATED_CONFIGS_DIR_NAME in name
    ]


def test_two_generated_configs_claiming_one_filename_raise(
    save_dir, stub_summary_writer, tmp_path
):
    with pytest.raises(ValueError, match="same filename"):
        export_bundle.build_export_zip(
            save_dir, [], str(tmp_path), generated_configs=[GENERATED[0], GENERATED[0]]
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
