"""
V2 Redline Engine
=================
Safe DOCX tracked-changes writer.

V1 problems fixed:
- V1 used _find_best_paragraph with word-overlap scoring → wrong paragraph.
- V1 used _diff_sentences index-alignment → mismatched sentences appended.
- V1 had global mutable state for revision IDs and comments.
- V2 targets exact paragraph by index, applies sentence-level REPLACE/DELETE
  via exact-match text search within runs, and never appends.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from docx import Document as DocxDocument
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from lxml import etree

from .review_engine import Action, ReviewDecision
from .doc_reader import DocumentContent, TableData

logger = logging.getLogger("v2.redline_engine")

AUTHOR_AGENT1 = "Agent-1-legal-reviewer"
AUTHOR_AGENT2 = "Agent-2-legal-reviewer"


# ---------------------------------------------------------------------------
# Revision ID generator — instance-level, not global
# ---------------------------------------------------------------------------

@dataclass
class RevisionState:
    """Encapsulates all mutable state for one redline operation."""
    counter: int = 100
    comments_root: etree._Element | None = None
    comments_part: Any = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    def next_id(self) -> str:
        self.counter += 1
        return str(self.counter)


# ---------------------------------------------------------------------------
# Low-level XML helpers
# ---------------------------------------------------------------------------

def _copy_run_props(run) -> OxmlElement | None:
    """Deep-copy run properties from an existing run, or None."""
    rpr = run._element.find(qn("w:rPr"))
    return copy.deepcopy(rpr) if rpr is not None else None


def _make_del_element(text: str, author: str, state: RevisionState, rpr_source=None) -> OxmlElement:
    """Create a <w:del> element with <w:delText>."""
    rid = state.next_id()
    del_elem = OxmlElement("w:del")
    del_elem.set(qn("w:id"), rid)
    del_elem.set(qn("w:author"), author)
    del_elem.set(qn("w:date"), state.timestamp)

    del_run = OxmlElement("w:r")
    if rpr_source is not None:
        del_run.append(copy.deepcopy(rpr_source))

    del_text = OxmlElement("w:delText")
    del_text.set(qn("xml:space"), "preserve")
    del_text.text = text
    del_run.append(del_text)
    del_elem.append(del_run)
    return del_elem


def _make_ins_element(text: str, author: str, state: RevisionState, rpr_source=None) -> OxmlElement:
    """Create a <w:ins> element with <w:t>."""
    rid = state.next_id()
    ins_elem = OxmlElement("w:ins")
    ins_elem.set(qn("w:id"), rid)
    ins_elem.set(qn("w:author"), author)
    ins_elem.set(qn("w:date"), state.timestamp)

    ins_run = OxmlElement("w:r")
    if rpr_source is not None:
        ins_run.append(copy.deepcopy(rpr_source))

    ins_text = OxmlElement("w:t")
    ins_text.set(qn("xml:space"), "preserve")
    ins_text.text = text
    ins_run.append(ins_text)
    ins_elem.append(ins_run)
    return ins_elem


# ---------------------------------------------------------------------------
# Enable tracked changes in document settings
# ---------------------------------------------------------------------------

def _enable_track_changes(doc: DocxDocument) -> None:
    """Configure document to open with tracked changes visible."""
    settings = doc.settings.element
    for tag in ("w:trackChanges", "w:revisionView"):
        for old in settings.findall(qn(tag)):
            settings.remove(old)

    settings.append(OxmlElement("w:trackChanges"))

    rv = OxmlElement("w:revisionView")
    rv.set(qn("w:markup"), "1")
    rv.set(qn("w:comments"), "1")
    rv.set(qn("w:insDel"), "1")
    rv.set(qn("w:formatting"), "1")
    settings.append(rv)


# ---------------------------------------------------------------------------
# Comments part management
# ---------------------------------------------------------------------------

def _init_comments(doc: DocxDocument, state: RevisionState) -> None:
    """Create the comments XML part if it doesn't exist."""
    from docx.opc.part import Part as OpcPart
    from docx.opc.packuri import PackURI

    COMMENTS_URI = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
    COMMENTS_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"

    for rel in doc.part.rels.values():
        if rel.reltype == COMMENTS_URI:
            state.comments_root = etree.fromstring(rel.target_part.blob)
            state.comments_part = rel.target_part
            return

    xml = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>'
    )
    state.comments_root = etree.fromstring(xml)
    state.comments_part = OpcPart(
        partname=PackURI("/word/comments.xml"),
        content_type=COMMENTS_CT,
        blob=xml,
        package=doc.part.package,
    )
    doc.part.relate_to(state.comments_part, COMMENTS_URI)


