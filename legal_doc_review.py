#!/usr/bin/env python3
"""
Legal Document Review System — Two-Agent Architecture
=====================================================
Cost-efficient, page-based legal document review using two Claude agents
with orchestration, sentence ledger tracking, and DOCX tracked-changes output.
"""

from __future__ import annotations

import json
import re
import copy
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import anthropic
from docx import Document as DocxDocument
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from lxml import etree

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("legal_doc_review")

# ---------------------------------------------------------------------------
# Model configuration — cost-aware separation
# ---------------------------------------------------------------------------
MODEL_CHEAP: str = "claude-haiku-4-5-20251001"
MODEL_STRONG: str = "claude-sonnet-4-6"

# ---------------------------------------------------------------------------
# Prompt constants — stored verbatim
# ---------------------------------------------------------------------------

ORCHESTRATOR_PROMPT: str = r"""You are the orchestration layer for a two-pass legal document review system using two agents:
1. Agent-1-legal-reviewer
2. Agent-2-legal-reviewer

CORE OBJECTIVE:
Ensure complete sentence-by-sentence legal review of Document A against Document B with zero skipped sentences, while minimizing token usage and total cost.

PROCESS FLOW:

1. Split both documents into pages.
2. Detect total pages dynamically.
3. Select only the requested page(s).
4. For each selected page:
   a. Extract only the relevant page slice from Document A.
   b. Extract only the corresponding page slice from Document B when available.
   c. Segment all sentences in Document A for that page.
   d. Assign sentence IDs using format: {page}.{index}
   e. Create sentence ledger.

5. Send only the relevant page slice and sentence ledger to Agent-1-legal-reviewer.

6. Send only the following to Agent-2-legal-reviewer:
   - original page slice of Document A,
   - corresponding page slice of Document B,
   - Agent 1 revised output for that page,
   - sentence ledger for that page.

7. Validation:
   - every sentence ID must be accounted for,
   - if any sentence is missing, reprocess only that page,
   - do not reprocess the entire document.

8. Output:
   - final marked-up DOCX for the requested page(s) only.

HARD RULES:
- No skipped sentence.
- No full-document processing in one request.
- Must be page-based.
- Must preserve formatting.
- Must minimize token usage.
- Must preserve page boundaries.
- Must avoid unnecessary retries.
- Must not hallucinate missing content."""

AGENT1_PROMPT: str = r"""You are Agent-1-legal-reviewer.

TASK:
Compare Document A against Document B and revise Document A where necessary.

SCOPE:
- Work only on the assigned page(s).
- Review every sentence listed in the sentence ledger.
- Compare meaning, sentence context, and paragraph context.
- Do not perform literal word-by-word comparison only.

REVISION RULES:
- Preserve legal meaning unless benchmark B clearly requires correction.
- Preserve structure, numbering, capitalization patterns, cross-references, and page boundaries.
- Do not paraphrase only for style.
- Revise only if:
  1. grammar is incorrect,
  2. legal meaning is inaccurate,
  3. wording is materially inconsistent with benchmark B,
  4. ambiguity creates legal risk.
- If unsure, mark uncertain instead of inventing.
- No sentence may be skipped.

EDITING DISCIPLINE:
- Make the minimum necessary revision.
- Do not rewrite an entire paragraph if only one sentence needs correction.
- Do not make stylistic cleanup edits that are not legally necessary.
- If benchmark B does not justify a change, preserve Document A.

OUTPUT:
1. A marked-up DOCX revision of Document A for the assigned page(s) only.
   - Use markup / tracked revision style.
   - Reviewer label: Agent-1-legal-reviewer

2. A sentence ledger JSON containing every sentence ID:
[
  {
    "page": <page_number>,
    "sentence_id": "<page>.<index>",
    "status": "unchanged | revise | uncertain",
    "reason": "clear legal explanation",
    "proposed_revision": "only if revised"
  }
]

RULES FOR LEDGER:
- Every sentence ID must appear exactly once.
- No omissions.
- No merged sentence IDs.
- No skipped sentence."""

AGENT2_PROMPT: str = r"""You are Agent-2-legal-reviewer.

TASK:
Validate Agent 1 output and correct any remaining issue.

INPUT:
- Original page slice of Document A
- Corresponding page slice of Document B
- Agent 1 revised output for the same page
- Sentence ledger for the same page

VALIDATION DUTIES:
For every sentence ID:
1. Confirm the sentence was reviewed by Agent 1.
2. Confirm no sentence is missing.
3. Validate grammar.
4. Validate legal meaning.
5. Validate consistency with Document B when available.
6. Detect hallucination, over-editing, unsupported revisions, and missed revisions.

ACTION RULES:
- Keep valid edits.
- Correct invalid edits.
- Override Agent 1 if necessary.
- Fix any missing sentence handling.
- Do not rewrite unnecessarily.
- Preserve markup reviewability in DOCX.

REVISION RULES:
- Preserve legal meaning unless benchmark B clearly requires correction.
- Preserve structure, numbering, capitalization patterns, cross-references, and page boundaries.
- Do not paraphrase only for style.
- Revise only if:
  1. grammar is incorrect,
  2. legal meaning is inaccurate,
  3. wording is materially inconsistent with benchmark B,
  4. ambiguity creates legal risk.
- If unsure, mark uncertain instead of inventing.
- No sentence may be skipped.

OUTPUT:
1. Final marked-up DOCX for the assigned page(s) only.
   - Must remain human-reviewable.
   - Must preserve markup / tracked revision style.
   - Reviewer label: Agent-2-legal-reviewer

2. Validation log JSON:
[
  {
    "sentence_id": "<page>.<index>",
    "agent1_status": "unchanged | revise | uncertain",
    "validation": "approved | corrected | missing_fixed",
    "final_decision": "unchanged | revised | uncertain",
    "notes": "clear legal reasoning"
  }
]

FINAL VALIDATION RULES:
- Every sentence ID must be present.
- Zero missing sentence IDs.
- If any sentence is missing, reprocess only that page.
- Never approve output with skipped sentences."""

