"""
V2 Document Reader
==================
Extracts structured content from DOCX files: paragraphs, sentences, tables.
Provides paragraph-level mapping between Document A and Document B.

V1 problems fixed:
- V1 extracted raw page text and lost paragraph boundaries.
- V1 had no paragraph mapping → model mixed content across boundaries.
- V1 ignored tables entirely.
- V1 used fragile page-break detection that silently produced wrong splits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from docx import Document as DocxDocument
from docx.oxml.ns import qn
from docx.table import Table as DocxTable


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Sentence:
    """One sentence within a paragraph."""
    idx: int
    text: str


@dataclass
class Paragraph:
    """One paragraph from a DOCX, with sentences pre-segmented."""
    idx: int                          # 0-based position in document body
    text: str                         # full paragraph text
    sentences: list[Sentence] = field(default_factory=list)
    style_name: str = ""              # e.g. "Heading 1", "Normal"
    is_heading: bool = False


@dataclass
class TableCell:
    """One cell in a table."""
    row: int
    col: int
    text: str


@dataclass
class TableData:
    """Structured representation of a DOCX table."""
    idx: int                          # position among body elements
    rows: int = 0
    cols: int = 0
    cells: list[TableCell] = field(default_factory=list)

    def cell_text(self, row: int, col: int) -> str:
        for c in self.cells:
            if c.row == row and c.col == col:
                return c.text
        return ""


@dataclass
class DocumentContent:
    """All structured content extracted from a DOCX."""
    path: Path
    paragraphs: list[Paragraph] = field(default_factory=list)
    tables: list[TableData] = field(default_factory=list)
    # Maps body-element index → type ("para" or "table") for ordering
    body_order: list[tuple[str, int]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Sentence segmentation
# ---------------------------------------------------------------------------

# Abbreviations that should NOT trigger sentence splits
_ABBREVS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "no", "nos",
    "vol", "dept", "est", "approx", "inc", "ltd", "co", "corp",
    "vs", "etc", "al", "eg", "ie", "jan", "feb", "mar", "apr",
    "jun", "jul", "aug", "sep", "oct", "nov", "dec",
    "art", "sec", "cl", "pt", "para", "sch",
}


def segment_sentences(text: str) -> list[str]:
    """Split text into sentences, handling legal abbreviations."""
    if not text.strip():
        return []

    # Protect abbreviations: replace "Dr." → "Dr<DOT>"
    protected = text
    for abbr in _ABBREVS:
        pattern = re.compile(rf'\b({re.escape(abbr)})\.\s', re.IGNORECASE)
        protected = pattern.sub(r'\1<DOT> ', protected)

    # Protect numbered lists like "1." "2." "(a)."
    protected = re.sub(r'(\d+)\.\s', r'\1<DOT> ', protected)

    # Split on sentence-ending punctuation followed by space + capital or end
    parts = re.split(r'(?<=[.!?])\s+(?=[A-Z"\u201c(])', protected)

    # Restore dots
    sentences: list[str] = []
    for part in parts:
        restored = part.replace("<DOT>", ".").strip()
        if restored:
            sentences.append(restored)
    return sentences


# ---------------------------------------------------------------------------
# DOCX extraction
# ---------------------------------------------------------------------------

def _extract_table(table: DocxTable, idx: int) -> TableData:
    """Extract all cells from a python-docx Table object."""
    rows = len(table.rows)
    cols = len(table.columns) if table.rows else 0
    cells: list[TableCell] = []
    for r_idx, row in enumerate(table.rows):
        for c_idx, cell in enumerate(row.cells):
            cells.append(TableCell(row=r_idx, col=c_idx, text=cell.text.strip()))
    return TableData(idx=idx, rows=rows, cols=cols, cells=cells)


def read_document(docx_path: str | Path) -> DocumentContent:
    """Read a DOCX and extract paragraphs, tables, and body ordering.

    This iterates the document body elements in order, distinguishing
    paragraphs from tables, so we know the interleaved structure.
    """
    path = Path(docx_path)
    doc = DocxDocument(str(path))
    content = DocumentContent(path=path)

    para_counter = 0
    table_counter = 0

    # Iterate body children in document order
    for element in doc.element.body:
        tag = element.tag.split("}")[-1] if "}" in element.tag else element.tag

        if tag == "p":
            # It's a paragraph
            text = element.text or ""
            # Collect all run text
            full_text_parts: list[str] = []
            for r in element.findall(qn("w:r")):
                for t in r.findall(qn("w:t")):
                    if t.text:
                        full_text_parts.append(t.text)
            full_text = "".join(full_text_parts).strip()

            if not full_text:
                # Skip empty paragraphs but still count them for indexing
                para_counter += 1
                continue

            # Detect style
            style_name = ""
            is_heading = False
            ppr = element.find(qn("w:pPr"))
            if ppr is not None:
                pstyle = ppr.find(qn("w:pStyle"))
                if pstyle is not None:
                    style_name = pstyle.get(qn("w:val"), "")
                    is_heading = style_name.lower().startswith("heading")

            sents = segment_sentences(full_text)
            sent_objs = [Sentence(idx=i, text=s) for i, s in enumerate(sents)]

            para = Paragraph(
                idx=para_counter,
                text=full_text,
                sentences=sent_objs,
                style_name=style_name,
                is_heading=is_heading,
            )
            content.paragraphs.append(para)
            content.body_order.append(("para", para_counter))
            para_counter += 1

        elif tag == "tbl":
            # It's a table
            # Find the matching python-docx Table object
            if table_counter < len(doc.tables):
                tbl = doc.tables[table_counter]
                td = _extract_table(tbl, idx=table_counter)
                content.tables.append(td)
                content.body_order.append(("table", table_counter))
            table_counter += 1

    return content


# ---------------------------------------------------------------------------
# Paragraph mapping
# ---------------------------------------------------------------------------

def _normalize_for_matching(text: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation for fuzzy matching."""
    t = text.lower().strip()
    t = re.sub(r'[^\w\s]', '', t)
    t = re.sub(r'\s+', ' ', t)
    return t


