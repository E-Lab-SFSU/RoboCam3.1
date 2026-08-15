"""
Tests for run-level title persistence in ExperimentRunner.run():
the experiment.json manifest, title sanitisation for on-disk paths, and the
title/sub-label carried into image filenames and per-well metadata.

Image mode is used throughout — it exercises the whole run() body (manifest,
CSV, well loop, filenames) without needing the raw-burst machinery.
"""
import csv
import json
from pathlib import Path

import numpy as np
import pytest

from robocam.experiment import EXPERIMENT_MANIFEST_NAME, ExperimentRunner


class _FakeMotion:
    """Minimal motion controller: already homed, no profile (skips the ETA path)."""
    supports_profiles = False

    def __init__(self):
        self.is_homed = True
        self.X = self.Y = self.Z = 0.0
        self.moves = []

    def home(self):
        self.is_homed = True

    def move_absolute(self, X=None, Y=None, Z=None):
        self.moves.append((X, Y, Z))
        self.X, self.Y, self.Z = X, Y, Z

    def read_profiles(self):
        return {}


class _FakeCamera:
    backend = "playerone"

    def get_frame(self):
        return np.zeros((8, 10, 3), dtype=np.uint8)

    def get_exposure(self):
        return 10_000

    def get_camera_meta(self):
        return {"backend": "playerone", "bit_depth": 8, "bayer_pattern": "mono"}


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = ExperimentRunner(motion_controller=_FakeMotion(), camera=_FakeCamera())
    r.out_dir = str(tmp_path / "outputs")
    Path(r.out_dir).mkdir(parents=True, exist_ok=True)
    return r


def _run(runner, name, labels=("A1",), **kw):
    positions = [(float(i), float(i), 0.0) for i in range(len(labels))]
    runner.run(name=name, positions=positions, labels=list(labels),
               delay_per_well=0.0, mode="image", image_format="png", **kw)
    return Path(runner.last_exp_dir)


class TestManifest:
    def test_written_with_title_and_wells(self, runner):
        exp_dir = _run(runner, "stentor assay #4", labels=("A1-treated", "A2-ctrl"))
        manifest = json.loads((exp_dir / EXPERIMENT_MANIFEST_NAME).read_text())

        assert manifest["experiment_name"] == "stentor assay #4"
        assert manifest["experiment_name_safe"] == "stentor_assay_4"
        assert manifest["completed"] is True
        assert manifest["well_count"] == 2
        assert manifest["wells"][0] == {
            "label": "A1-treated", "well": "A1", "sub_label": "treated",
            "x": 0.0, "y": 0.0, "z": 0.0,
        }

    def test_marks_a_stopped_run_incomplete_but_still_stamps_finish(self, runner):
        """A partial folder is only useful if it says it's partial."""
        real_move = runner.motion.move_absolute

        def stop_after_first(**kw):
            real_move(**kw)
            runner.running = False          # what the Stop button does

        runner.motion.move_absolute = stop_after_first
        exp_dir = _run(runner, "run1", labels=("A1", "A2", "A3"))

        manifest = json.loads((exp_dir / EXPERIMENT_MANIFEST_NAME).read_text())
        assert manifest["completed"] is False
        assert manifest["finished_at"] is not None
        assert manifest["well_count"] == 3
        assert manifest["wells_captured"] < 3

    def test_errored_run_still_closes_the_manifest(self, runner, monkeypatch):
        def boom():
            raise RuntimeError("camera exploded")

        monkeypatch.setattr(runner.camera, "get_frame", boom)
        exp_dir = _run(runner, "run1")

        manifest = json.loads((exp_dir / EXPERIMENT_MANIFEST_NAME).read_text())
        assert manifest["completed"] is False
        assert manifest["finished_at"] is not None

    def test_records_capture_settings(self, runner):
        exp_dir = _run(runner, "run1")
        manifest = json.loads((exp_dir / EXPERIMENT_MANIFEST_NAME).read_text())
        assert manifest["mode"] == "image"
        assert manifest["image_format"] == "png"
        assert manifest["use_laser"] is False
        assert manifest["started_at"] and manifest["finished_at"]

    def test_unwritable_manifest_is_logged_not_raised(self, tmp_path):
        """Bookkeeping must never take down an otherwise fine capture run."""
        # A directory that doesn't exist -> open() raises OSError.
        ExperimentRunner._write_manifest(str(tmp_path / "nope" / "x.json"), {"a": 1})

    def test_run_completes_when_manifest_cannot_be_written(self, runner, monkeypatch):
        real_open = open

        def flaky_open(path, *a, **kw):
            if str(path).endswith(EXPERIMENT_MANIFEST_NAME):
                raise OSError("disk full")
            return real_open(path, *a, **kw)

        monkeypatch.setattr("builtins.open", flaky_open)
        exp_dir = _run(runner, "run1")

        assert not (exp_dir / EXPERIMENT_MANIFEST_NAME).exists()
        assert list(exp_dir.glob("*.png"))          # capture still happened


class TestNameSanitisation:
    @pytest.mark.parametrize("raw,expected_dir_suffix", [
        ("my experiment", "my_experiment"),
        ("a/b", "a_b"),
        ("plain", "plain"),
    ])
    def test_directory_name_is_safe(self, runner, raw, expected_dir_suffix):
        exp_dir = _run(runner, raw)
        assert exp_dir.name.endswith(f"_{expected_dir_suffix}")
        assert exp_dir.parent == Path(runner.out_dir)

    def test_slash_does_not_create_nested_directories(self, runner):
        exp_dir = _run(runner, "a/b")
        assert exp_dir.is_dir()
        assert not (Path(runner.out_dir) / "a").is_dir()

    def test_blank_name_falls_back(self, runner):
        exp_dir = _run(runner, "   ")
        assert exp_dir.name.endswith("_experiment")
        manifest = json.loads((exp_dir / EXPERIMENT_MANIFEST_NAME).read_text())
        assert manifest["experiment_name"] == "experiment"

    def test_raw_title_preserved_in_manifest(self, runner):
        exp_dir = _run(runner, "my experiment")
        manifest = json.loads((exp_dir / EXPERIMENT_MANIFEST_NAME).read_text())
        assert manifest["experiment_name"] == "my experiment"


class TestImageOutputNaming:
    def test_still_filename_carries_title_and_sub_label(self, runner):
        exp_dir = _run(runner, "stentor assay #4", labels=("A1-treated",))
        names = [p.name for p in exp_dir.glob("*.png")]
        assert len(names) == 1
        assert names[0].startswith("stentor_assay_4_A1-treated_")
        assert names[0].endswith(".png")

    def test_csv_records_the_composed_label(self, runner):
        exp_dir = _run(runner, "run1", labels=("A1-treated", "A2"))
        csv_path = next(exp_dir.glob("*_points.csv"))
        with open(csv_path, newline="") as f:
            rows = list(csv.DictReader(f))
        assert [r["Well"] for r in rows] == ["A1-treated", "A2"]
        assert rows[0]["Capture_File"].startswith("run1_A1-treated_")

    def test_csv_filename_uses_safe_name(self, runner):
        exp_dir = _run(runner, "my experiment")
        assert next(exp_dir.glob("*_points.csv")).name.endswith("_my_experiment_points.csv")
