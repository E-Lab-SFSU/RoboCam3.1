"""
Tests for the well grid's sub-label state and, importantly, for the
selection → calibration mapping.

The mapping test is the reason this file exists: the grid's cells are laid
out row-major, but a calibration stores wells in travel order, and
PATTERN_SNAKE reverses odd rows. Matching by well id rather than by flat
index is what keeps a sub-label attached to the well the user actually
clicked.

Qt runs headless here via QT_QPA_PLATFORM=offscreen. Only WellGrid itself is
constructed — never ExperimentPanel, which would write the real
~/.local/share/RoboCam3/session.json.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from robocam.calibration import WellPlate  # noqa: E402
from robocam.naming import compose_well_label  # noqa: E402
from ui.well_grid import WellGrid, sub_label_color  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def grid(qapp):
    g = WellGrid(rows=8, cols=12, mode=WellGrid.Mode.SELECT)
    yield g
    g.deleteLater()


def _select_only(grid, wells):
    grid.uncheck_all()
    for r in range(grid._rows):
        for c in range(grid._cols):
            if grid.well_label(r, c) in wells:
                grid._selected[r][c] = True


class TestWellLabels:
    def test_labels_match_calibration_ordering_convention(self, grid):
        assert grid.well_label(0, 0) == "A1"
        assert grid.well_label(1, 2) == "B3"

    def test_past_row_z(self, qapp):
        g = WellGrid(rows=30, cols=2, mode=WellGrid.Mode.SELECT)
        assert g.well_label(26, 0) == "AA1"

    def test_selected_labels_default_to_everything(self, grid):
        assert len(grid.get_selected_labels()) == 96
        assert "H12" in grid.get_selected_labels()


class TestSubLabelState:
    def test_set_and_get(self, grid):
        grid.set_sub_labels({"A1": "treated", "A2": "treated"})
        assert grid.get_sub_labels() == {"A1": "treated", "A2": "treated"}

    def test_values_are_sanitised_on_the_way_in(self, grid):
        grid.set_sub_labels({"A1": "high dose", "A2": "a_b"})
        assert grid.get_sub_labels() == {"A1": "high-dose", "A2": "a-b"}

    def test_empty_values_dropped(self, grid):
        grid.set_sub_labels({"A1": "", "A2": "   ", "A3": "ok"})
        assert grid.get_sub_labels() == {"A3": "ok"}

    def test_clear(self, grid):
        grid.set_sub_labels({"A1": "treated"})
        grid.clear_sub_labels()
        assert grid.get_sub_labels() == {}

    def test_emits_signal(self, grid):
        seen = []
        grid.sub_labels_changed.connect(lambda: seen.append(1))
        grid.set_sub_labels({"A1": "treated"})
        assert seen

    def test_visible_count_ignores_dormant_labels(self, grid):
        grid.set_sub_labels({"A1": "treated", "Z9": "offgrid"})
        assert grid.sub_labelled_count() == 1

    def test_labels_survive_rebuild(self, grid):
        """Session restore runs before calibration resizes the grid."""
        grid.set_sub_labels({"A1": "treated", "P24": "edge"})
        grid.rebuild(16, 24)
        assert grid.get_sub_labels()["P24"] == "edge"
        assert grid.sub_labelled_count() == 2

    def test_shrink_then_grow_keeps_labels(self, grid):
        grid.set_sub_labels({"H12": "corner"})
        grid.rebuild(2, 2)
        assert grid.sub_labelled_count() == 0
        grid.rebuild(8, 12)
        assert grid.get_sub_labels()["H12"] == "corner"

    def test_color_is_stable_and_groups_by_label(self):
        assert sub_label_color("treated") == sub_label_color("treated")
        assert sub_label_color("treated") != sub_label_color("ctrl")


class TestSelectionToCalibrationMapping:
    """
    Mirrors what ExperimentPanel._start_experiment does, on both plate
    patterns.
    """

    @staticmethod
    def _resolve(grid, cal_labels, cal_positions):
        selected = grid.get_selected_labels()
        subs = grid.get_sub_labels()
        out = []
        for i, well in enumerate(cal_labels):
            if well not in selected or i >= len(cal_positions):
                continue
            out.append((compose_well_label(well, subs.get(well)), cal_positions[i]))
        return out

    @staticmethod
    def _calibration(pattern, rows=4, cols=4):
        corners = [(0, 0, 0), (0, 30, 0), (30, 0, 0), (30, 30, 0)]
        plate = WellPlate(cols, rows, corners, pattern)
        pairs = plate.get_path_with_labels()
        return [lab for lab, _ in pairs], [pos for _, pos in pairs]

    @pytest.mark.parametrize("pattern", [WellPlate.PATTERN_RASTER,
                                         WellPlate.PATTERN_SNAKE])
    def test_label_maps_to_its_own_position(self, qapp, pattern):
        cal_labels, cal_positions = self._calibration(pattern)
        truth = dict(zip(cal_labels, cal_positions))

        g = WellGrid(rows=4, cols=4, mode=WellGrid.Mode.SELECT)
        _select_only(g, {"B1", "B4"})
        g.set_sub_labels({"B1": "treated", "B4": "ctrl"})

        resolved = self._resolve(g, cal_labels, cal_positions)
        assert dict(resolved) == {
            "B1-treated": truth["B1"],
            "B4-ctrl": truth["B4"],
        }

    def test_snake_row_is_where_positional_indexing_went_wrong(self, qapp):
        """
        Regression guard: on a snake plate, row-major index 4 (grid cell B1)
        addresses the *last* well of row B, so the old positional lookup
        attached B1's label to B4's coordinates.
        """
        cal_labels, cal_positions = self._calibration(WellPlate.PATTERN_SNAKE)
        assert cal_labels[4] == "B4"          # travel order, not grid order

        g = WellGrid(rows=4, cols=4, mode=WellGrid.Mode.SELECT)
        _select_only(g, {"B1"})
        (label, pos), = self._resolve(g, cal_labels, cal_positions)

        assert label == "B1"
        assert pos == dict(zip(cal_labels, cal_positions))["B1"]
        assert pos != cal_positions[4]

    def test_capture_order_follows_calibration_travel_path(self, qapp):
        cal_labels, cal_positions = self._calibration(WellPlate.PATTERN_SNAKE)
        g = WellGrid(rows=4, cols=4, mode=WellGrid.Mode.SELECT)
        resolved = [lab for lab, _ in self._resolve(g, cal_labels, cal_positions)]
        assert resolved == cal_labels

    def test_wells_absent_from_calibration_are_skipped(self, qapp):
        cal_labels, cal_positions = self._calibration(WellPlate.PATTERN_RASTER)
        g = WellGrid(rows=8, cols=12, mode=WellGrid.Mode.SELECT)  # bigger than cal
        resolved = self._resolve(g, cal_labels, cal_positions)
        assert len(resolved) == len(cal_labels)

    def test_unlabelled_wells_keep_bare_ids(self, qapp):
        cal_labels, cal_positions = self._calibration(WellPlate.PATTERN_RASTER)
        g = WellGrid(rows=4, cols=4, mode=WellGrid.Mode.SELECT)
        g.set_sub_labels({"A1": "treated"})
        resolved = dict(self._resolve(g, cal_labels, cal_positions))
        assert "A1-treated" in resolved
        assert "A2" in resolved


class TestPainting:
    def test_paints_without_error_with_and_without_labels(self, grid):
        """Guards the two-line cell layout against an exception on render."""
        from PySide6.QtGui import QPixmap

        grid.set_sub_labels({"A1": "treated", "B2": "a-very-long-label-here"})
        grid.resize(grid.sizeHint())
        pixmap = QPixmap(grid.size())
        grid.render(pixmap)
        assert not pixmap.isNull()