GLOBAL_REVISION_RULES: str = r"""Revision rules:
- Preserve legal meaning unless benchmark B clearly requires correction.
- Preserve structure, numbering, capitalization patterns, cross-references, and page boundaries.
- Do not paraphrase only for style.
- Revise only if:
  1. grammar is incorrect,
  2. legal meaning is inaccurate,
  3. wording is materially inconsistent with benchmark B,
  4. ambiguity creates legal risk.
- If unsure, mark uncertain instead of inventing.
- No sentence may be skipped."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PromptBundle:
    """Holds all system prompts used by the review pipeline."""
    orchestrator: str = ""
    agent1: str = ""
    agent2: str = ""
    global_revision_rules: str = ""


@dataclass
class SentenceEntry:
    """A single sentence in the ledger."""
    page: int
    sentence_id: str
    original_text: str
    status: str = "pending"
    reason: str = ""
    proposed_revision: str = ""


@dataclass
class ValidationEntry:
    """Agent-2 validation record for one sentence."""
    sentence_id: str
    agent1_status: str = ""
    validation: str = ""
    final_decision: str = ""
    notes: str = ""
    final_text: str = ""


@dataclass
class PageResult:
    """Collects all artefacts for one processed page."""
    page_number: int
    original_text: str = ""
    benchmark_text: str = ""
    agent1_revised_text: str = ""
    agent1_ledger: list[dict[str, Any]] = field(default_factory=list)
    agent2_final_text: str = ""
    agent2_validation_log: list[dict[str, Any]] = field(default_factory=list)
    success: bool = False


# ---------------------------------------------------------------------------
# Prompt helper functions
# ---------------------------------------------------------------------------

def load_prompts() -> PromptBundle:
    """Return a PromptBundle populated with the stored prompt constants."""
    return PromptBundle(
        orchestrator=ORCHESTRATOR_PROMPT,
        agent1=AGENT1_PROMPT,
        agent2=AGENT2_PROMPT,
        global_revision_rules=GLOBAL_REVISION_RULES,
    )


def get_orchestrator_prompt() -> str:
    """Return the orchestrator system prompt."""
    return ORCHESTRATOR_PROMPT


def get_agent1_prompt() -> str:
    """Return the Agent-1-legal-reviewer system prompt."""
    return AGENT1_PROMPT


def get_agent2_prompt() -> str:
    """Return the Agent-2-legal-reviewer system prompt."""
    return AGENT2_PROMPT


# ---------------------------------------------------------------------------
# DOCX page-detection utilities
# ---------------------------------------------------------------------------

_PAGE_BREAK_TAGS = {
    qn("w:br"),
    qn("w:lastRenderedPageBreak"),
}


def _is_page_break(element: etree._Element) -> bool:
    """Return True if *element* is an explicit or rendered page break."""
    tag = element.tag
    if tag == qn("w:br"):
        return element.get(qn("w:type")) == "page"
    if tag == qn("w:lastRenderedPageBreak"):
        return True
    return False


def _split_docx_into_pages(doc: DocxDocument) -> list[list[Any]]:
    """Split a python-docx Document into pages based on page-break markers.

    Returns a list of lists, where each inner list contains the paragraph
    objects that belong to that page.  Page detection uses both explicit
    ``w:br type="page"`` and ``w:lastRenderedPageBreak`` elements.

    This is a best-effort heuristic — DOCX does not natively store page
    boundaries, but explicit breaks and rendered-page-break hints are the
    most reliable indicators available without a full layout engine.
    """
    pages: list[list[Any]] = [[]]
    for para in doc.paragraphs:
        has_break_before = False
        for run in para.runs:
            for child in run._element:
                if _is_page_break(child):
                    has_break_before = True
                    break
            if has_break_before:
                break
        if has_break_before:
            pages.append([])
        pages[-1].append(para)
    # Remove empty trailing page if present
    if pages and not pages[-1]:
        pages.pop()
    return pages


def detect_total_pages(docx_path: str | Path) -> int:
    """Detect the total number of pages in a DOCX file dynamically."""
    doc = DocxDocument(str(docx_path))
    pages = _split_docx_into_pages(doc)
    return max(len(pages), 1)


def extract_page_text(docx_path: str | Path, page_number: int) -> str:
    """Extract text for a single 1-based page number.

    Returns an empty string if the page does not exist.
    """
    doc = DocxDocument(str(docx_path))
    pages = _split_docx_into_pages(doc)
    idx = page_number - 1
    if idx < 0 or idx >= len(pages):
        return ""
    return "\n".join(para.text for para in pages[idx])


def extract_page_paragraphs(docx_path: str | Path, page_number: int) -> list[str]:
    """Return a list of paragraph texts for the given 1-based page."""
    doc = DocxDocument(str(docx_path))
    pages = _split_docx_into_pages(doc)
    idx = page_number - 1
    if idx < 0 or idx >= len(pages):
        return []
    return [para.text for para in pages[idx]]


# ---------------------------------------------------------------------------
# Page parameter parsing
# ---------------------------------------------------------------------------

def parse_page_param(page_param: str, total_pages: int) -> list[int]:
    """Parse the page parameter and return a sorted list of 1-based page numbers.

    Supported formats:
        "1"       → [1]
        "1-3"     → [1, 2, 3]
        "2,5,7"   → [2, 5, 7]
        "all"     → [1 .. total_pages]

    Pages beyond *total_pages* are silently clamped.
    """
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


# ---------------------------------------------------------------------------
# Sentence segmentation and ledger
# ---------------------------------------------------------------------------

_SENTENCE_RE = re.compile(
    r'(?<=[.!?])\s+(?=[A-Z"\u201c(])'
    r'|(?<=[.!?])\s*$',
)


def segment_sentences(text: str) -> list[str]:
    """Split *text* into sentences using a lightweight regex heuristic.

    Handles common legal sentence boundaries while preserving numbering
    and cross-reference patterns.
    """
    if not text.strip():
        return []
    raw = re.split(r'(?<=[.!?])\s+', text)
    sentences: list[str] = []
    for chunk in raw:
        chunk = chunk.strip()
        if chunk:
            sentences.append(chunk)
    return sentences


def build_sentence_ledger(page_number: int, sentences: list[str]) -> list[SentenceEntry]:
    """Create a sentence ledger for a given page."""
    ledger: list[SentenceEntry] = []
    for idx, sent in enumerate(sentences, start=1):
        sid = f"{page_number}.{idx}"
        ledger.append(SentenceEntry(
            page=page_number,
            sentence_id=sid,
            original_text=sent,
        ))
    return ledger


def ledger_to_json(ledger: list[SentenceEntry]) -> list[dict[str, Any]]:
    """Convert a ledger list to a JSON-serialisable list of dicts."""
    return [
        {
            "page": e.page,
            "sentence_id": e.sentence_id,
            "original_text": e.original_text,
            "status": e.status,
            "reason": e.reason,
            "proposed_revision": e.proposed_revision,
        }
        for e in ledger
    ]


def validate_ledger_completeness(
    expected_ids: set[str],
    returned_ledger: list[dict[str, Any]],
) -> list[str]:
    """Return a list of sentence IDs that are missing from *returned_ledger*."""
    returned_ids = {entry.get("sentence_id", "") for entry in returned_ledger}
    return sorted(expected_ids - returned_ids)


# ---------------------------------------------------------------------------
# Claude API helper
# ---------------------------------------------------------------------------

def _call_claude(
    client: anthropic.Anthropic,
    *,
    system: str,
    user_message: str,
    model: str | None = None,
    max_tokens: int = 4096,
) -> str:
    """Send a single request to the Claude API and return the text response."""
    model = model or MODEL_STRONG
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return resp.content[0].text


def _extract_json_from_response(text: str) -> list[dict[str, Any]]:
    """Best-effort extraction of a JSON array from model output."""
    # Try to find a JSON array in the response
    match = re.search(r'\[.*\]', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    # Fallback: try the whole text
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []


def _extract_revised_text(text: str) -> str:
    """Extract the revised document text from a model response.

    Looks for text between common delimiters agents might use, or returns
    the full text with JSON portions stripped.
    """
    # Try to find text between markers
    for pattern in [
        r'(?s)---\s*REVISED TEXT\s*---\s*(.*?)\s*---\s*(?:END|LEDGER|SENTENCE)',
        r'(?s)## Revised Text\s*(.*?)\s*##',
        r'(?s)<revised>\s*(.*?)\s*</revised>',
        r'(?s)REVISED TEXT:\s*(.*?)\s*(?:SENTENCE LEDGER|VALIDATION LOG|\[)',
    ]:
        m = re.search(pattern, text)
        if m:
            return m.group(1).strip()
    # Fallback: strip JSON arrays and return rest
    cleaned = re.sub(r'\[.*?\]', '', text, flags=re.DOTALL).strip()
    if cleaned:
        return cleaned
    return text


# ---------------------------------------------------------------------------
# DOCX tracked-changes (markup) output layer
# ---------------------------------------------------------------------------

_AUTHOR_AGENT1 = "Agent-1-legal-reviewer"
_AUTHOR_AGENT2 = "Agent-2-legal-reviewer"
_REVISION_ID_COUNTER: int = 100


def _next_rev_id() -> int:
    global _REVISION_ID_COUNTER
    _REVISION_ID_COUNTER += 1
    return _REVISION_ID_COUNTER


def _make_rpr_copy(run) -> OxmlElement | None:
    """Copy run properties (rPr) from an existing run element, or None."""
    rpr = run._element.find(qn("w:rPr"))
    if rpr is not None:
        return copy.deepcopy(rpr)
    return None


def _add_tracked_deletion(paragraph, run_index: int, old_text: str, author: str) -> None:
    """Insert a tracked deletion <w:del> element into *paragraph* at *run_index*."""
    rev_id = str(_next_rev_id())
    del_elem = OxmlElement("w:del")
    del_elem.set(qn("w:id"), rev_id)
    del_elem.set(qn("w:author"), author)
    del_elem.set(qn("w:date"), "2026-04-09T00:00:00Z")

    del_run = OxmlElement("w:r")
    # Copy formatting from the original run if possible
    if run_index < len(paragraph.runs):
        rpr = _make_rpr_copy(paragraph.runs[run_index])
        if rpr is not None:
            del_run.append(rpr)

    del_text = OxmlElement("w:delText")
    del_text.set(qn("xml:space"), "preserve")
    del_text.text = old_text
    del_run.append(del_text)
    del_elem.append(del_run)

    # Insert before the run at run_index (or append)
    p_elem = paragraph._element
    runs = p_elem.findall(qn("w:r"))
    if run_index < len(runs):
        runs[run_index].addprevious(del_elem)
    else:
        p_elem.append(del_elem)


def _add_tracked_insertion(paragraph, run_index: int, new_text: str, author: str) -> None:
    """Insert a tracked insertion <w:ins> element into *paragraph* at *run_index*."""
    rev_id = str(_next_rev_id())
    ins_elem = OxmlElement("w:ins")
    ins_elem.set(qn("w:id"), rev_id)
    ins_elem.set(qn("w:author"), author)
    ins_elem.set(qn("w:date"), "2026-04-09T00:00:00Z")

    ins_run = OxmlElement("w:r")
    if run_index < len(paragraph.runs):
        rpr = _make_rpr_copy(paragraph.runs[run_index])
        if rpr is not None:
            ins_run.append(rpr)

    ins_text = OxmlElement("w:t")
    ins_text.set(qn("xml:space"), "preserve")
    ins_text.text = new_text
    ins_run.append(ins_text)
    ins_elem.append(ins_run)

    p_elem = paragraph._element
    runs = p_elem.findall(qn("w:r"))
    if run_index < len(runs):
        runs[run_index].addnext(ins_elem)
    else:
        p_elem.append(ins_elem)


def _ensure_comments_part(doc: DocxDocument) -> etree._Element:
    """Return the <w:comments> root element, creating the comments part if needed."""
    COMMENTS_URI = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
    for rel in doc.part.rels.values():
        if rel.reltype == COMMENTS_URI:
            return rel.target_part._element
    # Create a new comments part
    from docx.opc.part import Part
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    comments_xml = (
        '<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>'
    )
    comments_element = etree.fromstring(comments_xml.encode("utf-8"))
    comments_part = Part(
        partname="/word/comments.xml",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
        blob=etree.tostring(comments_element, xml_declaration=True, encoding="UTF-8"),
        package=doc.part.package,
    )
    comments_part._element = comments_element
    doc.part.relate_to(comments_part, COMMENTS_URI)
    return comments_element


def _add_comment_to_paragraph(
    paragraph,
    comment_text: str,
    author: str,
    doc: DocxDocument,
) -> None:
    """Add a Word comment annotation to the paragraph.

    Creates the full XML scaffolding: a <w:comment> in the comments part,
    plus commentRangeStart/End and commentReference in the paragraph, so the
    comment is visible in Word's review pane.
    """
    comments_root = _ensure_comments_part(doc)
    comment_id = str(_next_rev_id())

    # --- 1. Create <w:comment> in the comments part ---
    comment_elem = OxmlElement("w:comment")
    comment_elem.set(qn("w:id"), comment_id)
    comment_elem.set(qn("w:author"), author)
    comment_elem.set(qn("w:date"), "2026-04-09T00:00:00Z")
    comment_elem.set(qn("w:initials"), author[:3])
    # Comment body paragraph
    cp = OxmlElement("w:p")
    cr = OxmlElement("w:r")
    ct = OxmlElement("w:t")
    ct.text = comment_text
    cr.append(ct)
    cp.append(cr)
    comment_elem.append(cp)
    comments_root.append(comment_elem)

    # --- 2. Add comment range markers in the document paragraph ---
    range_start = OxmlElement("w:commentRangeStart")
    range_start.set(qn("w:id"), comment_id)
    paragraph._element.insert(0, range_start)

    range_end = OxmlElement("w:commentRangeEnd")
    range_end.set(qn("w:id"), comment_id)
    paragraph._element.append(range_end)

    # --- 3. Add comment reference run ---
    ref_run = OxmlElement("w:r")
    ref_rpr = OxmlElement("w:rPr")
    ref_style = OxmlElement("w:rStyle")
    ref_style.set(qn("w:val"), "CommentReference")
    ref_rpr.append(ref_style)
    ref_run.append(ref_rpr)
    comment_ref = OxmlElement("w:commentReference")
    comment_ref.set(qn("w:id"), comment_id)
    ref_run.append(comment_ref)
    paragraph._element.append(ref_run)


def _diff_sentences(original: str, revised: str) -> list[tuple[str, str, str]]:
    """Produce a simple diff between original and revised text.

    Returns a list of (type, old, new) tuples where type is one of:
    'equal', 'replace', 'insert', 'delete'.
    """
    orig_sents = segment_sentences(original) if original else []
    rev_sents = segment_sentences(revised) if revised else []

    diffs: list[tuple[str, str, str]] = []
    max_len = max(len(orig_sents), len(rev_sents))
    for i in range(max_len):
        o = orig_sents[i] if i < len(orig_sents) else ""
        r = rev_sents[i] if i < len(rev_sents) else ""
        if o == r:
            diffs.append(("equal", o, r))
        elif o and r:
            diffs.append(("replace", o, r))
        elif not o:
            diffs.append(("insert", "", r))
        else:
            diffs.append(("delete", o, ""))
    return diffs


# ---------------------------------------------------------------------------
# DOCX markup builder
# ---------------------------------------------------------------------------

def _enable_track_changes_view(doc: DocxDocument) -> None:
    """Set document settings so Word opens with tracked changes visible."""
    settings = doc.settings.element
    # Remove existing trackChanges if present
    for existing in settings.findall(qn("w:trackChanges")):
        settings.remove(existing)
    tc = OxmlElement("w:trackChanges")
    settings.append(tc)
    # Ensure revision view shows markup
    for existing in settings.findall(qn("w:revisionView")):
        settings.remove(existing)
    rv = OxmlElement("w:revisionView")
    rv.set(qn("w:markup"), "1")
    rv.set(qn("w:comments"), "1")
    rv.set(qn("w:insDel"), "1")
    rv.set(qn("w:formatting"), "1")
    settings.append(rv)


def _apply_diff_to_paragraph(paragraph, old_text: str, new_text: str, diff_type: str, author: str) -> None:
    """Apply a single diff operation to the appropriate paragraph."""
    if diff_type == "replace":
        # Try to find the old text in an existing run
        for run_idx, run in enumerate(paragraph.runs):
            if old_text.strip() and old_text.strip() in run.text:
                _add_tracked_deletion(paragraph, run_idx, old_text, author)
                _add_tracked_insertion(paragraph, run_idx, new_text, author)
                run.text = run.text.replace(old_text.strip(), "")
                return
        # Could not locate in runs — append tracked change at end
        _add_tracked_deletion(paragraph, len(paragraph.runs), old_text, author)
        _add_tracked_insertion(paragraph, len(paragraph.runs), new_text, author)
    elif diff_type == "insert":
        _add_tracked_insertion(paragraph, len(paragraph.runs), new_text, author)
    elif diff_type == "delete":
        for run_idx, run in enumerate(paragraph.runs):
            if old_text.strip() and old_text.strip() in run.text:
                _add_tracked_deletion(paragraph, run_idx, old_text, author)
                run.text = run.text.replace(old_text.strip(), "")
                return
        _add_tracked_deletion(paragraph, len(paragraph.runs), old_text, author)


def _find_best_paragraph(page_paras: list[Any], sentence_text: str) -> Any:
    """Find the paragraph that contains (or best matches) the given sentence text."""
    if not sentence_text.strip():
        return page_paras[0] if page_paras else None
    # Exact containment
    for para in page_paras:
        if sentence_text.strip() in para.text:
            return para
    # Partial match — pick paragraph with highest overlap
    best_para = page_paras[0]
    best_score = 0
    needle_words = set(sentence_text.lower().split())
    for para in page_paras:
        para_words = set(para.text.lower().split())
        overlap = len(needle_words & para_words)
        if overlap > best_score:
            best_score = overlap
            best_para = para
    return best_para


def build_tracked_changes_docx(
    source_docx_path: str | Path,
    page_results: list[PageResult],
    output_path: str | Path,
) -> Path:
    """Create a DOCX with native tracked revisions applied to Document A.

    For each page in *page_results* that has revisions, this function:
    1. Locates the original paragraphs on that page.
    2. Diffs the original text against the final revised text.
    3. Inserts ``<w:del>`` / ``<w:ins>`` XML elements so the file opens
       with visible tracked changes in Microsoft Word.
    4. Adds ``<w:comment>`` elements for Agent-2 corrections.
    5. Enables track-changes view in document settings.

    Pages not included in *page_results* are left untouched.
    """
    doc = DocxDocument(str(source_docx_path))
    pages = _split_docx_into_pages(doc)

    # Enable tracked changes view so Word opens in review mode
    _enable_track_changes_view(doc)

    result_by_page: dict[int, PageResult] = {
        pr.page_number: pr for pr in page_results if pr.success
    }

    for page_num, pr in result_by_page.items():
        page_idx = page_num - 1
        if page_idx < 0 or page_idx >= len(pages):
            continue

        page_paras = pages[page_idx]
        if not page_paras:
            continue

        original_text = "\n".join(p.text for p in page_paras)
        final_text = pr.agent2_final_text or pr.agent1_revised_text

        if not final_text or final_text.strip() == original_text.strip():
            continue

        diffs = _diff_sentences(original_text, final_text)

        # Determine the author label based on which agent made changes
        author = _AUTHOR_AGENT2 if pr.agent2_final_text else _AUTHOR_AGENT1

        # Apply diffs — route each change to the paragraph that contains it
        for diff_type, old, new in diffs:
            if diff_type == "equal":
                continue
            # Find the best matching paragraph for this sentence
            search_text = old if old else new
            target_para = _find_best_paragraph(page_paras, search_text)
            _apply_diff_to_paragraph(target_para, old, new, diff_type, author)

        # Add comments from validation log where Agent 2 corrected Agent 1
        for v_entry in pr.agent2_validation_log:
            if v_entry.get("validation") in ("corrected", "missing_fixed"):
                notes = v_entry.get("notes", "")
                if notes:
                    # Route comment to the paragraph containing the sentence
                    sid = v_entry.get("sentence_id", "")
                    # Find sentence original text from Agent 1 ledger
                    sent_text = ""
                    for le in pr.agent1_ledger:
                        if le.get("sentence_id") == sid:
                            sent_text = le.get("original_text", "")
                            break
                    comment_para = _find_best_paragraph(page_paras, sent_text) if sent_text else page_paras[0]
                    _add_comment_to_paragraph(comment_para, notes, _AUTHOR_AGENT2, doc)

    out = Path(output_path)
    doc.save(str(out))
    logger.info("Tracked-changes DOCX saved to %s", out)
    return out


def build_fallback_markup_docx(
    source_docx_path: str | Path,
    page_results: list[PageResult],
    output_path: str | Path,
) -> Path:
    """Fallback: produce a DOCX with color-coded markup if native tracked
    changes are not supported or fail.

    Deletions are shown as red strikethrough; insertions as blue underline.
    This can be swapped out for *build_tracked_changes_docx* if needed.
    """
    from docx.shared import RGBColor, Pt

    doc = DocxDocument(str(source_docx_path))
    pages = _split_docx_into_pages(doc)
    result_by_page = {pr.page_number: pr for pr in page_results if pr.success}

    for page_num, pr in result_by_page.items():
        page_idx = page_num - 1
        if page_idx < 0 or page_idx >= len(pages):
            continue

        page_paras = pages[page_idx]
        original_text = "\n".join(p.text for p in page_paras)
        final_text = pr.agent2_final_text or pr.agent1_revised_text
        if not final_text or final_text.strip() == original_text.strip():
            continue

        author = _AUTHOR_AGENT2 if pr.agent2_final_text else _AUTHOR_AGENT1
        diffs = _diff_sentences(original_text, final_text)

        if not page_paras:
            continue

        # Append a summary paragraph after the last page paragraph
        last_para = page_paras[-1]
        parent = last_para._element.getparent()
        insert_after = last_para._element

        header_p = OxmlElement("w:p")
        header_r = OxmlElement("w:r")
        header_rpr = OxmlElement("w:rPr")
        bold = OxmlElement("w:b")
        header_rpr.append(bold)
        header_r.append(header_rpr)
        header_t = OxmlElement("w:t")
        header_t.text = f"[{author} — Markup for Page {page_num}]"
        header_r.append(header_t)
        header_p.append(header_r)
        insert_after.addnext(header_p)
        insert_after = header_p

        for diff_type, old, new in diffs:
            if diff_type == "equal":
                continue

            markup_p = OxmlElement("w:p")

            if diff_type in ("replace", "delete"):
                # Strikethrough in red
                del_r = OxmlElement("w:r")
                del_rpr = OxmlElement("w:rPr")
                strike = OxmlElement("w:strike")
                del_rpr.append(strike)
                color = OxmlElement("w:color")
                color.set(qn("w:val"), "FF0000")
                del_rpr.append(color)
                del_r.append(del_rpr)
                del_t = OxmlElement("w:t")
                del_t.set(qn("xml:space"), "preserve")
                del_t.text = old
                del_r.append(del_t)
                markup_p.append(del_r)

            if diff_type in ("replace", "insert"):
                # Underline in blue
                ins_r = OxmlElement("w:r")
                ins_rpr = OxmlElement("w:rPr")
                uline = OxmlElement("w:u")
                uline.set(qn("w:val"), "single")
                ins_rpr.append(uline)
                ins_color = OxmlElement("w:color")
                ins_color.set(qn("w:val"), "0000FF")
                ins_rpr.append(ins_color)
                ins_r.append(ins_rpr)
                ins_t = OxmlElement("w:t")
                ins_t.set(qn("xml:space"), "preserve")
                ins_t.text = " " + new
                ins_r.append(ins_t)
                markup_p.append(ins_r)

            insert_after.addnext(markup_p)
            insert_after = markup_p

    out = Path(output_path)
    doc.save(str(out))
    logger.info("Fallback markup DOCX saved to %s", out)
    return out


# ---------------------------------------------------------------------------
# Agent execution — Agent 1
# ---------------------------------------------------------------------------

def _build_agent1_user_message(
    page_num: int,
    doc_a_text: str,
    doc_b_text: str,
    ledger_json: list[dict[str, Any]],
) -> str:
    """Compose the user-turn message for Agent 1."""
    benchmark_section = (
        f"--- Document B (benchmark) page {page_num} ---\n{doc_b_text}"
        if doc_b_text
        else f"--- Document B (benchmark) page {page_num} ---\n[Benchmark unavailable for this page]"
    )
    return (
        f"Page: {page_num}\n\n"
        f"--- Document A page {page_num} ---\n{doc_a_text}\n\n"
        f"{benchmark_section}\n\n"
        f"--- Sentence Ledger ---\n{json.dumps(ledger_json, indent=2)}\n\n"
        "Please review every sentence and provide:\n"
        "1. Your revised text (clearly delimited).\n"
        "2. The sentence ledger JSON with status for every sentence ID.\n"
        "Delimit revised text with --- REVISED TEXT --- and --- END REVISED TEXT ---\n"
        "Delimit the ledger JSON with --- SENTENCE LEDGER --- and --- END SENTENCE LEDGER ---"
    )


def run_agent1(
    client: anthropic.Anthropic,
    page_num: int,
    doc_a_text: str,
    doc_b_text: str,
    ledger: list[SentenceEntry],
    model: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Run Agent-1-legal-reviewer on a single page.

    Returns (revised_text, ledger_entries).
    """
    model = model or MODEL_STRONG
    ledger_json = ledger_to_json(ledger)
    user_msg = _build_agent1_user_message(page_num, doc_a_text, doc_b_text, ledger_json)

    logger.info("Agent 1 processing page %d (model=%s)", page_num, model)
    raw = _call_claude(
        client,
        system=AGENT1_PROMPT,
        user_message=user_msg,
        model=model,
        max_tokens=4096,
    )

    revised_text = _extract_revised_text(raw)
    returned_ledger = _extract_json_from_response(raw)
    return revised_text, returned_ledger


