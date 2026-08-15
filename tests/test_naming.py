"""
Tests for robocam/naming.py — the shared filename rules.

The load-bearing property here is that a sub-label can never introduce an
underscore, because postprocess.parse_meta_name() splits well filenames on
"_" and takes parts[0] as the well.
"""
import pytest

from robocam.naming import (
    SUB_LABEL_SEP,
    compose_well_label,
    row_label,
    sanitize_experiment_name,
    sanitize_sub_label,
    split_well_label,
)


class TestSanitizeExperimentName:
    def test_plain_name_untouched(self):
        assert sanitize_experiment_name("stentor_assay") == "stentor_assay"

    @pytest.mark.parametrize("raw", [
        "my experiment",
        "my/experiment",
        "my:experiment",
        "my\\experiment",
    ])
    def test_path_separators_and_spaces_become_underscores(self, raw):
        out = sanitize_experiment_name(raw)
        assert out == "my_experiment"
        assert "/" not in out and "\\" not in out

    def test_collapses_underscore_runs(self):
        assert sanitize_experiment_name("a   //  b") == "a_b"

    def test_strips_leading_dot_so_dir_is_not_hidden(self):
        assert not sanitize_experiment_name(".hidden").startswith(".")

    @pytest.mark.parametrize("raw", ["", "   ", "///", "___", "..."])
    def test_empty_after_cleaning_falls_back(self, raw):
        assert sanitize_experiment_name(raw) == "experiment"

    def test_custom_fallback(self):
        assert sanitize_experiment_name("", fallback="") == ""

    def test_none_is_safe(self):
        assert sanitize_experiment_name(None) == "experiment"

    def test_length_capped(self):
        assert len(sanitize_experiment_name("x" * 500)) <= 64


class TestSanitizeSubLabel:
    def test_plain_label_untouched(self):
        assert sanitize_sub_label("treated") == "treated"

    @pytest.mark.parametrize("raw", [
        "high dose",
        "high_dose",
        "high/dose",
        "high.dose",
    ])
    def test_separators_fold_to_dash(self, raw):
        assert sanitize_sub_label(raw) == "high-dose"

    def test_never_emits_underscore(self):
        # The whole reason this function is stricter than the experiment-name
        # one — an underscore here would truncate the well in parse_meta_name.
        assert "_" not in sanitize_sub_label("a_b_c_d")

    def test_collapses_and_strips_dashes(self):
        assert sanitize_sub_label("--a---b--") == "a-b"

    @pytest.mark.parametrize("raw", ["", "   ", "___", "---", None])
    def test_empty_returns_empty(self, raw):
        assert sanitize_sub_label(raw) == ""

    def test_length_capped(self):
        assert len(sanitize_sub_label("y" * 100)) <= 24


class TestComposeSplit:
    def test_compose_with_label(self):
        assert compose_well_label("A1", "treated") == "A1-treated"

    @pytest.mark.parametrize("sub", ["", None, "   ", "___"])
    def test_compose_without_label_is_bare_well(self, sub):
        assert compose_well_label("A1", sub) == "A1"

    def test_compose_sanitizes(self):
        assert compose_well_label("A1", "high dose") == "A1-high-dose"

    def test_split_roundtrip(self):
        assert split_well_label(compose_well_label("B12", "ctrl")) == ("B12", "ctrl")

    def test_split_bare_well(self):
        assert split_well_label("B12") == ("B12", "")

    def test_split_keeps_dashes_inside_label(self):
        assert split_well_label("A1-day-2") == ("A1", "day-2")

    def test_composed_label_survives_underscore_split(self):
        """The property postprocess.parse_meta_name() depends on."""
        label = compose_well_label("A1", "high dose")
        filename = f"{label}_20260814_101500_metadata"
        assert filename.split("_")[0] == label


class TestRowLabel:
    @pytest.mark.parametrize("idx,expected", [
        (0, "A"), (1, "B"), (25, "Z"), (26, "AA"), (27, "AB"), (51, "AZ"), (52, "BA"),
    ])
    def test_spreadsheet_style(self, idx, expected):
        assert row_label(idx) == expected

    def test_past_z_is_not_punctuation(self):
        # The old grid used chr(ord('A') + r), which yields "[" at row 26 and
        # could never match a calibration label.
        assert row_label(26).isalpha()

    def test_matches_calibration_generator(self):
        from robocam.calibration import WellPlate
        corners = [(0, 0, 0), (0, 10, 0), (10, 0, 0), (10, 10, 0)]
        plate = WellPlate(2, 30, corners, WellPlate.PATTERN_RASTER)
        labels = [lab for lab, _ in plate.get_path_with_labels()]
        assert labels[0] == "A1"
        assert f"{row_label(26)}1" in labels
