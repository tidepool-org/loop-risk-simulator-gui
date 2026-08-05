"""Assemble one downloadable zip for a completed GUI risk run (TRSET-7).

Deliberately streamlit-free. streamlit_app.py is the view layer (see its
docstring); writing the severity-summary RTFs and building the archive is not
presentation, and keeping it here means the zip's contents can be asserted
directly instead of through AppTest.

The RTFs come from ``create_severity_summary.process_results_directory`` used
as-is -- reached through gui_runner, which is the single validated entry point
and already puts the simulator's non-packaged ``post_processing/`` dir on
sys.path -- so an exported summary is byte-identical to the CLI's.
"""

import os
import re
import zipfile
from typing import Iterable, Tuple

from tidepool_data_science_simulator.projects.risk.gui_runner import (
    METADATA_FILENAME,
    process_results_directory,
)

# Charts are loose files in one folder rather than mirroring the app's
# profile x stage grid: only the filename carries identity (TRSET-7 scope).
CHARTS_DIR_NAME = "charts"

# The run directory create_save_dir() makes is "Risk_Run_<ISO timestamp>"; the
# export is named for the same timestamp so a downloaded zip is traceable back
# to the run directory it came from.
RUN_DIR_PREFIX = "Risk_Run_"
EXPORT_STEM_PREFIX = "risk_run_"

# ISO timestamps and VP-profile labels both carry characters that are awkward or
# illegal in filenames (":" and " " respectively), so every name token is passed
# through this rather than trusted.
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize(token: str) -> str:
    """Collapse each run of filesystem-unsafe characters in token to one "_"."""
    return _UNSAFE_CHARS.sub("_", token).strip("_")


def chart_filename(risk_dir_name: str, profile_label: str, stage_display: str) -> str:
    """Filesystem-safe ``<TLR dir>_<profile>_<stage>.png`` name for one chart.

    The three tokens are the chart's full identity, since the charts are loose
    files. Two distinct sim_ids in one config file never classify to the same
    stage (the invariant _profile_stage_rows already relies on), so the triple is
    unique within a run.
    """
    tokens = [_sanitize(token) for token in (risk_dir_name, profile_label, stage_display)]
    return "_".join(token for token in tokens if token) + ".png"


def export_root_name(save_dir: str) -> str:
    """``risk_run_<sanitized run timestamp>`` -- the zip's stem and top folder.

    Everything is nested under this single folder so unzipping produces one
    directory instead of scattering TLR dirs into the download location.
    """
    basename = os.path.basename(os.path.normpath(save_dir))
    if basename.startswith(RUN_DIR_PREFIX):
        basename = basename[len(RUN_DIR_PREFIX):]
    return EXPORT_STEM_PREFIX + _sanitize(basename)


def _tlr_dir_names(save_dir: str) -> list:
    """The TLR-* subdirectories of save_dir -- the ones that actually ran.

    Matches how process_results_directory finds its work, so the guard below
    fails on exactly the directories it would silently do nothing for.
    """
    return sorted(
        name for name in os.listdir(save_dir)
        if name.startswith("TLR-") and os.path.isdir(os.path.join(save_dir, name))
    )


def build_export_zip(
    save_dir: str,
    charts: Iterable[Tuple[str, bytes]],
    dest_dir: str,
) -> str:
    """Write one zip of a completed run plus its charts, and return its path.

    Writes the ``risk_summary_<sim_id>.rtf`` summaries into save_dir's TLR dirs
    first (via process_results_directory), then archives everything under
    save_dir -- summary CSVs, ``<sim_id>.tsv`` traces, figures, ``loop_algo_io/``
    and the new RTFs -- alongside ``charts/``, all nested under one top folder.
    Files are written into the archive one at a time, so a large run does not
    have to fit in memory.

    ``charts`` is ``(filename, png_bytes)`` pairs, already named by
    ``chart_filename``. Raises ValueError if save_dir is missing metadata.json or
    has no TLR-* directory (the two cases process_results_directory only prints
    and returns on, which would otherwise yield a zip with no summaries), or if
    two charts claim the same filename.
    """
    if not os.path.isfile(os.path.join(save_dir, METADATA_FILENAME)):
        raise ValueError(
            f"Cannot export {save_dir}: {METADATA_FILENAME} is missing, so the "
            "severity summaries cannot be dated or written."
        )
    if not _tlr_dir_names(save_dir):
        raise ValueError(f"Cannot export {save_dir}: it contains no TLR-* directories.")

    process_results_directory(save_dir)

    root = export_root_name(save_dir)
    zip_path = os.path.join(dest_dir, f"{root}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for dirpath, _, filenames in os.walk(save_dir):
            for filename in sorted(filenames):
                file_path = os.path.join(dirpath, filename)
                archive.write(
                    file_path,
                    os.path.join(root, os.path.relpath(file_path, save_dir)),
                )
        written_charts = set()
        for filename, png_bytes in charts:
            if filename in written_charts:
                raise ValueError(f"Two charts resolved to the same filename: {filename}")
            written_charts.add(filename)
            archive.writestr(os.path.join(root, CHARTS_DIR_NAME, filename), png_bytes)
    return zip_path