# ---------------------------------------------------------------------------
# Agent execution — Agent 2
# ---------------------------------------------------------------------------

def _build_agent2_user_message(
    page_num: int,
    doc_a_text: str,
    doc_b_text: str,
    agent1_revised_text: str,
    agent1_ledger: list[dict[str, Any]],
) -> str:
    """Compose the user-turn message for Agent 2."""
    benchmark_section = (
        f"--- Document B (benchmark) page {page_num} ---\n{doc_b_text}"
        if doc_b_text
        else f"--- Document B (benchmark) page {page_num} ---\n[Benchmark unavailable for this page]"
    )
    return (
        f"Page: {page_num}\n\n"
        f"--- Original Document A page {page_num} ---\n{doc_a_text}\n\n"
        f"{benchmark_section}\n\n"
        f"--- Agent 1 Revised Text ---\n{agent1_revised_text}\n\n"
        f"--- Agent 1 Sentence Ledger ---\n{json.dumps(agent1_ledger, indent=2)}\n\n"
        "Please validate every sentence and provide:\n"
        "1. Your final revised text (clearly delimited).\n"
        "2. The validation log JSON with an entry for every sentence ID.\n"
        "Delimit revised text with --- REVISED TEXT --- and --- END REVISED TEXT ---\n"
        "Delimit the validation log JSON with --- VALIDATION LOG --- and --- END VALIDATION LOG ---"
    )


