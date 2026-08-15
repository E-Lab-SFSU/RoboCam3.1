"""
well_grid.py — shared well-plate grid widget used by both the Calibration
and Experiment panels.

Design goals
------------
* A single custom-painted QWidget owns the entire grid.  No individual
  QPushButton children are created, which eliminates the drag-detection
  problem that arises when Qt routes mouse events to the widget that
  received the initial press rather than to widgets the cursor moves over.
* No row/column axis header labels — every cell is already labelled
  (A1, B3, etc.) so the headers are redundant.
* Consistent visual style across both panels.
* Efficient: one paintEvent redraws all cells; no per-cell stylesheet
  recalculations.

Two modes
---------
WellGrid.Mode.NAVIGATE
    Used by the Calibration panel.  Clicking a cell emits ``well_clicked``
    with the (row, col) of the cell.  No selection state is maintained.

WellGrid.Mode.SELECT
    Used by the Experiment panel.  Cells have a selected/deselected state.
    Click-and-drag paints all cells the cursor passes over to the target
    state determined by the first cell touched in the stroke.
    Emits ``selection_changed`` whenever the selection changes.

Sub-labels
----------
In SELECT mode, right-clicking opens a context menu for attaching a free-text
sub-label ("treated", "ctrl", ...) to a well or to the whole current
selection.  The sub-label is drawn under the well id, tinted with a colour
derived from the label text so identical labels are visually grouped and a
mis-assignment is obvious at a glance.

Sub-labels are display + bookkeeping state only; the Experiment panel reads
them via ``get_sub_labels()`` and folds them into the well labels it hands
the runner, which is what puts them into capture filenames and metadata.
"""
from __future__ import annotations

from enum import Enum, auto
from typing import Optional

from PySide6.QtCore import Qt, Signal, QRect, QPoint, QSize
from PySide6.QtGui import (
    QPainter, QColor, QFont, QFontMetrics, QPen, QMouseEvent,
)
from PySide6.QtWidgets import QWidget, QSizePolicy, QMenu, QInputDialog

from robocam.naming import row_label, sanitize_sub_label


# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------

_COL_SEL        = QColor("#2a7ae2")   # selected well background
_COL_SEL_HOVER  = QColor("#1a5ab2")   # selected well hovered
_COL_DESEL      = QColor("#555555")   # deselected well background
_COL_DESEL_HOVER= QColor("#6a6a6a")   # deselected well hovered
_COL_NAV        = QColor("#3a3a3a")   # navigate-mode well background
_COL_NAV_HOVER  = QColor("#2a7ae2")   # navigate-mode well hovered
_COL_TEXT_SEL   = QColor("#ffffff")
_COL_TEXT_DESEL = QColor("#aaaaaa")
_COL_BORDER_SEL = QColor("#1a5ab2")
_COL_BORDER_DESEL = QColor("#333333")
_COL_BG         = QColor("#f0f0f0")   # widget background

# Accent colours for sub-labels.  Picked to stay legible against both the
# selected (blue) and deselected (grey) cell backgrounds, and to be
# distinguishable from each other — the point is to spot at a glance that
# one well in a "treated" block was accidentally left "ctrl".
_SUB_LABEL_COLORS = [
    QColor("#ffd166"),   # amber
    QColor("#8ce99a"),   # green
    QColor("#ffa8a8"),   # salmon
    QColor("#b197fc"),   # violet
    QColor("#66d9e8"),   # cyan
    QColor("#ffc9de"),   # pink
    QColor("#d8f5a2"),   # lime
    QColor("#ffb37a"),   # orange
]


def sub_label_color(label: str) -> QColor:
    """
    Stable colour for a sub-label.  Uses a hand-rolled hash rather than
    ``hash()`` because Python salts string hashing per process, which would
    make a well change colour between app launches.
    """
    acc = 0
    for ch in label:
        acc = (acc * 131 + ord(ch)) & 0xFFFFFFFF
    return _SUB_LABEL_COLORS[acc % len(_SUB_LABEL_COLORS)]


# ---------------------------------------------------------------------------
# WellGrid
# ---------------------------------------------------------------------------

