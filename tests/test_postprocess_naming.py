"""
Tests for experiment-title persistence through post-processing:
resolve_experiment_name()'s fallback chain, video filename prefixing,
container tagging, and the image-sequence sidecar.
"""
import json

import numpy as np
import pytest

from robocam.postprocess import (
    parse_meta_name,
    process_well,
    resolve_experiment_name,
)

av = pytest.importorskip("av", reason="PyAV needed to inspect container tags")

TS = "20260814_101500"
TITLE = "stentor assay #4"


def _make_experiment(tmp_path, wells=("A1",), *, manifest=True,
                     meta_name=True, cam_meta_name=True, dir_name=None):
    """Build a minimal but real raw-burst experiment folder on disk."""
    exp_dir = tmp_path / (dir_name or f"{TS}_stentor_assay")
    raw = exp_dir / "raw"
    raw.mkdir(parents=True)

    if manifest:
        (exp_dir / "experiment.json").write_text(json.dumps({
            "experiment_name": TITLE,
            "experiment_name_safe": "stentor_assay_4",
            "timestamp": TS,
        }))

    cam_meta = {"backend": "playerone", "bit_depth": 8, "bayer_pattern": "mono"}
    if cam_meta_name:
        cam_meta["experiment_name"] = TITLE
    (raw / "camera_meta.json").write_text(json.dumps(cam_meta))

    n, h, w = 4, 16, 20
    for well in wells:
        stack_name = f"{well}_{TS}_stack.npy"
        np.save(raw / stack_name, np.zeros((n, h, w), dtype=np.uint8))
        meta = {
            "frames_captured": n,
            "frames_file": stack_name,
            "duration_actual_s": 0.2,
            "fps_average": 20.0,
            "laser_events": [],
            "frames": [{"frame_index": i, "time_offset_s": i * 0.05} for i in range(n)],
            "well": well,
        }
        if meta_name:
            meta["experiment_name"] = TITLE
        (raw / f"{well}_{TS}_metadata.json").write_text(json.dumps(meta))

    return exp_dir


def _tags(path):
    """Container tags, upper-cased keys — Matroska normalises tag case."""
    with av.open(str(path)) as c:
        return {k.lower(): v for k, v in dict(c.metadata).items()}


class TestResolveExperimentName:
    def test_prefers_manifest(self, tmp_path):
        exp_dir = _make_experiment(tmp_path)
        assert resolve_experiment_name(exp_dir) == TITLE

    def test_falls_back_to_well_metadata(self, tmp_path):
        exp_dir = _make_experiment(tmp_path, manifest=False)
        meta = json.loads((exp_dir / "raw" / f"A1_{TS}_metadata.json").read_text())
        assert resolve_experiment_name(exp_dir, meta=meta) == TITLE

    def test_falls_back_to_camera_meta(self, tmp_path):
        exp_dir = _make_experiment(tmp_path, manifest=False)
        cam = json.loads((exp_dir / "raw" / "camera_meta.json").read_text())
        assert resolve_experiment_name(exp_dir, camera_meta=cam) == TITLE

    def test_falls_back_to_directory_name_for_legacy_data(self, tmp_path):
        """Pre-existing captures have no title recorded anywhere."""
        exp_dir = _make_experiment(tmp_path, manifest=False, meta_name=False,
                                   cam_meta_name=False)
        assert resolve_experiment_name(exp_dir) == "stentor_assay"

    def test_directory_without_timestamp_prefix_used_whole(self, tmp_path):
        exp_dir = _make_experiment(tmp_path, manifest=False, meta_name=False,
                                   cam_meta_name=False, dir_name="loose_folder")
        assert resolve_experiment_name(exp_dir) == "loose_folder"

    def test_corrupt_manifest_falls_through(self, tmp_path):
        exp_dir = _make_experiment(tmp_path, manifest=False, meta_name=False,
                                   cam_meta_name=False)
        (exp_dir / "experiment.json").write_text("{not json")
        assert resolve_experiment_name(exp_dir) == "stentor_assay"


