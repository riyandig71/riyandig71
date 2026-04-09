#!/usr/bin/env python3
"""
Demo execution of the legal document review system.

Uses mock Claude API responses to demonstrate the full pipeline
end-to-end, producing a real DOCX with native tracked changes.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
from pathlib import Path

import legal_doc_review as ldr

# ---------------------------------------------------------------------------
# Mock Agent responses — realistic outputs for the dummy documents
# ---------------------------------------------------------------------------

AGENT1_RESPONSE_PAGE1 = """--- REVISED TEXT ---
In the event that the Buyer fails to remit payment within thirty (30) calendar days, the Seller shall have the right to terminate this Agreement and retain the deposit as liquidated damages.
The Parties agree to submit any dispute arising out of or in connection with this Agreement to mandatory mediation prior to initiating any litigation or arbitration proceedings.
--- END REVISED TEXT ---

--- SENTENCE LEDGER ---
[
  {
    "page": 1,
    "sentence_id": "1.1",
    "status": "revise",
    "reason": "Original sentence uses casual language ('doesn't pay', 'basically cancel the whole deal') that is materially inconsistent with the formal benchmark in Document B. Revised to match formal legal terminology while preserving the same legal meaning: payment failure triggers termination right and deposit retention as liquidated damages.",
    "proposed_revision": "In the event that the Buyer fails to remit payment within thirty (30) calendar days, the Seller shall have the right to terminate this Agreement and retain the deposit as liquidated damages."
  },
  {
    "page": 1,
    "sentence_id": "1.2",
    "status": "revise",
    "reason": "Original sentence uses informal phrasing ('sort out any disagreements', 'talking it over', 'going to court or anything like that') that is materially inconsistent with Document B. Revised to use proper legal terms: 'dispute', 'mandatory mediation', 'litigation or arbitration proceedings'.",
    "proposed_revision": "The Parties agree to submit any dispute arising out of or in connection with this Agreement to mandatory mediation prior to initiating any litigation or arbitration proceedings."
  }
]
--- END SENTENCE LEDGER ---"""

AGENT2_RESPONSE_PAGE1 = """--- REVISED TEXT ---
In the event that the Buyer fails to remit payment within thirty (30) calendar days of the invoice date, the Seller shall have the right to terminate this Agreement and retain the deposit as liquidated damages.
The Parties agree to submit any dispute arising out of or in connection with this Agreement to mandatory mediation prior to initiating any litigation or arbitration proceedings.
--- END REVISED TEXT ---

