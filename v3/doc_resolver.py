"""
V3 Document Resolver
====================
Handles multi-part document inputs.

Real-world structure:
  Doc A can be:
    - A single .docx file
    - A folder containing Part 1, Part 2, ... Part N .docx files

  Doc B is always:
    - A single .docx file (benchmark)

This module resolves the input into a list of (part_label, docx_path) pairs,
so the orchestrator can process each part independently.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("v3.doc_resolver")


@dataclass
class DocPart:
    """One part of a (possibly multi-part) document."""
    label: str              # e.g. "Part 1 _ 1-614" or "CLOD 415_EN" (single file)
    path: Path              # full path to the .docx file
    part_number: int        # 1-based part index (1 for single files)
    page_range: str         # e.g. "1-614" or "all"


@dataclass
class ResolvedInput:
    """Resolved input for the review pipeline."""
    doc_a_parts: list[DocPart]
    doc_b_path: Path
    doc_a_is_multipart: bool
    doc_a_label: str        # e.g. "CLOD 394" or "CLOD 415_EN"


def _extract_part_number(filename: str) -> int:
    """Extract part number from filename like 'CLOD 394 (Part 2 _ 615-1228)_EN'.

    Tries multiple patterns:
      - 'Part 1', 'Part 2', 'part 3'
      - 'Part1', 'Part2'
      - Falls back to alphabetical order
    """
    m = re.search(r'[Pp]art\s*(\d+)', filename)
    if m:
        return int(m.group(1))
    return 0  # unknown — will be sorted alphabetically


def _extract_page_range(filename: str) -> str:
    """Extract page range from filename like 'Part 2 _ 615-1228'.

    Real patterns seen:
      - 'CLOD 394 (Part 1 _ 1-614)_EN'      → '1-614'
      - 'CLOD 412 Part 2 _ 302-602_EN'       → '302-602'
      - 'CLOD 412 Part 4 _ 904 1204_EN'      → '904-1204'
    """
    # First strip the "Part N" portion to avoid capturing part number
    stripped = re.sub(r'[Pp]art\s*\d+', '', filename)

    # Pattern: _ followed by start-end (with dash, space, or underscore separator)
    m = re.search(r'_\s*(\d+)\s*[-_ ]\s*(\d+)', stripped)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return "all"


def resolve_input(
    doc_a_path: str | Path,
    doc_b_path: str | Path,
) -> ResolvedInput:
    """Resolve Doc A (single file or folder) and Doc B into structured input.

    Rules:
    - If doc_a_path is a .docx file → single-part input
    - If doc_a_path is a folder → scan for .docx files, sort by part number
    - doc_b_path must be a .docx file
    """
    a_path = Path(doc_a_path)
    b_path = Path(doc_b_path)

    if not b_path.exists():
        raise FileNotFoundError(f"Document B not found: {b_path}")

    # --- Single file ---
    if a_path.is_file() and a_path.suffix.lower() == ".docx":
        label = a_path.stem
        part = DocPart(
            label=label,
            path=a_path,
            part_number=1,
            page_range="all",
        )
        logger.info("Doc A: single file — %s", a_path.name)
        return ResolvedInput(
            doc_a_parts=[part],
            doc_b_path=b_path,
            doc_a_is_multipart=False,
            doc_a_label=label,
        )

    # --- Folder with parts ---
    if a_path.is_dir():
        docx_files = sorted(a_path.glob("*.docx"))
        if not docx_files:
            raise FileNotFoundError(f"No .docx files found in folder: {a_path}")

        parts: list[DocPart] = []
        for f in docx_files:
            pnum = _extract_part_number(f.stem)
            prange = _extract_page_range(f.stem)
            parts.append(DocPart(
                label=f.stem,
                path=f,
                part_number=pnum if pnum > 0 else len(parts) + 1,
                page_range=prange,
            ))

        # Sort by part number
        parts.sort(key=lambda p: p.part_number)

        label = a_path.name  # folder name, e.g. "CLOD 394"
        logger.info("Doc A: folder with %d parts — %s", len(parts), label)
        for p in parts:
            logger.info("  Part %d: %s (pages %s, %s)",
                        p.part_number, p.path.name, p.page_range,
                        _human_size(p.path.stat().st_size))

        return ResolvedInput(
            doc_a_parts=parts,
            doc_b_path=b_path,
            doc_a_is_multipart=True,
            doc_a_label=label,
        )

    raise FileNotFoundError(
        f"Doc A must be a .docx file or a folder containing .docx parts: {a_path}"
    )


def _human_size(size_bytes: int) -> str:
    """Format bytes as human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.0f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def build_output_paths(
    resolved: ResolvedInput,
    output_dir: str | Path,
) -> list[tuple[DocPart, Path]]:
    """Build output file paths for each part.

    Single file:  output_dir/CLOD_415_EN_redline.docx
    Multi-part:   output_dir/CLOD_394_Part1_redline.docx
                  output_dir/CLOD_394_Part2_redline.docx
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    result: list[tuple[DocPart, Path]] = []
    for part in resolved.doc_a_parts:
        if resolved.doc_a_is_multipart:
            filename = f"{resolved.doc_a_label}_Part{part.part_number}_redline.docx"
        else:
            filename = f"{part.label}_redline.docx"
        # Clean filename
        filename = re.sub(r'[^\w\s\-.]', '_', filename)
        result.append((part, out / filename))

    return result