class WellGrid(QWidget):
    """
    Custom-painted well-plate grid.

    Parameters
    ----------
    rows, cols : int
        Initial plate dimensions.
    mode : WellGrid.Mode
        NAVIGATE or SELECT (see module docstring).
    cell_w, cell_h : int
        Cell size in pixels.
    spacing : int
        Gap between cells in pixels.
    """

    class Mode(Enum):
        NAVIGATE = auto()
        SELECT   = auto()

    # Signals
    well_clicked       = Signal(int, int)   # (row, col) — NAVIGATE mode
    selection_changed  = Signal()           # SELECT mode
    sub_labels_changed = Signal()           # SELECT mode

    def __init__(
        self,
        rows: int = 8,
        cols: int = 12,
        mode: "WellGrid.Mode" = None,
        cell_w: int = 36,
        cell_h: int = 22,
        spacing: int = 2,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._mode    = mode if mode is not None else WellGrid.Mode.SELECT
        self._rows    = rows
        self._cols    = cols
        self._cell_w  = cell_w
        self._cell_h  = cell_h
        self._spacing = spacing

        # SELECT mode state
        self._selected: list[list[bool]] = [
            [True] * cols for _ in range(rows)
        ]
        # Sub-labels keyed by well id ("A1"), not by (row, col).  Well ids
        # survive a rebuild(), so a label set while the grid is still at its
        # construction-time size isn't lost when a larger calibration later
        # resizes the grid — which is exactly the order the Experiment panel
        # does it in (session restore runs before calibration sync).
        # Absent key == no label; empty strings are never stored.
        self._sub_labels: dict[str, str] = {}
        self._drag_target: Optional[bool] = None
        self._hover_cell: Optional[tuple[int, int]] = None

        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.setMinimumSize(self.sizeHint())

    # ------------------------------------------------------------------
    # Size
    # ------------------------------------------------------------------

    def sizeHint(self) -> QSize:
        w = self._cols * (self._cell_w + self._spacing) + self._spacing
        h = self._rows * (self._cell_h + self._spacing) + self._spacing
        return QSize(w, h)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def rebuild(self, rows: int, cols: int):
        """Resize the grid, preserving existing selection state where possible."""
        old = [row[:] for row in self._selected]
        self._rows = rows
        self._cols = cols
        self._selected = []
        for r in range(rows):
            row_data = []
            for c in range(cols):
                if r < len(old) and c < len(old[r]):
                    row_data.append(old[r][c])
                else:
                    row_data.append(True)
            self._selected.append(row_data)
        # Sub-labels are deliberately *not* pruned here — they're keyed by
        # well id, so a label on a well outside the new dimensions lies
        # dormant and comes back if the plate grows again.  Wells absent
        # from the active calibration are skipped at run time anyway.
        self._drag_target = None
        self._hover_cell = None
        self.setMinimumSize(self.sizeHint())
        self.updateGeometry()
        self.update()

    def check_all(self):
        for r in range(self._rows):
            for c in range(self._cols):
                self._selected[r][c] = True
        self.selection_changed.emit()
        self.update()

    def uncheck_all(self):
        for r in range(self._rows):
            for c in range(self._cols):
                self._selected[r][c] = False
        self.selection_changed.emit()
        self.update()

    def invert(self):
        for r in range(self._rows):
            for c in range(self._cols):
                self._selected[r][c] = not self._selected[r][c]
        self.selection_changed.emit()
        self.update()

    # NOTE: there is deliberately no get_selected_indices() returning flat
    # row-major indices.  It existed until 2026-08-14 and was a trap: a
    # calibration stores wells in *travel* order, and PATTERN_SNAKE reverses
    # odd rows, so row-major indices addressed the mirrored well on every
    # other row.  Match by well id via get_selected_labels() instead.

    def well_label(self, row: int, col: int) -> str:
        """Well id for a cell, e.g. ``(0, 0)`` → ``"A1"``."""
        return f"{row_label(row)}{col + 1}"

    def get_selected_labels(self) -> set[str]:
        """
        Well ids of every selected cell.

        Callers match these against the calibration's own ``labels`` list
        rather than indexing into it positionally, which keeps the mapping
        correct regardless of whether the plate was calibrated in raster or
        snake order.
        """
        return {
            self.well_label(r, c)
            for r in range(self._rows)
            for c in range(self._cols)
            if self._selected[r][c]
        }

    def get_sub_labels(self) -> dict[str, str]:
        """
        All sub-labels keyed by well id, e.g. ``{"A1": "treated"}``.

        Includes labels for wells outside the current grid dimensions so
        that saving and reloading a layout is lossless — see
        :meth:`rebuild`.
        """
        return dict(self._sub_labels)

    def set_sub_labels(self, mapping: dict) -> None:
        """
        Replace all sub-labels from a ``{well_id: label}`` mapping (the form
        stored in presets/session).  Values are sanitised on the way in, so
        a hand-edited preset can't smuggle an underscore into a well label.
        """
        cleaned: dict[str, str] = {}
        for well, raw in (mapping or {}).items():
            clean = sanitize_sub_label(str(raw))
            if clean:
                cleaned[str(well)] = clean
        self._sub_labels = cleaned
        self.sub_labels_changed.emit()
        self.update()

    def clear_sub_labels(self) -> None:
        if not self._sub_labels:
            return
        self._sub_labels = {}
        self.sub_labels_changed.emit()
        self.update()

    def sub_labelled_count(self) -> int:
        """Number of *visible* labelled wells (dormant ones aren't counted)."""
        return sum(
            1
            for r in range(self._rows)
            for c in range(self._cols)
            if self._sub_labels.get(self.well_label(r, c))
        )

    def selected_count(self) -> int:
        return sum(self._selected[r][c] for r in range(self._rows) for c in range(self._cols))

    def total_count(self) -> int:
        return self._rows * self._cols

    # ------------------------------------------------------------------
    # Cell geometry helpers
    # ------------------------------------------------------------------

    def _cell_rect(self, row: int, col: int) -> QRect:
        x = self._spacing + col * (self._cell_w + self._spacing)
        y = self._spacing + row * (self._cell_h + self._spacing)
        return QRect(x, y, self._cell_w, self._cell_h)

    def _cell_at(self, pos: QPoint) -> Optional[tuple[int, int]]:
        """Return (row, col) for a pixel position, or None if outside any cell."""
        for r in range(self._rows):
            for c in range(self._cols):
                if self._cell_rect(r, c).contains(pos):
                    return (r, c)
        return None

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Background
        painter.fillRect(self.rect(), _COL_BG)

        font = QFont()
        font.setPixelSize(9)
        sub_font = QFont()
        sub_font.setPixelSize(8)
        sub_font.setBold(True)
        sub_metrics = QFontMetrics(sub_font)
        painter.setFont(font)

        for r in range(self._rows):
            for c in range(self._cols):
                rect = self._cell_rect(r, c)
                is_hover = (self._hover_cell == (r, c))
                label = self.well_label(r, c)
                sub = self._sub_labels.get(label, "")

                if self._mode == WellGrid.Mode.SELECT:
                    sel = self._selected[r][c]
                    if sel:
                        bg = _COL_SEL_HOVER if is_hover else _COL_SEL
                        border = _COL_BORDER_SEL
                        fg = _COL_TEXT_SEL
                    else:
                        bg = _COL_DESEL_HOVER if is_hover else _COL_DESEL
                        border = _COL_BORDER_DESEL
                        fg = _COL_TEXT_DESEL
                else:  # NAVIGATE
                    bg = _COL_NAV_HOVER if is_hover else _COL_NAV
                    border = _COL_BORDER_DESEL
                    fg = _COL_TEXT_SEL

                # Cell background with rounded corners
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(bg)
                painter.drawRoundedRect(rect, 3, 3)

                # Border — a sub-labelled cell borrows its label's accent
                # colour so grouped wells read as a block.
                accent = sub_label_color(sub) if sub else None
                painter.setPen(QPen(accent if accent else border, 2 if accent else 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(rect, 3, 3)

                if sub:
                    # Two-line cell: well id on top, sub-label beneath.
                    # Split the box rather than overlaying so neither
                    # clips the other on a small cell.
                    top = QRect(rect.x(), rect.y(), rect.width(), rect.height() // 2)
                    bottom = QRect(rect.x(), rect.y() + rect.height() // 2,
                                   rect.width(), rect.height() - rect.height() // 2)

                    painter.setFont(font)
                    painter.setPen(fg)
                    painter.drawText(top, Qt.AlignmentFlag.AlignCenter, label)

                    painter.setFont(sub_font)
                    painter.setPen(accent)
                    shown = sub_metrics.elidedText(
                        sub, Qt.TextElideMode.ElideRight, bottom.width() - 2
                    )
                    painter.drawText(bottom, Qt.AlignmentFlag.AlignCenter, shown)
                    painter.setFont(font)
                else:
                    painter.setPen(fg)
                    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        painter.end()

    # ------------------------------------------------------------------
    # Mouse events
    # ------------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        cell = self._cell_at(event.position().toPoint())
        if cell is None:
            return
        r, c = cell
        if self._mode == WellGrid.Mode.NAVIGATE:
            self.well_clicked.emit(r, c)
        else:
            # Start drag: target state is the opposite of the clicked cell
            self._drag_target = not self._selected[r][c]
            self._selected[r][c] = self._drag_target
            self.selection_changed.emit()
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent):
        pos = event.position().toPoint()
        cell = self._cell_at(pos)

        # Update hover highlight
        if cell != self._hover_cell:
            self._hover_cell = cell
            self.update()

        # Drag painting (SELECT mode only)
        if (
            self._mode == WellGrid.Mode.SELECT
            and self._drag_target is not None
            and event.buttons() & Qt.MouseButton.LeftButton
            and cell is not None
        ):
            r, c = cell
            if self._selected[r][c] != self._drag_target:
                self._selected[r][c] = self._drag_target
                self.selection_changed.emit()
                self.update()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_target = None

    def leaveEvent(self, event):
        self._hover_cell = None
        self.update()

    # ------------------------------------------------------------------
    # Sub-label context menu
    # ------------------------------------------------------------------

    def contextMenuEvent(self, event):
        """
        Right-click menu for attaching sub-labels (SELECT mode only).

        Acts on the whole selection when the right-clicked well is part of
        it — labelling a block is the common case — and on the single well
        otherwise, so right-clicking outside the selection can't silently
        relabel wells the user wasn't pointing at.
        """
        if self._mode != WellGrid.Mode.SELECT:
            return

        cell = self._cell_at(event.pos())
        if cell is None:
            return
        r, c = cell
        clicked = self.well_label(r, c)

        selected = sorted(self.get_selected_labels())
        clicked_in_selection = clicked in selected
        targets = selected if clicked_in_selection else [clicked]
        scope = (
            f"{len(targets)} selected wells"
            if clicked_in_selection and len(targets) > 1
            else clicked
        )

        menu = QMenu(self)
        set_act = menu.addAction(f"Set label for {scope}…")
        clear_act = menu.addAction(f"Clear label on {scope}")
        clear_act.setEnabled(any(t in self._sub_labels for t in targets))
        menu.addSeparator()
        clear_all_act = menu.addAction("Clear all labels")
        clear_all_act.setEnabled(bool(self._sub_labels))

        chosen = menu.exec(event.globalPos())
        if chosen is None:
            return

        if chosen is clear_all_act:
            self.clear_sub_labels()
            return

        if chosen is clear_act:
            for t in targets:
                self._sub_labels.pop(t, None)
            self.sub_labels_changed.emit()
            self.update()
            return

        if chosen is set_act:
            current = self._sub_labels.get(clicked, "")
            text, ok = QInputDialog.getText(
                self, "Well Sub-Label",
                f"Sub-label for {scope}:\n"
                "(letters and digits; other characters become '-')",
                text=current,
            )
            if not ok:
                return
            clean = sanitize_sub_label(text)
            for t in targets:
                if clean:
                    self._sub_labels[t] = clean
                else:
                    self._sub_labels.pop(t, None)
            self.sub_labels_changed.emit()
            self.update()
