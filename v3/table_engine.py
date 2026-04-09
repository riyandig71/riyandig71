"""
V2 Table Engine
===============
Dedicated table comparison and reconciliation.

V1 had NO table handling.  V2 compares tables cell-by-cell and produces
structured diffs that the redline engine can apply safely.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .doc_reader import TableData, TableCell
from .review_engine import Action, ReviewDecision

logger = logging.getLogger("v2.table_engine")


@dataclass
class CellDiff:
    """One cell-level difference between Doc A and Doc B tables."""
    table_idx: int
    row: int
    col: int
    text_a: str
    text_b: str
    action: Action          # KEEP if same, REPLACE if different


def compare_tables(
    table_a: TableData,
    table_b: TableData,
) -> list[CellDiff]:
    """Compare two tables cell by cell.

    Returns a CellDiff for every cell that differs.
    Cells present in A but not in B (extra rows/cols) are flagged.
    """
    diffs: list[CellDiff] = []
    max_rows = max(table_a.rows, table_b.rows)
    max_cols = max(table_a.cols, table_b.cols)

    for r in range(max_rows):
        for c in range(max_cols):
            text_a = table_a.cell_text(r, c)
            text_b = table_b.cell_text(r, c)

            if text_a.strip() == text_b.strip():
                continue

            # Determine action
            if r >= table_a.rows or c >= table_a.cols:
                action = Action.INSERT
            elif r >= table_b.rows or c >= table_b.cols:
                action = Action.DELETE
            else:
                action = Action.REPLACE

            diffs.append(CellDiff(
                table_idx=table_a.idx,
                row=r,
                col=c,
                text_a=text_a,
                text_b=text_b,
                action=action,
            ))

    if diffs:
        logger.info("Table %d: %d cell differences found", table_a.idx, len(diffs))
    else:
        logger.info("Table %d: identical", table_a.idx)

    return diffs


def table_diffs_to_decisions(diffs: list[CellDiff]) -> list[ReviewDecision]:
    """Convert cell diffs into ReviewDecision objects for the agents."""
    decisions: list[ReviewDecision] = []
    for d in diffs:
        decisions.append(ReviewDecision(
            para_idx=d.table_idx,       # overloaded: table index
            sent_idx=None,
            element_type="table_cell",
            action=d.action,
            original=d.text_a,
            revised=d.text_b if d.action in (Action.REPLACE, Action.INSERT) else "",
            reason=f"Table cell [{d.row},{d.col}]: Doc A='{d.text_a}' differs from Doc B='{d.text_b}'",
        ))
    return decisions


def table_diffs_for_agent(diffs: list[CellDiff]) -> list[dict[str, Any]]:
    """Serialize cell diffs to JSON-friendly dicts for agent prompts."""
    return [
        {
            "table_idx": d.table_idx,
            "row": d.row,
            "col": d.col,
            "doc_a_value": d.text_a,
            "doc_b_value": d.text_b,
            "suggested_action": d.action.value,
        }
        for d in diffs
    ]
