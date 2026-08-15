"""
naming.py — shared rules for turning user-supplied text into file names.

Two separate concerns live here, with deliberately different strictness:

* ``sanitize_experiment_name`` — the experiment title.  It becomes a
  directory name (``<ts>_<name>/``) and a prefix on post-processed video
  files (``<name>_<well>_<ts>.mp4``), so it only has to be filesystem-safe.
  Underscores are fine.

* ``sanitize_sub_label`` — a per-well suffix such as ``treated`` or
  ``ctrl``.  This one is stricter: raw capture files are named
  ``<well>_<ts>_metadata.json`` and are parsed back apart on the underscore
  by ``postprocess.parse_meta_name()``, which takes ``parts[0]`` as the
  well.  A suffix containing ``_`` would silently truncate the well name
  there, so underscores (and anything else that isn't alphanumeric) are
  folded to ``-``.  That keeps ``A1-treated_20260814_101500_metadata.json``
  parsing to well ``A1-treated``.

The composed form is ``<well><SUB_LABEL_SEP><sub_label>`` and is what the
runner is handed as a well label — everything downstream (filenames, CSV,
metadata JSON, video tags) then carries it for free.
"""
from __future__ import annotations

from typing import Optional, Tuple

# Separator between a well id and its sub-label. Must not be "_" — see the
# module docstring.
SUB_LABEL_SEP = "-"

# Keeps a label readable in a file name and well under any path limit.
MAX_SUB_LABEL_LEN = 24
MAX_EXPERIMENT_NAME_LEN = 64

_FALLBACK_NAME = "experiment"


def sanitize_experiment_name(name: Optional[str], fallback: str = _FALLBACK_NAME) -> str:
    """
    Make an experiment title safe to embed in a directory or file name.

    Anything that isn't alphanumeric, ``-``, ``_`` or ``.`` becomes ``_``;
    runs of ``_`` collapse; leading/trailing separators and dots are
    stripped (a leading dot would hide the directory, a trailing one upsets
    Windows).  Returns ``fallback`` if nothing usable is left.
    """
    raw = (name or "").strip()
    out = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in raw)

    # Collapse runs of underscores introduced by the substitution above.
    while "__" in out:
        out = out.replace("__", "_")

    out = out.strip("_.-")[:MAX_EXPERIMENT_NAME_LEN].strip("_.-")
    return out or fallback


def sanitize_sub_label(label: Optional[str]) -> str:
    """
    Make a per-well sub-label safe to append to a well id.

    Stricter than :func:`sanitize_experiment_name`: only alphanumerics
    survive as-is, everything else (spaces, ``_``, ``/``, ...) folds to
    ``-``.  Returns ``""`` for anything that reduces to nothing, which
    callers treat as "no sub-label".
    """
    raw = (label or "").strip()
    out = "".join(c if c.isalnum() else SUB_LABEL_SEP for c in raw)

    dbl = SUB_LABEL_SEP * 2
    while dbl in out:
        out = out.replace(dbl, SUB_LABEL_SEP)

    return out.strip(SUB_LABEL_SEP)[:MAX_SUB_LABEL_LEN].strip(SUB_LABEL_SEP)


def compose_well_label(well: str, sub_label: Optional[str]) -> str:
    """
    Join a well id and an optional sub-label: ``("A1", "treated")`` →
    ``"A1-treated"``.  An empty/None sub-label returns the well unchanged.
    """
    clean = sanitize_sub_label(sub_label)
    return f"{well}{SUB_LABEL_SEP}{clean}" if clean else well


def split_well_label(label: str) -> Tuple[str, str]:
    """
    Inverse of :func:`compose_well_label` — ``"A1-treated"`` →
    ``("A1", "treated")``, ``"A1"`` → ``("A1", "")``.

    Splits on the *first* separator, so a sub-label may itself contain
    ``-`` (``"A1-day-2"`` → ``("A1", "day-2")``).
    """
    well, sep, sub = (label or "").partition(SUB_LABEL_SEP)
    return (well, sub) if sep else (label or "", "")


def row_label(index: int) -> str:
    """
    Spreadsheet-style row label: 0 → ``A``, 25 → ``Z``, 26 → ``AA``.

    Shared so the well-grid widget and the calibration path generator agree
    on well ids past row 26 — the grid used to do ``chr(ord('A') + r)``,
    which produces ``[`` for row 26 and would never match calibration.
    """
    label = ""
    i = index + 1
    while i > 0:
        i, rem = divmod(i - 1, 26)
        label = chr(ord("A") + rem) + label
    return label