def _add_comment(paragraph, text: str, author: str, state: RevisionState) -> None:
    """Add a Word comment to a paragraph."""
    if state.comments_root is None:
        return

    cid = state.next_id()

    # Comment body in comments part
    comment_elem = OxmlElement("w:comment")
    comment_elem.set(qn("w:id"), cid)
    comment_elem.set(qn("w:author"), author)
    comment_elem.set(qn("w:date"), state.timestamp)
    cp = OxmlElement("w:p")
    cr = OxmlElement("w:r")
    ct = OxmlElement("w:t")
    ct.text = text
    cr.append(ct)
    cp.append(cr)
    comment_elem.append(cp)
    state.comments_root.append(comment_elem)

    # Range markers in paragraph
    rs = OxmlElement("w:commentRangeStart")
    rs.set(qn("w:id"), cid)
    paragraph._element.insert(0, rs)

    re_elem = OxmlElement("w:commentRangeEnd")
    re_elem.set(qn("w:id"), cid)
    paragraph._element.append(re_elem)

    # Reference run
    ref_run = OxmlElement("w:r")
    ref_rpr = OxmlElement("w:rPr")
    ref_style = OxmlElement("w:rStyle")
    ref_style.set(qn("w:val"), "CommentReference")
    ref_rpr.append(ref_style)
    ref_run.append(ref_rpr)
    cref = OxmlElement("w:commentReference")
    cref.set(qn("w:id"), cid)
    ref_run.append(cref)
    paragraph._element.append(ref_run)


def _flush_comments(state: RevisionState) -> None:
    """Serialize accumulated comments back into the Part blob."""
    if state.comments_part is not None and state.comments_root is not None:
        state.comments_part._blob = etree.tostring(
            state.comments_root, xml_declaration=True, encoding="UTF-8", standalone=True,
        )


# ---------------------------------------------------------------------------
# Paragraph-level DOCX manipulation
# ---------------------------------------------------------------------------