def _similarity_score(a: str, b: str) -> float:
    """Jaccard similarity on word sets — fast and sufficient for paragraph mapping."""
    wa = set(_normalize_for_matching(a).split())
    wb = set(_normalize_for_matching(b).split())
    if not wa and not wb:
        return 1.0
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def map_paragraphs(
    doc_a: DocumentContent,
    doc_b: DocumentContent,
    *,
    threshold: float = 0.15,
) -> list[tuple[int, int | None]]:
    """Map each Document A paragraph to the best-matching Document B paragraph.

    Returns a list of (doc_a_para_idx, doc_b_para_idx_or_None).
    A None means no matching paragraph was found in Document B.

    Uses heading alignment first, then content similarity as fallback.
    """
    mapping: list[tuple[int, int | None]] = []
    used_b: set[int] = set()

    # Phase 1: match headings by text similarity
    a_headings = [(p.idx, p) for p in doc_a.paragraphs if p.is_heading]
    b_headings = [(p.idx, p) for p in doc_b.paragraphs if p.is_heading]

    heading_map: dict[int, int] = {}
    for a_idx, a_para in a_headings:
        best_score = 0.0
        best_b_idx: int | None = None
        for b_idx, b_para in b_headings:
            if b_idx in used_b:
                continue
            score = _similarity_score(a_para.text, b_para.text)
            if score > best_score:
                best_score = score
                best_b_idx = b_idx
        if best_b_idx is not None and best_score >= threshold:
            heading_map[a_idx] = best_b_idx
            used_b.add(best_b_idx)

    # Phase 2: for each A paragraph, find best B match
    #   - Within the section defined by the nearest heading alignment
    #   - Fall back to global search
    for a_para in doc_a.paragraphs:
        if a_para.idx in heading_map:
            mapping.append((a_para.idx, heading_map[a_para.idx]))
            continue

        best_score = 0.0
        best_b: int | None = None
        for b_para in doc_b.paragraphs:
            if b_para.idx in used_b:
                continue
            score = _similarity_score(a_para.text, b_para.text)
            if score > best_score:
                best_score = score
                best_b = b_para.idx

        if best_b is not None and best_score >= threshold:
            mapping.append((a_para.idx, best_b))
            used_b.add(best_b)
        else:
            mapping.append((a_para.idx, None))

    return mapping


def map_tables(
    doc_a: DocumentContent,
    doc_b: DocumentContent,
) -> list[tuple[int, int | None]]:
    """Map each Document A table to the best-matching Document B table.

    Uses position-based matching (table 0 → table 0, etc.) since tables
    in legal documents typically appear in corresponding order.
    """
    mapping: list[tuple[int, int | None]] = []
    for a_tbl in doc_a.tables:
        if a_tbl.idx < len(doc_b.tables):
            mapping.append((a_tbl.idx, a_tbl.idx))
        else:
            mapping.append((a_tbl.idx, None))
    return mapping


# ---------------------------------------------------------------------------
# Page-range utilities (kept from V1, cleaned up)
# ---------------------------------------------------------------------------

def detect_total_pages(docx_path: str | Path) -> int:
    """Best-effort page count from DOCX page-break markers.

    DOCX does not natively store page boundaries; this counts explicit
    page breaks + 1.  May undercount if the document relies on soft
    page breaks from the layout engine.
    """
    doc = DocxDocument(str(docx_path))
    page_count = 1
    for para in doc.paragraphs:
        for run in para.runs:
            for child in run._element:
                if child.tag == qn("w:br") and child.get(qn("w:type")) == "page":
                    page_count += 1
                elif child.tag == qn("w:lastRenderedPageBreak"):
                    page_count += 1
    return page_count


def parse_page_param(page_param: str, total_pages: int) -> list[int]:
    """Parse page parameter → sorted list of 1-based page numbers."""
    param = page_param.strip().lower()
    if param == "all":
        return list(range(1, total_pages + 1))

    result: set[int] = set()
    for segment in param.split(","):
        segment = segment.strip()
        if "-" in segment:
            parts = segment.split("-", 1)
            start = max(int(parts[0].strip()), 1)
            end = min(int(parts[1].strip()), total_pages)
            result.update(range(start, end + 1))
        else:
            pg = int(segment)
            if 1 <= pg <= total_pages:
                result.add(pg)
    return sorted(result)