class TestVideoNamingAndTags:
    def test_video_filenames_carry_experiment_name(self, tmp_path):
        exp_dir = _make_experiment(tmp_path)
        process_well(exp_dir / "raw" / f"A1_{TS}_metadata.json", exp_dir,
                     codec="libx264", do_png=False, do_mp4=True, do_vfr=True)

        assert (exp_dir / "videos_mp4" / f"stentor_assay_4_A1_{TS}.mp4").is_file()
        assert (exp_dir / "videos_vfr" / f"stentor_assay_4_A1_{TS}_vfr.mkv").is_file()

    def test_container_tags_written(self, tmp_path):
        exp_dir = _make_experiment(tmp_path)
        process_well(exp_dir / "raw" / f"A1_{TS}_metadata.json", exp_dir,
                     codec="libx264", do_png=False, do_mp4=True, do_vfr=True)

        for video in (exp_dir / "videos_mp4" / f"stentor_assay_4_A1_{TS}.mp4",
                      exp_dir / "videos_vfr" / f"stentor_assay_4_A1_{TS}_vfr.mkv"):
            tags = _tags(video)
            assert tags["title"] == TITLE
            assert TITLE in tags["comment"]
            assert "well A1" in tags["comment"]

    def test_sub_label_appears_in_name_and_tags(self, tmp_path):
        exp_dir = _make_experiment(tmp_path, wells=("A1-treated",))
        process_well(exp_dir / "raw" / f"A1-treated_{TS}_metadata.json", exp_dir,
                     codec="libx264", do_png=False, do_mp4=True, do_vfr=False)

        out = exp_dir / "videos_mp4" / f"stentor_assay_4_A1-treated_{TS}.mp4"
        assert out.is_file()
        assert "well A1 [treated]" in _tags(out)["comment"]

    def test_explicit_exp_name_overrides_resolution(self, tmp_path):
        exp_dir = _make_experiment(tmp_path)
        process_well(exp_dir / "raw" / f"A1_{TS}_metadata.json", exp_dir,
                     codec="libx264", do_png=False, do_mp4=True, do_vfr=False,
                     exp_name="override title")

        out = exp_dir / "videos_mp4" / f"override_title_A1_{TS}.mp4"
        assert out.is_file()
        assert _tags(out)["title"] == "override title"

    def test_no_title_means_no_prefix(self, tmp_path):
        """An unnameable folder must not produce a leading-underscore file."""
        exp_dir = _make_experiment(tmp_path)
        process_well(exp_dir / "raw" / f"A1_{TS}_metadata.json", exp_dir,
                     codec="libx264", do_png=False, do_mp4=True, do_vfr=False,
                     exp_name="")

        assert (exp_dir / "videos_mp4" / f"A1_{TS}.mp4").is_file()


class TestImageSidecar:
    def test_sidecar_written_next_to_pngs(self, tmp_path):
        exp_dir = _make_experiment(tmp_path, wells=("A1-treated",))
        process_well(exp_dir / "raw" / f"A1-treated_{TS}_metadata.json", exp_dir,
                     do_png=True, do_mp4=False, do_vfr=False)

        sidecar = exp_dir / "images_png" / "A1-treated" / "_source.json"
        payload = json.loads(sidecar.read_text())
        assert payload["experiment_name"] == TITLE
        assert payload["well"] == "A1-treated"
        assert payload["well_base"] == "A1"
        assert payload["sub_label"] == "treated"

    def test_sidecar_written_into_zip(self, tmp_path):
        import zipfile
        from robocam.postprocess import open_export_zip

        exp_dir = _make_experiment(tmp_path)
        zp = open_export_zip(exp_dir, "png")
        try:
            process_well(exp_dir / "raw" / f"A1_{TS}_metadata.json", exp_dir,
                         do_png=True, do_mp4=False, do_vfr=False, zip_png=zp)
        finally:
            zp.close()

        with zipfile.ZipFile(zp.filename) as z:
            payload = json.loads(z.read("A1/_source.json"))
        assert payload["experiment_name"] == TITLE


class TestParseMetaNameWithSubLabels:
    def test_sub_label_survives_underscore_split(self, tmp_path):
        from pathlib import Path
        well, ts = parse_meta_name(Path(f"A1-high-dose_{TS}_metadata.json"))
        assert well == "A1-high-dose"
        assert ts == TS