def _find_and_replace_in_runs(
    paragraph,
    old_text: str,
    new_text: str,
    author: str,
    state: RevisionState,
) -> bool:
    """Find old_text across the paragraph's runs and replace it with tracked change.

    This is the CORE V2 fix: instead of appending, we locate the exact text
    span, mark it as deleted (w:del), and insert the replacement (w:ins)
    in the same position.

    Returns True if the replacement was made, False if old_text wasn't found.
    """
    old_stripped = old_text.strip()
    if not old_stripped:
        return False

    # Strategy 1: text exists entirely within a single run
    for run in paragraph.runs:
        if old_stripped in run.text:
            rpr = _copy_run_props(run)
            p_elem = paragraph._element

            # Split run text: [before][match][after]
            idx = run.text.index(old_stripped)
            before = run.text[:idx]
            after = run.text[idx + len(old_stripped):]

            # Build replacement nodes
            nodes_to_insert = []

            if before:
                before_run = OxmlElement("w:r")
                if rpr is not None:
                    before_run.append(copy.deepcopy(rpr))
                bt = OxmlElement("w:t")
                bt.set(qn("xml:space"), "preserve")
                bt.text = before
                before_run.append(bt)
                nodes_to_insert.append(before_run)

            # Deletion of old text
            nodes_to_insert.append(_make_del_element(old_stripped, author, state, rpr))

            # Insertion of new text
            if new_text.strip():
                nodes_to_insert.append(_make_ins_element(new_text.strip(), author, state, rpr))

            if after:
                after_run = OxmlElement("w:r")
                if rpr is not None:
                    after_run.append(copy.deepcopy(rpr))
                at = OxmlElement("w:t")
                at.set(qn("xml:space"), "preserve")
                at.text = after
                after_run.append(at)
                nodes_to_insert.append(after_run)

            # Replace the original run with our new nodes
            run_elem = run._element
            parent = run_elem.getparent()
            for node in reversed(nodes_to_insert):
                run_elem.addnext(node)
            parent.remove(run_elem)

            return True

    # Strategy 2: text spans multiple runs — collect and replace
    # Concatenate all run text, find the span, then rebuild
    full_text = "".join(r.text for r in paragraph.runs if r.text)
    if old_stripped in full_text:
        # Fallback: mark the whole paragraph content as del + ins
        # This is safe because we preserve the original as deletion
        rpr = _copy_run_props(paragraph.runs[0]) if paragraph.runs else None
        p_elem = paragraph._element

        # Remove all existing runs
        for run in list(paragraph.runs):
            p_elem.remove(run._element)

        # Re-create with tracked change at the exact position
        idx = full_text.index(old_stripped)
        before = full_text[:idx]
        after = full_text[idx + len(old_stripped):]

        if before:
            br = OxmlElement("w:r")
            if rpr is not None:
                br.append(copy.deepcopy(rpr))
            bt = OxmlElement("w:t")
            bt.set(qn("xml:space"), "preserve")
            bt.text = before
            br.append(bt)
            p_elem.append(br)

        p_elem.append(_make_del_element(old_stripped, author, state, rpr))
        if new_text.strip():
            p_elem.append(_make_ins_element(new_text.strip(), author, state, rpr))

        if after:
            ar = OxmlElement("w:r")
            if rpr is not None:
                ar.append(copy.deepcopy(rpr))
            at = OxmlElement("w:t")
            at.set(qn("xml:space"), "preserve")
            at.text = after
            ar.append(at)
            p_elem.append(ar)

        return True

    return False


def _append_insertion(
    paragraph,
    new_text: str,
    author: str,
    state: RevisionState,
) -> None:
    """Append a tracked insertion at the end of a paragraph.

    Used ONLY for INSERT actions — genuinely new content.
    """
    rpr = _copy_run_props(paragraph.runs[0]) if paragraph.runs else None
    ins = _make_ins_element(new_text, author, state, rpr)
    paragraph._element.append(ins)


def _mark_paragraph_deleted(
    paragraph,
    author: str,
    state: RevisionState,
) -> None:
    """Mark all runs in a paragraph as tracked deletions."""
    for run in list(paragraph.runs):
        if not run.text or not run.text.strip():
            continue
        rpr = _copy_run_props(run)
        del_elem = _make_del_element(run.text, author, state, rpr)
        run._element.addnext(del_elem)
        run._element.getparent().remove(run._element)


# ---------------------------------------------------------------------------
# Table cell replacement
# ---------------------------------------------------------------------------

def _replace_table_cell(
    doc: DocxDocument,
    table_idx: int,
    row: int,
    col: int,
    new_text: str,
    author: str,
    state: RevisionState,
) -> bool:
    """Replace text in a specific table cell with tracked change."""
    if table_idx >= len(doc.tables):
        logger.warning("Table index %d out of range", table_idx)
        return False

    table = doc.tables[table_idx]
    if row >= len(table.rows):
        return False

    row_obj = table.rows[row]
    if col >= len(row_obj.cells):
        return False

    cell = row_obj.cells[col]
    for para in cell.paragraphs:
        if para.text.strip():
            return _find_and_replace_in_runs(para, para.text.strip(), new_text, author, state)
    return False


