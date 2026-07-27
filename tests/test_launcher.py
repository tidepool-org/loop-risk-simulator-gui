"""
Unit tests for packaging/launcher.py -- the Phase 5 colleague-facing launcher
helper.

Tests the decision + provisioning logic in isolation with the filesystem,
subprocess, and network boundaries mocked, so no conda, no Xcode, no real
Miniconda download, and no arm64 machine are needed. The heavy end-to-end
behaviour (a real bundle -> provision -> run) is covered separately by
tests/test_phase5_launcher_integration.py.
"""

import hashlib
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "packaging"))
import launcher  # noqa: E402


# --- helpers ---------------------------------------------------------------

def _completed(stdout=""):
    """A fake subprocess.CompletedProcess with the given stdout."""
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _arch_run(machine):
    """A fake `run` that answers the platform.machine() probe with `machine`."""
    def run(cmd, **kwargs):
        return _completed(stdout=f"{machine}\n")
    return run


# --- arm64 conda discovery -------------------------------------------------

def test_verify_conda_arch_reads_base_python(tmp_path):
    conda = tmp_path / "bin" / "conda"
    conda.parent.mkdir(parents=True)
    conda.write_text("#!/bin/sh\n")
    (tmp_path / "bin" / "python").write_text("#!/bin/sh\n")
    assert launcher.verify_conda_arch(str(conda), run=_arch_run("arm64")) == "arm64"


def test_verify_conda_arch_none_when_base_python_missing(tmp_path):
    conda = tmp_path / "bin" / "conda"
    conda.parent.mkdir(parents=True)
    conda.write_text("#!/bin/sh\n")  # no sibling python
    assert launcher.verify_conda_arch(str(conda), run=_arch_run("arm64")) is None