def run_agent2(
    client: anthropic.Anthropic,
    page_num: int,
    doc_a_text: str,
    doc_b_text: str,
    agent1_revised_text: str,
    agent1_ledger: list[dict[str, Any]],
    model: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Run Agent-2-legal-reviewer on a single page.

    Returns (final_revised_text, validation_log).
    """
    model = model or MODEL_STRONG
    user_msg = _build_agent2_user_message(
        page_num, doc_a_text, doc_b_text, agent1_revised_text, agent1_ledger,
    )

    logger.info("Agent 2 processing page %d (model=%s)", page_num, model)
    raw = _call_claude(
        client,
        system=AGENT2_PROMPT,
        user_message=user_msg,
        model=model,
        max_tokens=4096,
    )

    final_text = _extract_revised_text(raw)
    validation_log = _extract_json_from_response(raw)
    return final_text, validation_log


# ---------------------------------------------------------------------------
# Validation & retry
# ---------------------------------------------------------------------------

def validate_page_result(
    page_num: int,
    expected_ids: set[str],
    agent2_log: list[dict[str, Any]],
) -> list[str]:
    """Check that Agent 2's validation log covers every expected sentence ID.

    Uses the cheap model for a lightweight completeness check.
    Returns a list of missing sentence IDs (empty means pass).
    """
    returned_ids = {e.get("sentence_id", "") for e in agent2_log}
    missing = sorted(expected_ids - returned_ids)
    if missing:
        logger.warning(
            "Page %d validation failed — missing IDs: %s", page_num, missing,
        )
    else:
        logger.info("Page %d validation passed — all %d sentences covered.",
                     page_num, len(expected_ids))
    return missing


def retry_page(
    client: anthropic.Anthropic,
    page_num: int,
    doc_a_text: str,
    doc_b_text: str,
    ledger: list[SentenceEntry],
    *,
    max_retries: int = 2,
    agent1_model: str | None = None,
    agent2_model: str | None = None,
) -> PageResult:
    """Process a single page through Agent 1 → Agent 2 with retry on failure.

    Only retries the *exact page* that failed validation.
    """
    expected_ids = {e.sentence_id for e in ledger}

    for attempt in range(1, max_retries + 1):
        logger.info("Page %d — attempt %d/%d", page_num, attempt, max_retries)

        # Agent 1
        a1_text, a1_ledger = run_agent1(
            client, page_num, doc_a_text, doc_b_text, ledger,
            model=agent1_model,
        )

        # Quick ledger completeness check (cheap model could do this,
        # but it's fast enough locally)
        a1_missing = validate_ledger_completeness(expected_ids, a1_ledger)
        if a1_missing:
            logger.warning(
                "Agent 1 missed sentences %s on page %d, retrying...",
                a1_missing, page_num,
            )
            continue

        # Agent 2
        a2_text, a2_log = run_agent2(
            client, page_num, doc_a_text, doc_b_text, a1_text, a1_ledger,
            model=agent2_model,
        )

        # Validate Agent 2 output
        missing = validate_page_result(page_num, expected_ids, a2_log)
        if not missing:
            return PageResult(
                page_number=page_num,
                original_text=doc_a_text,
                benchmark_text=doc_b_text,
                agent1_revised_text=a1_text,
                agent1_ledger=a1_ledger,
                agent2_final_text=a2_text,
                agent2_validation_log=a2_log,
                success=True,
            )
        logger.warning("Page %d still has missing IDs after attempt %d.", page_num, attempt)

    # Exhausted retries — return partial result
    logger.error("Page %d failed after %d attempts.", page_num, max_retries)
    return PageResult(
        page_number=page_num,
        original_text=doc_a_text,
        benchmark_text=doc_b_text,
        agent1_revised_text=a1_text,
        agent1_ledger=a1_ledger,
        agent2_final_text=a2_text,
        agent2_validation_log=a2_log,
        success=False,
    )


# ---------------------------------------------------------------------------
# Orchestrator — main pipeline
# ---------------------------------------------------------------------------

class Orchestrator:
    """Top-level controller that drives the two-agent review pipeline.

    Responsibilities:
    - Dynamic page detection
    - Page-range parsing
    - Per-page sentence segmentation and ledger creation
    - Sequential page processing through Agent 1 → Agent 2
    - Ledger validation with targeted retry
    - Final DOCX markup generation
    """

    def __init__(
        self,
        doc_a_path: str | Path,
        doc_b_path: str | Path,
        page_param: str = "all",
        *,
        output_path: str | Path = "review_output.docx",
        agent1_model: str | None = None,
        agent2_model: str | None = None,
        orchestrator_model: str | None = None,
        max_retries: int = 2,
        api_key: str | None = None,
    ) -> None:
        self.doc_a_path = Path(doc_a_path)
        self.doc_b_path = Path(doc_b_path)
        self.page_param = page_param
        self.output_path = Path(output_path)
        self.agent1_model = agent1_model or MODEL_STRONG
        self.agent2_model = agent2_model or MODEL_STRONG
        self.orchestrator_model = orchestrator_model or MODEL_CHEAP
        self.max_retries = max_retries

        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.prompts = load_prompts()

        # Will be populated during run()
        self.total_pages_a: int = 0
        self.total_pages_b: int = 0
        self.selected_pages: list[int] = []
        self.page_results: list[PageResult] = []

    # ----- public API -----

    def run(self) -> Path:
        """Execute the full review pipeline and return the output DOCX path."""
        self._detect_pages()
        self._select_pages()
        self._process_pages()
        return self._generate_output()

    # ----- internal steps -----

    def _detect_pages(self) -> None:
        self.total_pages_a = detect_total_pages(self.doc_a_path)
        self.total_pages_b = detect_total_pages(self.doc_b_path)
        logger.info(
            "Document A: %d page(s)  |  Document B: %d page(s)",
            self.total_pages_a,
            self.total_pages_b,
        )

    def _select_pages(self) -> None:
        self.selected_pages = parse_page_param(self.page_param, self.total_pages_a)
        logger.info("Selected pages: %s", self.selected_pages)

    def _process_pages(self) -> None:
        for pg in self.selected_pages:
            logger.info("===== Processing page %d =====", pg)
            doc_a_text = extract_page_text(self.doc_a_path, pg)
            doc_b_text = extract_page_text(self.doc_b_path, pg) if pg <= self.total_pages_b else ""

            if not doc_a_text.strip():
                logger.info("Page %d of Document A is empty — skipping.", pg)
                continue

            # Sentence segmentation & ledger (cheap operation, no model call needed)
            sentences = segment_sentences(doc_a_text)
            ledger = build_sentence_ledger(pg, sentences)
            logger.info("Page %d: %d sentences segmented.", pg, len(sentences))

            # Process through Agent 1 → Agent 2 with retry
            result = retry_page(
                self.client,
                pg,
                doc_a_text,
                doc_b_text,
                ledger,
                max_retries=self.max_retries,
                agent1_model=self.agent1_model,
                agent2_model=self.agent2_model,
            )
            self.page_results.append(result)

            if result.success:
                logger.info("Page %d completed successfully.", pg)
            else:
                logger.warning("Page %d completed with issues.", pg)

    def _generate_output(self) -> Path:
        """Produce the final DOCX with tracked changes."""
        try:
            out = build_tracked_changes_docx(
                self.doc_a_path, self.page_results, self.output_path,
            )
        except Exception:
            logger.exception(
                "Native tracked-changes generation failed — falling back to color markup."
            )
            out = build_fallback_markup_docx(
                self.doc_a_path, self.page_results, self.output_path,
            )
        logger.info("Review complete. Output: %s", out)
        return out


# ---------------------------------------------------------------------------
# Main execution example
# ---------------------------------------------------------------------------

def main() -> None:
    """Example main entry point demonstrating the full pipeline."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Two-agent legal document review system",
    )
    parser.add_argument(
        "--doc-a",
        required=True,
        help="Path to Document A (DOCX to be reviewed)",
    )
    parser.add_argument(
        "--doc-b",
        required=True,
        help="Path to Document B (benchmark DOCX)",
    )
    parser.add_argument(
        "--pages",
        default="all",
        help='Page selection: "1", "1-3", "2,5", or "all" (default: all)',
    )
    parser.add_argument(
        "--output",
        default="review_output.docx",
        help="Output DOCX path (default: review_output.docx)",
    )
    parser.add_argument(
        "--agent1-model",
        default=MODEL_STRONG,
        help=f"Model for Agent 1 legal review (default: {MODEL_STRONG})",
    )
    parser.add_argument(
        "--agent2-model",
        default=MODEL_STRONG,
        help=f"Model for Agent 2 validation (default: {MODEL_STRONG})",
    )
    parser.add_argument(
        "--orchestrator-model",
        default=MODEL_CHEAP,
        help=f"Model for orchestration tasks (default: {MODEL_CHEAP})",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="Max retries per failed page (default: 2)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Anthropic API key (or set ANTHROPIC_API_KEY env var)",
    )
    args = parser.parse_args()

    # ---- Step 1: Load prompts ----
    prompts = load_prompts()
    logger.info("Prompts loaded: orchestrator=%d chars, agent1=%d chars, agent2=%d chars",
                len(prompts.orchestrator), len(prompts.agent1), len(prompts.agent2))

    # ---- Step 2: Initialize orchestrator ----
    orchestrator = Orchestrator(
        doc_a_path=args.doc_a,
        doc_b_path=args.doc_b,
        page_param=args.pages,
        output_path=args.output,
        agent1_model=args.agent1_model,
        agent2_model=args.agent2_model,
        orchestrator_model=args.orchestrator_model,
        max_retries=args.max_retries,
        api_key=args.api_key,
    )

    # ---- Step 3: Detect total pages ----
    orchestrator._detect_pages()
    logger.info("Document A total pages: %d", orchestrator.total_pages_a)
    logger.info("Document B total pages: %d", orchestrator.total_pages_b)

    # ---- Step 4: Select page scope ----
    orchestrator._select_pages()
    logger.info("Pages to process: %s", orchestrator.selected_pages)

    # ---- Step 5-6: Run Agent 1 and Agent 2 per page ----
    orchestrator._process_pages()

    # ---- Step 7: Generate final DOCX markup output ----
    output_file = orchestrator._generate_output()

    # ---- Summary ----
    total = len(orchestrator.page_results)
    passed = sum(1 for r in orchestrator.page_results if r.success)
    failed = total - passed
    logger.info(
        "Pipeline complete. %d/%d pages succeeded, %d failed. Output: %s",
        passed, total, failed, output_file,
    )


if __name__ == "__main__":
    main()