# ---------------------------------------------------------------------------
# Public API: apply decisions to DOCX
# ---------------------------------------------------------------------------

def apply_redline(
    source_docx_path: str | Path,
    doc_a_content: DocumentContent,
    decisions: list[ReviewDecision],
    output_path: str | Path,
    *,
    author: str = AUTHOR_AGENT2,
) -> Path:
    """Apply reviewed decisions to Document A, producing a DOCX redline.

    This is the main V2 output function.  It:
    1. Opens the original Document A DOCX.
    2. For each REPLACE decision, locates the exact text in the exact
       paragraph and applies w:del + w:ins — never appending.
    3. For each DELETE decision, marks the text as deleted.
    4. For each INSERT decision, adds tracked insertion.
    5. For KEEP, does nothing (preserves original).
    6. Adds comments for UNCERTAIN decisions and Agent 2 corrections.
    7. Enables tracked-changes view in document settings.
    """
    doc = DocxDocument(str(source_docx_path))
    state = RevisionState()

    _enable_track_changes(doc)
    _init_comments(doc, state)

    # Build a map from para_idx → python-docx paragraph object
    # We iterate body elements to match indices from doc_reader
    para_map: dict[int, Any] = {}
    para_counter = 0
    for element in doc.element.body:
        tag = element.tag.split("}")[-1] if "}" in element.tag else element.tag
        if tag == "p":
            # Find the matching python-docx Paragraph object
            for p in doc.paragraphs:
                if p._element is element:
                    para_map[para_counter] = p
                    break
            para_counter += 1

    applied = 0
    skipped = 0

    for d in decisions:
        if d.action == Action.KEEP:
            continue

        if d.element_type == "table_cell":
            # Table cell change
            if d.action == Action.REPLACE and d.revised:
                ok = _replace_table_cell(
                    doc, d.para_idx, 0, 0, d.revised, author, state,
                )
                if ok:
                    applied += 1
                else:
                    skipped += 1
            continue

        # Sentence-level change in a paragraph
        target_para = para_map.get(d.para_idx)
        if target_para is None:
            logger.warning("Para idx %d not found in DOCX — skipping decision", d.para_idx)
            skipped += 1
            continue

        if d.action == Action.REPLACE:
            if not d.revised:
                logger.warning("REPLACE with empty revised text for para %d — skipping", d.para_idx)
                skipped += 1
                continue
            ok = _find_and_replace_in_runs(target_para, d.original, d.revised, author, state)
            if ok:
                applied += 1
            else:
                # Could not find exact text — add as comment instead of corrupting
                logger.warning("Could not locate text in para %d for REPLACE — adding comment", d.para_idx)
                _add_comment(
                    target_para,
                    f"[{author}] Suggested change: {d.original!r} → {d.revised!r}. Reason: {d.reason}",
                    author,
                    state,
                )
                skipped += 1

        elif d.action == Action.DELETE:
            ok = _find_and_replace_in_runs(target_para, d.original, "", author, state)
            if ok:
                applied += 1
            else:
                skipped += 1

        elif d.action == Action.INSERT:
            if d.revised:
                _append_insertion(target_para, d.revised, author, state)
                applied += 1

        elif d.action == Action.UNCERTAIN:
            _add_comment(
                target_para,
                f"[{author}] UNCERTAIN: {d.reason}. Original: {d.original!r}",
                author,
                state,
            )

        # Add correction note as comment if Agent 2 overrode Agent 1
        if d.correction_note:
            _add_comment(
                target_para,
                f"[{author}] Correction: {d.correction_note}",
                author,
                state,
            )

    _flush_comments(state)

    out = Path(output_path)
    doc.save(str(out))
    logger.info("Redline saved: %s (%d applied, %d skipped)", out, applied, skipped)
    return out