def test_verify_conda_arch_none_when_probe_raises(tmp_path):
    conda = tmp_path / "bin" / "conda"
    conda.parent.mkdir(parents=True)
    conda.write_text("#!/bin/sh\n")
    (tmp_path / "bin" / "python").write_text("#!/bin/sh\n")

    def boom(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    assert launcher.verify_conda_arch(str(conda), run=boom) is None


def _make_conda(root, name, arch, machines):
    """Create an executable fake conda under root/name and register its arch."""
    conda = root / name / "bin" / "conda"
    conda.parent.mkdir(parents=True)
    conda.write_text("#!/bin/sh\n")
    conda.chmod(0o755)
    python = root / name / "bin" / "python"
    python.write_text("#!/bin/sh\n")
    machines[str(python)] = arch
    return str(conda)


def test_find_arm64_conda_selects_arm64_skips_intel(tmp_path):
    machines = {}
    intel = _make_conda(tmp_path, "anaconda3", "x86_64", machines)  # noqa: F841
    arm = _make_conda(tmp_path, "miniconda3", "arm64", machines)

    def run(cmd, **kwargs):
        return _completed(stdout=machines.get(cmd[0], "unknown") + "\n")

    candidates = [
        str(tmp_path / "anaconda3" / "bin" / "conda"),
        str(tmp_path / "miniconda3" / "bin" / "conda"),
    ]
    assert launcher.find_arm64_conda(candidates=candidates, run=run) == arm


def test_find_arm64_conda_none_when_only_intel(tmp_path):
    machines = {}
    intel = _make_conda(tmp_path, "anaconda3", "x86_64", machines)

    def run(cmd, **kwargs):
        return _completed(stdout=machines.get(cmd[0], "unknown") + "\n")

    assert launcher.find_arm64_conda(candidates=[intel], run=run) is None


def test_candidate_paths_prefer_conda_exe_and_exclude_intel_default():
    candidates = launcher._candidate_conda_paths(home="/Users/x", environ={"CONDA_EXE": "/custom/conda"})
    assert candidates[0] == "/custom/conda"
    assert "/opt/anaconda3/bin/conda" not in candidates  # ambient Intel default never probed


# --- reconcile -------------------------------------------------------------

STAMP = {"simulator_sha": "a" * 40, "swift_sha": "b" * 40, "bundle_version": "0.1.0"}


def test_reconcile_provision_when_no_state():
    assert launcher.reconcile(STAMP, None) == launcher.PROVISION


def test_reconcile_provision_when_incomplete():
    assert launcher.reconcile(STAMP, {"simulator_sha": "a" * 40, "swift_sha": "b" * 40}) == launcher.PROVISION


def test_reconcile_reuse_when_shas_match():
    state = {"complete": True, "simulator_sha": "a" * 40, "swift_sha": "b" * 40}
    assert launcher.reconcile(STAMP, state) == launcher.REUSE


def test_reconcile_provision_when_simulator_sha_differs():
    state = {"complete": True, "simulator_sha": "c" * 40, "swift_sha": "b" * 40}
    assert launcher.reconcile(STAMP, state) == launcher.PROVISION


def test_reconcile_provision_when_swift_sha_differs():
    state = {"complete": True, "simulator_sha": "a" * 40, "swift_sha": "d" * 40}
    assert launcher.reconcile(STAMP, state) == launcher.PROVISION


# --- state round-trip ------------------------------------------------------

def test_state_round_trip(tmp_path):
    root = str(tmp_path / "root")
    assert launcher.read_state(root) is None  # absent -> None
    payload = {"complete": True, "simulator_sha": "a" * 40, "env_prefix": "/x/env"}
    launcher.write_state(root, payload)
    assert launcher.read_state(root) == payload


def test_read_state_none_on_corrupt(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / launcher.STATE_FILENAME).write_text("{not json")
    assert launcher.read_state(str(root)) is None


def test_read_stamp_raises_when_absent(tmp_path):
    with pytest.raises(launcher.LauncherError):
        launcher.read_stamp(str(tmp_path))


def test_read_stamp_raises_when_corrupt(tmp_path):
    (tmp_path / launcher.BUNDLE_STAMP_FILENAME).write_text("{bad")
    with pytest.raises(launcher.LauncherError):
        launcher.read_stamp(str(tmp_path))


# --- app support root ------------------------------------------------------

def test_app_support_root_derived_from_home():
    root = launcher.app_support_root(home="/Users/someone")
    assert root == "/Users/someone/Library/Application Support/loop-risk-simulator-gui"


# --- symlink planning + application -----------------------------------------

def test_plan_symlinks_maps_both_orphan_paths():
    plan = launcher.plan_symlinks("/sp", "/vendor")
    assert plan["/sp/scenario_configs"] == "/vendor/scenario_configs"
    assert plan["/sp/severity_model.py"] == "/vendor/post_processing/severity_model.py"


def test_apply_symlinks_creates_and_replaces(tmp_path):
    sp = tmp_path / "site-packages"
    sp.mkdir()
    vendor = tmp_path / "vendor"
    (vendor / "scenario_configs").mkdir(parents=True)
    (vendor / "post_processing").mkdir(parents=True)
    (vendor / "post_processing" / "severity_model.py").write_text("# sev")
    # Pre-existing plain file where a link must go -> must be replaced.
    (sp / "severity_model.py").write_text("stale")

    plan = launcher.plan_symlinks(str(sp), str(vendor))
    launcher.apply_symlinks(plan)

    assert os.path.islink(sp / "scenario_configs")
    assert os.path.islink(sp / "severity_model.py")
    assert os.path.realpath(sp / "severity_model.py") == os.path.realpath(vendor / "post_processing" / "severity_model.py")


def test_apply_symlinks_raises_on_missing_target(tmp_path):
    sp = tmp_path / "sp"
    sp.mkdir()
    plan = launcher.plan_symlinks(str(sp), str(tmp_path / "nope"))
    with pytest.raises(launcher.LauncherError):
        launcher.apply_symlinks(plan)


# --- Swift .dylib + Xcode CLT ----------------------------------------------

def test_needs_dylib_build(tmp_path):
    swift = tmp_path / "swift"
    (swift / "loop_to_python_api").mkdir(parents=True)
    assert launcher.needs_dylib_build(str(swift)) is True
    (swift / "loop_to_python_api" / launcher.DYLIB_NAME).write_text("binary")
    assert launcher.needs_dylib_build(str(swift)) is False


def test_ensure_xcode_clt_raises_when_absent():
    def missing(cmd, **kwargs):
        raise subprocess.CalledProcessError(2, cmd)

    with pytest.raises(launcher.LauncherError):
        launcher.ensure_xcode_clt(run=missing)


def test_ensure_xcode_clt_ok_when_present():
    launcher.ensure_xcode_clt(run=lambda cmd, **kw: _completed(stdout="/Library/Developer/CommandLineTools\n"))


# --- Miniconda bootstrap ----------------------------------------------------

def _valid_installer_download(content=b"installer-bytes"):
    """A fake download writing `content`; returns (download_fn, sha256)."""
    sha = hashlib.sha256(content).hexdigest()

    def download(url, dest):
        with open(dest, "wb") as fh:
            fh.write(content)

    return download, sha


def test_bootstrap_verifies_checksum_before_install(tmp_path, monkeypatch):
    download, sha = _valid_installer_download()
    monkeypatch.setattr(launcher, "MINICONDA_SHA256", sha)
    prefix = tmp_path / "miniconda"
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        # simulate the installer creating the conda executable
        (prefix / "bin").mkdir(parents=True, exist_ok=True)
        (prefix / "bin" / "conda").write_text("#!/bin/sh\n")
        return _completed()

    conda = launcher.bootstrap_miniconda(str(prefix), download=download, run=run)
    assert conda == str(prefix / "bin" / "conda")
    assert calls and calls[0][0] == "bash" and "-b" in calls[0] and "-p" in calls[0]


def test_bootstrap_rejects_bad_checksum_and_does_not_run(tmp_path, monkeypatch):
    download, _ = _valid_installer_download(b"tampered")
    monkeypatch.setattr(launcher, "MINICONDA_SHA256", "0" * 64)  # deliberate mismatch
    ran = []

    def run(cmd, **kwargs):
        ran.append(cmd)
        return _completed()

    with pytest.raises(launcher.LauncherError, match="integrity check"):
        launcher.bootstrap_miniconda(str(tmp_path / "miniconda"), download=download, run=run)
    assert ran == []  # never executed the tampered installer


def test_bootstrap_bails_actionably_when_offline(tmp_path):
    def download(url, dest):
        raise OSError("network down")

    with pytest.raises(launcher.LauncherError, match="internet"):
        launcher.bootstrap_miniconda(str(tmp_path / "miniconda"), download=download, run=lambda *a, **k: _completed())


# --- resolve_conda: discovery-first, bootstrap fallback --------------------

def test_resolve_conda_prefers_discovered(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "find_arm64_conda", lambda **kw: "/found/conda")
    called = {"bootstrap": False}
    monkeypatch.setattr(launcher, "bootstrap_miniconda",
                        lambda *a, **k: called.__setitem__("bootstrap", True) or "/x")
    assert launcher.resolve_conda(str(tmp_path)) == "/found/conda"
    assert called["bootstrap"] is False  # no download when one is discovered


def test_resolve_conda_bootstraps_when_none_found(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "find_arm64_conda", lambda **kw: None)
    monkeypatch.setattr(launcher, "bootstrap_miniconda", lambda prefix, **kw: prefix + "/bin/conda")
    result = launcher.resolve_conda(str(tmp_path))
    assert result.endswith("miniconda/bin/conda")


# --- provision orchestration (subprocess/network mocked) -------------------

def _fake_bundle(tmp_path):
    """A minimal bundle tree with the stamp + vendored orphan paths + swift dir."""
    bundle = tmp_path / "bundle"
    (bundle / "vendor" / "sim" / "scenario_configs").mkdir(parents=True)
    (bundle / "vendor" / "sim" / "post_processing").mkdir(parents=True)
    (bundle / "vendor" / "sim" / "post_processing" / "severity_model.py").write_text("# sev")
    swift = bundle / "vendor" / "LoopAlgorithmToPython" / "loop_to_python_api"
    swift.mkdir(parents=True)
    (swift / launcher.DYLIB_NAME).write_text("prebuilt")  # dylib present -> no build
    (bundle / "conda-environment.yml").write_text("name: x\n")
    with open(bundle / launcher.BUNDLE_STAMP_FILENAME, "w") as fh:
        json.dump(STAMP, fh)
    return bundle


def test_provision_full_path_creates_env_symlinks_and_state(tmp_path, monkeypatch):
    bundle = _fake_bundle(tmp_path)
    root = tmp_path / "root"

    monkeypatch.setattr(launcher, "resolve_conda", lambda *a, **k: "/fake/conda")

    def fake_run(cmd, **kwargs):
        # env create -> materialise the prefix python
        if "env" in cmd and "create" in cmd:
            prefix = cmd[cmd.index("-p") + 1]
            os.makedirs(os.path.join(prefix, "bin"), exist_ok=True)
            with open(os.path.join(prefix, "bin", "python"), "w") as fh:
                fh.write("#!/bin/sh\n")
            return _completed()
        # site-packages probe
        if any("sysconfig" in str(c) for c in cmd):
            sp = os.path.join(str(root), "site-packages")
            os.makedirs(sp, exist_ok=True)
            return _completed(stdout=sp + "\n")
        return _completed()

    env_python = launcher.provision(str(bundle), root=str(root), run=fake_run,
                                    download=lambda *a, **k: None)

    assert env_python == os.path.join(str(root), "env", "bin", "python")
    # symlinks placed beside the "installed package"
    sp = os.path.join(str(root), "site-packages")
    assert os.path.islink(os.path.join(sp, "scenario_configs"))
    assert os.path.islink(os.path.join(sp, "severity_model.py"))
    # vendored orphan paths copied into the durable root (survives bundle moves)
    assert os.path.isdir(os.path.join(str(root), launcher.VENDOR_SIM_DIRNAME, "scenario_configs"))
    # state marker written, complete, with the bundle SHAs
    state = launcher.read_state(str(root))
    assert state["complete"] is True
    assert state["simulator_sha"] == STAMP["simulator_sha"]


def test_provision_reuses_intact_install_without_reprovision(tmp_path, monkeypatch):
    bundle = _fake_bundle(tmp_path)
    root = tmp_path / "root"

    # Pre-seed an intact, version-matching install.
    env_prefix = root / "env"
    (env_prefix / "bin").mkdir(parents=True)
    (env_prefix / "bin" / "python").write_text("#!/bin/sh\n")
    sp = root / "site-packages"
    sp.mkdir(parents=True)
    vendor_sim = root / launcher.VENDOR_SIM_DIRNAME
    (vendor_sim / "scenario_configs").mkdir(parents=True)
    (vendor_sim / "post_processing").mkdir(parents=True)
    (vendor_sim / "post_processing" / "severity_model.py").write_text("# sev")
    launcher.apply_symlinks(launcher.plan_symlinks(str(sp), str(vendor_sim)))
    launcher.write_state(str(root), {
        "complete": True,
        "simulator_sha": STAMP["simulator_sha"],
        "swift_sha": STAMP["swift_sha"],
        "env_prefix": str(env_prefix),
        "vendor_sim_root": str(vendor_sim),
    })

    def run(cmd, **kwargs):
        if any("sysconfig" in str(c) for c in cmd):
            return _completed(stdout=str(sp) + "\n")
        raise AssertionError(f"reuse path must not run provisioning commands: {cmd}")

    def no_resolve(*a, **k):
        raise AssertionError("reuse path must not resolve/bootstrap conda")

    monkeypatch.setattr(launcher, "resolve_conda", no_resolve)

    env_python = launcher.provision(str(bundle), root=str(root), run=run)
    assert env_python == str(env_prefix / "bin" / "python")


def test_provision_reprovisions_when_state_stale(tmp_path, monkeypatch):
    bundle = _fake_bundle(tmp_path)
    root = tmp_path / "root"
    # Stale state: different simulator SHA -> must reprovision.
    launcher.write_state(str(root), {
        "complete": True, "simulator_sha": "z" * 40, "swift_sha": STAMP["swift_sha"],
        "env_prefix": str(root / "env"), "vendor_sim_root": str(root / "vendor_sim"),
    })
    resolved = {"called": False}

    def fake_resolve(*a, **k):
        resolved["called"] = True
        return "/fake/conda"

    monkeypatch.setattr(launcher, "resolve_conda", fake_resolve)

    def fake_run(cmd, **kwargs):
        if "env" in cmd and "create" in cmd:
            prefix = cmd[cmd.index("-p") + 1]
            os.makedirs(os.path.join(prefix, "bin"), exist_ok=True)
            open(os.path.join(prefix, "bin", "python"), "w").close()
            return _completed()
        if any("sysconfig" in str(c) for c in cmd):
            sp = os.path.join(str(root), "site-packages")
            os.makedirs(sp, exist_ok=True)
            return _completed(stdout=sp + "\n")
        return _completed()

    launcher.provision(str(bundle), root=str(root), run=fake_run, download=lambda *a, **k: None)
    assert resolved["called"] is True  # stale -> reprovision happened


# --- CLI -------------------------------------------------------------------

def test_main_prints_env_python_on_success(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(launcher, "provision", lambda bundle_dir: "/env/bin/python")
    rc = launcher.main(["provision", "--bundle-dir", str(tmp_path)])
    out = capsys.readouterr()
    assert rc == 0
    assert out.out.strip() == "/env/bin/python"


def test_main_reports_launcher_error_without_traceback(tmp_path, monkeypatch, capsys):
    def boom(bundle_dir):
        raise launcher.LauncherError("no arm64 conda and offline")

    monkeypatch.setattr(launcher, "provision", boom)
    rc = launcher.main(["provision", "--bundle-dir", str(tmp_path)])
    out = capsys.readouterr()
    assert rc == 1
    assert "no arm64 conda and offline" in out.err
    assert out.out.strip() == ""  # nothing captured as the env python on failure
