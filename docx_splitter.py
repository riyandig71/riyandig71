#!/usr/bin/env python3
"""
DOCX Splitter — Split large DOCX files into parts of X pages each.

Usage:
    # Split single file into 300-page parts:
    python docx_splitter.py --input "CLOD 394.docx" --pages 300

    # Split all DOCX files in a folder:
    python docx_splitter.py --input "7 DMO/" --pages 300

    # Custom output folder:
    python docx_splitter.py --input "big_doc.docx" --pages 500 --output-dir splits/
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from copy import deepcopy

from docx import Document as DocxDocument
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from lxml import etree

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("docx_splitter")


# ---------------------------------------------------------------------------
# Page detection
# ---------------------------------------------------------------------------

def _iter_body_elements(doc: DocxDocument):
    """Yield (element, type) for each body child in document order."""
    for element in doc.element.body:
        tag = element.tag.split("}")[-1] if "}" in element.tag else element.tag
        yield element, tag


def _has_page_break(element) -> bool:
    """Check if an element contains a page break (explicit or rendered)."""
    for br in element.iter(qn("w:br")):
        if br.get(qn("w:type")) == "page":
            return True
    for lpb in element.iter(qn("w:lastRenderedPageBreak")):
        return True
    return False


def _count_page_breaks_in_element(element) -> int:
    """Count how many page breaks exist within an element."""
    count = 0
    for br in element.iter(qn("w:br")):
        if br.get(qn("w:type")) == "page":
            count += 1
    for lpb in element.iter(qn("w:lastRenderedPageBreak")):
        count += 1
    return count


def count_pages(docx_path: str | Path) -> int:
    """Count pages in a DOCX based on page break markers.

    This is best-effort — DOCX doesn't store page boundaries natively.
    Counts explicit page breaks + lastRenderedPageBreak hints + 1.
    """
    doc = DocxDocument(str(docx_path))
    page_count = 1
    for element, tag in _iter_body_elements(doc):
        page_count += _count_page_breaks_in_element(element)
    return page_count


# ---------------------------------------------------------------------------
# Splitter
# ---------------------------------------------------------------------------

def split_docx(
    docx_path: str | Path,
    pages_per_part: int,
    output_dir: str | Path | None = None,
) -> list[Path]:
    """Split a DOCX into multiple parts of *pages_per_part* pages each.

    Returns a list of output file paths.

    Strategy:
    - Iterate body elements, tracking page breaks.
    - Every *pages_per_part* pages, start a new output document.
    - Copy styles, headers, footers from the original.
    """
    source_path = Path(docx_path)
    if output_dir is None:
        out_dir = source_path.parent / f"{source_path.stem}_split"
    else:
        out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    source_doc = DocxDocument(str(source_path))
    total_pages = count_pages(source_path)
    stem = source_path.stem

    logger.info("Splitting: %s (%d pages, %d pages/part)",
                source_path.name, total_pages, pages_per_part)

    if total_pages <= pages_per_part:
        logger.info("Document has %d pages — no split needed.", total_pages)
        return [source_path]

    # Collect all body elements with their page positions
    elements: list[tuple] = []  # (element, tag, page_number)
    current_page = 1

    for element, tag in _iter_body_elements(source_doc):
        breaks = _count_page_breaks_in_element(element)
        if breaks > 0 and tag == "p":
            # Page break at start of this element — increment before assigning
            current_page += breaks
        elements.append((element, tag, current_page))
        if breaks > 0 and tag != "p":
            current_page += breaks

    # Group elements into parts
    parts: list[list[tuple]] = []
    current_part: list[tuple] = []
    part_start_page = 1

    for element, tag, page_num in elements:
        # Check if this element starts a new part
        if page_num > part_start_page + pages_per_part - 1 and current_part:
            parts.append(current_part)
            current_part = []
            part_start_page = page_num

        current_part.append((element, tag, page_num))

    if current_part:
        parts.append(current_part)

    logger.info("Split into %d parts", len(parts))

    # Generate output files
    output_paths: list[Path] = []

    for part_idx, part_elements in enumerate(parts):
        part_num = part_idx + 1
        first_page = part_elements[0][2]
        last_page = part_elements[-1][2]

        filename = f"{stem} (Part {part_num} _ {first_page}-{last_page}).docx"
        out_path = out_dir / filename

        # Create new document preserving styles
        new_doc = DocxDocument()

        # Copy styles from source
        _copy_styles(source_doc, new_doc)

        # Copy page setup from source
        _copy_page_setup(source_doc, new_doc)

        # Remove default empty paragraph
        if new_doc.paragraphs:
            p = new_doc.paragraphs[0]._element
            p.getparent().remove(p)

        # Add elements
        for element, tag, page_num in part_elements:
            new_doc.element.body.append(deepcopy(element))

        new_doc.save(str(out_path))
        logger.info("  Part %d: pages %d-%d → %s (%.1f KB)",
                     part_num, first_page, last_page,
                     out_path.name, out_path.stat().st_size / 1024)

        output_paths.append(out_path)

    return output_paths


def _copy_styles(source: DocxDocument, target: DocxDocument) -> None:
    """Copy style definitions from source to target document."""
    source_styles = source.element.find(qn("w:styles"))
    if source_styles is not None:
        target_styles = target.element.find(qn("w:styles"))
        if target_styles is not None:
            target_styles.getparent().remove(target_styles)
        target.element.insert(0, deepcopy(source_styles))


def _copy_page_setup(source: DocxDocument, target: DocxDocument) -> None:
    """Copy page size and margins from source to target."""
    try:
        src_section = source.sections[0]
        tgt_section = target.sections[0]
        tgt_section.page_width = src_section.page_width
        tgt_section.page_height = src_section.page_height
        tgt_section.top_margin = src_section.top_margin
        tgt_section.bottom_margin = src_section.bottom_margin
        tgt_section.left_margin = src_section.left_margin
        tgt_section.right_margin = src_section.right_margin
    except (IndexError, AttributeError):
        pass


# ---------------------------------------------------------------------------
# Folder processing
# ---------------------------------------------------------------------------

def split_folder(
    folder_path: str | Path,
    pages_per_part: int,
    output_dir: str | Path | None = None,
) -> dict[str, list[Path]]:
    """Split all DOCX files in a folder.

    Returns a dict of {original_filename: [output_paths]}.
    Skips files that are already small enough.
    """
    folder = Path(folder_path)
    docx_files = sorted(folder.glob("*.docx"))

    if not docx_files:
        logger.error("No .docx files found in %s", folder)
        return {}

    logger.info("Found %d DOCX files in %s", len(docx_files), folder)

    results: dict[str, list[Path]] = {}
    for docx_file in docx_files:
        logger.info("")
        try:
            out = output_dir or (folder / f"{docx_file.stem}_split")
            parts = split_docx(docx_file, pages_per_part, out)
            results[docx_file.name] = parts
        except Exception as exc:
            logger.error("Failed to split %s: %s", docx_file.name, exc)
            results[docx_file.name] = []

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Split large DOCX files into parts of X pages each",
    )
    parser.add_argument(
        "--input", required=True,
        help="Single .docx file OR folder containing .docx files",
    )
    parser.add_argument(
        "--pages", type=int, required=True,
        help="Number of pages per split part (e.g. 300, 500)",
    )
    parser.add_argument(
        "--output-dir", default=None,
        help="Output directory (default: {input}_split/)",
    )
    args = parser.parse_args()

    input_path = Path(args.input)

    if input_path.is_file() and input_path.suffix.lower() == ".docx":
        # Single file
        parts = split_docx(input_path, args.pages, args.output_dir)
        print(f"\nSplit complete: {len(parts)} part(s)")
        for p in parts:
            print(f"  {p}")

    elif input_path.is_dir():
        # Folder
        results = split_folder(input_path, args.pages, args.output_dir)
        print(f"\n{'='*60}")
        print(f"  SPLIT COMPLETE")
        print(f"{'='*60}")
        for name, parts in results.items():
            if len(parts) <= 1:
                print(f"  {name}: no split needed")
            else:
                print(f"  {name}: {len(parts)} parts")
                for p in parts:
                    print(f"    → {p.name}")

    else:
        print(f"ERROR: {input_path} is not a .docx file or folder")
        sys.exit(1)


if __name__ == "__main__":
    main()