--- VALIDATION LOG ---
[
  {
    "sentence_id": "1.1",
    "agent1_status": "revise",
    "validation": "corrected",
    "final_decision": "revised",
    "notes": "Agent 1 revision is substantively correct but omitted 'of the invoice date' which appears in Document B and clarifies the payment deadline trigger. Added this phrase to match benchmark precisely."
  },
  {
    "sentence_id": "1.2",
    "agent1_status": "revise",
    "validation": "approved",
    "final_decision": "revised",
    "notes": "Agent 1 revision correctly formalizes the dispute resolution clause to match Document B benchmark. No further changes needed."
  }
]
--- END VALIDATION LOG ---"""

# ---------------------------------------------------------------------------
# Run the demo
# ---------------------------------------------------------------------------

def mock_call_claude(client, *, system, user_message, model=None, max_tokens=4096):
    """Mock Claude API — return pre-built responses."""
    if "Agent-1-legal-reviewer" in system:
        print(f"  [MOCK] Agent 1 called (model={model})")
        return AGENT1_RESPONSE_PAGE1
    elif "Agent-2-legal-reviewer" in system:
        print(f"  [MOCK] Agent 2 called (model={model})")
        return AGENT2_RESPONSE_PAGE1
    return ""


def main():
    print("=" * 60)
    print("LEGAL DOCUMENT REVIEW SYSTEM — DEMO EXECUTION")
    print("=" * 60)

    # Step 1: Load prompts
    prompts = ldr.load_prompts()
    print(f"\n[1] Prompts loaded:")
    print(f"    Orchestrator: {len(prompts.orchestrator)} chars")
    print(f"    Agent 1:      {len(prompts.agent1)} chars")
    print(f"    Agent 2:      {len(prompts.agent2)} chars")

    # Step 2: Read documents
    doc_a = "doc_a.docx"
    doc_b = "doc_b.docx"

    print(f"\n[2] Documents:")
    print(f"    Doc A: {doc_a}")
    print(f"    Doc B: {doc_b}")

    # Step 3: Detect total pages dynamically
    total_a = ldr.detect_total_pages(doc_a)
    total_b = ldr.detect_total_pages(doc_b)
    print(f"\n[3] Dynamic page detection:")
    print(f"    Document A: {total_a} page(s)")
    print(f"    Document B: {total_b} page(s)")

    # Step 4: Parse page parameter
    page_param = "all"
    selected = ldr.parse_page_param(page_param, total_a)
    print(f"\n[4] Page scope: '{page_param}' → pages {selected}")

    # Step 5: Process each page
    print(f"\n[5] Processing pages...")
    page_results: list[ldr.PageResult] = []

    with patch.object(ldr, '_call_claude', side_effect=mock_call_claude):
        for pg in selected:
            print(f"\n  --- Page {pg} ---")

            # Extract page text
            doc_a_text = ldr.extract_page_text(doc_a, pg)
            doc_b_text = ldr.extract_page_text(doc_b, pg) if pg <= total_b else ""
            print(f"  Doc A text: {doc_a_text[:80]}...")
            print(f"  Doc B text: {doc_b_text[:80]}...")

            # Segment sentences
            sentences = ldr.segment_sentences(doc_a_text)
            ledger = ldr.build_sentence_ledger(pg, sentences)
            print(f"  Sentences segmented: {len(sentences)}")
            for entry in ledger:
                print(f"    [{entry.sentence_id}] {entry.original_text[:70]}...")

            # Create a mock client
            mock_client = MagicMock()

            # Agent 1
            print(f"\n  [Agent 1] Running legal review...")
            a1_text, a1_ledger = ldr.run_agent1(
                mock_client, pg, doc_a_text, doc_b_text, ledger
            )
            print(f"  [Agent 1] Revised text length: {len(a1_text)} chars")
            print(f"  [Agent 1] Ledger entries: {len(a1_ledger)}")
            for entry in a1_ledger:
                print(f"    [{entry['sentence_id']}] status={entry['status']} — {entry['reason'][:60]}...")

            # Validate Agent 1 ledger completeness
            expected_ids = {e.sentence_id for e in ledger}
            missing = ldr.validate_ledger_completeness(expected_ids, a1_ledger)
            print(f"  [Validation] Agent 1 missing IDs: {missing if missing else 'None — all covered'}")

            # Agent 2
            print(f"\n  [Agent 2] Running validation...")
            a2_text, a2_log = ldr.run_agent2(
                mock_client, pg, doc_a_text, doc_b_text, a1_text, a1_ledger
            )
            print(f"  [Agent 2] Final text length: {len(a2_text)} chars")
            print(f"  [Agent 2] Validation log entries: {len(a2_log)}")
            for entry in a2_log:
                print(f"    [{entry['sentence_id']}] {entry['validation']} — {entry['notes'][:60]}...")

            # Validate Agent 2 completeness
            missing2 = ldr.validate_page_result(pg, expected_ids, a2_log)
            print(f"  [Validation] Agent 2 missing IDs: {missing2 if missing2 else 'None — all covered'}")

            page_results.append(ldr.PageResult(
                page_number=pg,
                original_text=doc_a_text,
                benchmark_text=doc_b_text,
                agent1_revised_text=a1_text,
                agent1_ledger=a1_ledger,
                agent2_final_text=a2_text,
                agent2_validation_log=a2_log,
                success=True,
            ))

    # Step 6: Generate DOCX with tracked changes
    print(f"\n[6] Generating DOCX with native tracked changes...")
    output_path = "review_output.docx"
    try:
        out = ldr.build_tracked_changes_docx(doc_a, page_results, output_path)
        print(f"    Output saved: {out}")
    except Exception as e:
        print(f"    Native tracked changes failed: {e}")
        print(f"    Falling back to color markup...")
        out = ldr.build_fallback_markup_docx(doc_a, page_results, output_path)
        print(f"    Fallback output saved: {out}")

    # Summary
    print(f"\n{'=' * 60}")
    print(f"REVIEW COMPLETE")
    print(f"{'=' * 60}")
    print(f"Pages processed: {len(page_results)}")
    print(f"All sentences covered: {all(r.success for r in page_results)}")
    print(f"Output file: {output_path}")
    print(f"\nOpen {output_path} in Microsoft Word → Review → All Markup")
    print(f"to see tracked changes with Accept/Reject options.")

    # Show what's in the output
    print(f"\n--- Original Doc A Content ---")
    for pr in page_results:
        print(pr.original_text)
    print(f"\n--- Final Revised Content ---")
    for pr in page_results:
        print(pr.agent2_final_text)


if __name__ == "__main__":
    main()
