#!/usr/bin/env python3
"""
V2 Demo Runner
==============
End-to-end demo with mock API responses showing the full V2 pipeline.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from v2.doc_reader import read_document, map_paragraphs, map_tables, segment_sentences
from v2.table_engine import compare_tables, table_diffs_for_agent, table_diffs_to_decisions
from v2.review_engine import Action, ReviewDecision, run_agent1, run_agent2, _extract_json_array, parse_decisions
from v2.validation import validate_decisions
from v2.redline_engine import apply_redline
from v2.orchestrator import estimate_cost, _serialize_para, _empty_para_dict
from v2.prompt_store import load_prompts, MODEL_STRONG

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("v2.demo")


def build_mock_agent1_decisions(doc_a_paras, doc_b_paras):
    """Build realistic Agent 1 decisions based on actual document content."""
    decisions = []
    for a_dict, b_dict in zip(doc_a_paras, doc_b_paras):
        a_text = a_dict["text"]
        b_text = b_dict["text"]

        if not b_text:
            # No benchmark — keep all sentences
            for s in a_dict.get("sentences", []):
                decisions.append({
                    "para_idx": a_dict["para_idx"],
                    "sent_idx": s["idx"],
                    "element_type": "sentence",
                    "action": "KEEP",
                    "original": s["text"],
                    "revised": "",
                    "reason": "No benchmark counterpart available.",
                })
            continue

        # Compare sentences
        for s in a_dict.get("sentences", []):
            sent = s["text"]
            # Decide based on informality markers
            informal_markers = [
                "basically", "stuff", "sort out", "talking it over",
                "doesn't", "we'll", "won't", "we're", "aren't",
                "going to", "happy", "General Stuff", "General stuff",
                "Who Owns", "What We Promise", "How Long",
                "the whole deal", "anything like that",
                "do our best", "try to respond",
            ]
            is_informal = any(m.lower() in sent.lower() for m in informal_markers)

            if is_informal and b_text:
                # Find corresponding formal text from B
                b_sents = segment_sentences(b_text)
                # Use simple heuristic: pick the B sentence with most word overlap
                best_b = ""
                best_overlap = 0
                a_words = set(sent.lower().split())
                for bs in b_sents:
                    b_words = set(bs.lower().split())
                    overlap = len(a_words & b_words)
                    if overlap > best_overlap:
                        best_overlap = overlap
                        best_b = bs

                if best_b and best_overlap >= 2:
                    decisions.append({
                        "para_idx": a_dict["para_idx"],
                        "sent_idx": s["idx"],
                        "element_type": "sentence",
                        "action": "REPLACE",
                        "original": sent,
                        "revised": best_b,
                        "reason": "Informal language materially inconsistent with formal benchmark Document B.",
                    })
                else:
                    decisions.append({
                        "para_idx": a_dict["para_idx"],
                        "sent_idx": s["idx"],
                        "element_type": "sentence",
                        "action": "KEEP",
                        "original": sent,
                        "revised": "",
                        "reason": "Wording is acceptable and consistent with legal standards.",
                    })
            else:
                decisions.append({
                    "para_idx": a_dict["para_idx"],
                    "sent_idx": s["idx"],
                    "element_type": "sentence",
                    "action": "KEEP",
                    "original": sent,
                    "revised": "",
                    "reason": "Wording is acceptable and consistent with legal standards.",
                })

    return decisions


def build_mock_agent2_validation(agent1_decisions):
    """Build Agent 2 validation of Agent 1 decisions."""
    validated = []
    for d in agent1_decisions:
        entry = dict(d)
        entry["agent1_action"] = d["action"]

        if d["action"] == "REPLACE":
            # Agent 2 approves most, corrects a few
            entry["validation"] = "approved"
            entry["correction_note"] = ""
        else:
            entry["validation"] = "approved"
            entry["correction_note"] = ""

        validated.append(entry)
    return validated


def mock_call_claude(client, *, system, user_message, model=None, max_tokens=4096):
    """Mock Claude API with structured JSON responses."""
    # Parse the paragraph data from the user message
    if "Agent-1-legal-reviewer" in system:
        # Extract paragraph pairs from the message and build decisions
        # For simplicity, return a pre-built JSON based on content analysis
        logger.info("  [MOCK] Agent 1 called (model=%s)", model)

        # Parse paragraphs from message
        import re
        para_blocks = re.findall(
            r'Document A \[para_idx=(\d+)\]:\s*(.*?)(?=\nDocument B|\n---|\Z)',
            user_message, re.DOTALL
        )
        b_blocks = re.findall(
            r'Document B counterpart:\s*(.*?)(?=\nSentences|\n---|\Z)',
            user_message, re.DOTALL
        )
        sent_blocks = re.findall(
            r'Sentences in Document A:\s*(.*?)(?=\n---|$)',
            user_message, re.DOTALL
        )

        # Build structured paragraph data
        doc_a_paras = []
        doc_b_paras = []

        for i, (pidx, a_text) in enumerate(para_blocks):
            sents = []
            if i < len(sent_blocks):
                for m in re.finditer(r'\[(\d+)\]\s*(.*)', sent_blocks[i]):
                    sents.append({"idx": int(m.group(1)), "text": m.group(2).strip()})

            doc_a_paras.append({
                "para_idx": int(pidx),
                "text": a_text.strip(),
                "sentences": sents,
            })

            b_text = b_blocks[i].strip() if i < len(b_blocks) else ""
            doc_b_paras.append({
                "para_idx": int(pidx),
                "text": b_text,
            })

        decisions = build_mock_agent1_decisions(doc_a_paras, doc_b_paras)
        return json.dumps(decisions, indent=2)

    elif "Agent-2-legal-reviewer" in system:
        logger.info("  [MOCK] Agent 2 called (model=%s)", model)

        # Extract Agent 1 decisions
        import re
        match = re.search(r'--- Agent 1 Decisions ---\s*(\[.*\])', user_message, re.DOTALL)
        if match:
            a1_decisions = json.loads(match.group(1))
            validated = build_mock_agent2_validation(a1_decisions)
            return json.dumps(validated, indent=2)
        return "[]"

    return "[]"


def main():
    print("=" * 60)
    print("  V2 LEGAL DOC REVIEW — END-TO-END DEMO")
    print("=" * 60)

    doc_a_path = "doc_a.docx"
    doc_b_path = "doc_b.docx"

    # Verify docs exist
    if not Path(doc_a_path).exists() or not Path(doc_b_path).exists():
        print("ERROR: doc_a.docx and doc_b.docx must exist. Run create_full_docs.py first.")
        sys.exit(1)

    # Step 1: Load prompts
    prompts = load_prompts()
    print(f"\n[1] Prompts loaded: agent1={len(prompts.agent1)} chars, agent2={len(prompts.agent2)} chars")

    # Step 2: Read documents
    doc_a = read_document(doc_a_path)
    doc_b = read_document(doc_b_path)
    print(f"\n[2] Documents read:")
    print(f"    Doc A: {len(doc_a.paragraphs)} paragraphs, {len(doc_a.tables)} tables")
    print(f"    Doc B: {len(doc_b.paragraphs)} paragraphs, {len(doc_b.tables)} tables")
    total_sents = sum(len(p.sentences) for p in doc_a.paragraphs)
    print(f"    Total sentences in Doc A: {total_sents}")

    # Step 3: Map paragraphs
    para_mapping = map_paragraphs(doc_a, doc_b)
    table_mapping = map_tables(doc_a, doc_b)
    matched = sum(1 for _, b in para_mapping if b is not None)
    print(f"\n[3] Paragraph mapping: {len(para_mapping)} total, {matched} matched to Doc B")

    # Step 4: Cost estimate
    est = estimate_cost(doc_a, doc_b, MODEL_STRONG, MODEL_STRONG)
    print(est.display())
    print("  >> Auto-confirming for demo <<")

    # Step 5: Build paragraph pairs for agents
    b_para_map = {p.idx: p for p in doc_b.paragraphs}
    all_a_dicts = []
    all_b_dicts = []
    for a_idx, b_idx in para_mapping:
        a_para = next((p for p in doc_a.paragraphs if p.idx == a_idx), None)
        if a_para is None:
            continue
        all_a_dicts.append(_serialize_para(a_para))
        if b_idx is not None and b_idx in b_para_map:
            all_b_dicts.append(_serialize_para(b_para_map[b_idx]))
        else:
            all_b_dicts.append(_empty_para_dict(a_idx))

    # Step 6: Run Agent 1 + Agent 2 (mocked)
    mock_client = MagicMock()

    with patch("v2.review_engine.call_claude", side_effect=mock_call_claude):
        print(f"\n[6] Running Agent 1...")
        a1_decisions = run_agent1(mock_client, all_a_dicts, all_b_dicts, model=MODEL_STRONG)
        keeps = sum(1 for d in a1_decisions if d.action == Action.KEEP)
        replaces = sum(1 for d in a1_decisions if d.action == Action.REPLACE)
        print(f"    Agent 1: {len(a1_decisions)} decisions (KEEP={keeps}, REPLACE={replaces})")

        # Validate Agent 1
        a1_val = validate_decisions(a1_decisions, doc_a, auto_fix=True)
        if a1_val.fixed_decisions:
            a1_decisions = a1_val.fixed_decisions
        print(f"    Validation: {'PASSED' if a1_val.passed else 'FAILED'} "
              f"({len(a1_val.warnings)} warnings)")

        print(f"\n[7] Running Agent 2...")
        a2_decisions = run_agent2(mock_client, all_a_dicts, all_b_dicts, a1_decisions, model=MODEL_STRONG)
        keeps2 = sum(1 for d in a2_decisions if d.action == Action.KEEP)
        replaces2 = sum(1 for d in a2_decisions if d.action == Action.REPLACE)
        print(f"    Agent 2: {len(a2_decisions)} decisions (KEEP={keeps2}, REPLACE={replaces2})")

        # Final validation
        final = a2_decisions if a2_decisions else a1_decisions
        final_val = validate_decisions(final, doc_a, auto_fix=True)
        if final_val.fixed_decisions:
            final = final_val.fixed_decisions
        print(f"    Final validation: {'PASSED' if final_val.passed else 'FAILED'}")

    # Step 8: Apply redline
    print(f"\n[8] Applying redline to DOCX...")
    output_path = "review_output_v2.docx"
    out = apply_redline(doc_a_path, doc_a, final, output_path)
    print(f"    Output: {out}")

    # Step 9: Verify output
    print(f"\n[9] Output verification:")
    from docx import Document
    from lxml import etree
    from docx.oxml.ns import qn

    doc = Document(str(out))
    total_dels = sum(len(p._element.findall('.//' + qn('w:del'))) for p in doc.paragraphs)
    total_inss = sum(len(p._element.findall('.//' + qn('w:ins'))) for p in doc.paragraphs)
    tc = doc.settings.element.findall(qn('w:trackChanges'))

    comment_count = 0
    for rel in doc.part.rels.values():
        if 'comments' in rel.reltype:
            root = etree.fromstring(rel.target_part.blob)
            comment_count = len(root.findall(qn('w:comment')))

    print(f"    w:del (deletions):  {total_dels}")
    print(f"    w:ins (insertions): {total_inss}")
    print(f"    Comments:           {comment_count}")
    print(f"    Track Changes:      {'ON' if tc else 'OFF'}")

    # Summary
    print(f"\n{'='*60}")
    print(f"  V2 DEMO COMPLETE")
    print(f"{'='*60}")
    print(f"  Paragraphs reviewed: {len(para_mapping)}")
    print(f"  Decisions total:     {len(final)}")
    print(f"  KEEP:                {sum(1 for d in final if d.action == Action.KEEP)}")
    print(f"  REPLACE:             {sum(1 for d in final if d.action == Action.REPLACE)}")
    print(f"  INSERT:              {sum(1 for d in final if d.action == Action.INSERT)}")
    print(f"  DELETE:              {sum(1 for d in final if d.action == Action.DELETE)}")
    print(f"  UNCERTAIN:           {sum(1 for d in final if d.action == Action.UNCERTAIN)}")
    print(f"  Output: {out}")
    print(f"\n  Open in Word → Review → All Markup → Accept/Reject")


if __name__ == "__main__":
    main()
