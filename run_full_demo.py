#!/usr/bin/env python3
"""
Full 2-page demo execution of the legal document review system.
Uses mock Claude API responses to demonstrate the complete pipeline.
"""

from __future__ import annotations
import json
from unittest.mock import MagicMock, patch
from pathlib import Path

import legal_doc_review as ldr

# ---------------------------------------------------------------------------
# Mock responses — Agent 1 and Agent 2 for page 1 and page 2
# ---------------------------------------------------------------------------

def mock_agent1_page1(sentences):
    """Generate Agent 1 response for page 1."""
    ledger = []
    revisions = {
        "1.3": {
            "status": "revise",
            "reason": "Casual language ('basically about', 'stuff') materially inconsistent with formal benchmark. Revised to formal legal terminology.",
            "revision": "SCOPE OF SERVICES\nThe Service Provider shall render professional consulting services to the Client encompassing legal compliance advisory and regulatory guidance pertaining to the Client's business operations within the jurisdiction of the Republic of Indonesia."
        },
        "1.4": {
            "status": "revise",
            "reason": "Informal phrasing ('helping with legal compliance stuff') inconsistent with Document B benchmark.",
            "revision": "The scope of services shall include, but not be limited to, general legal consultation, contract drafting and review, regulatory filing assistance, and ongoing compliance monitoring."
        },
        "1.6": {
            "status": "revise",
            "reason": "Casual phrasing ('aren't listed here', 'discuss and add them later'). Benchmark requires formal addendum language.",
            "revision": "Any services not expressly enumerated herein shall require a written addendum executed by both Parties prior to commencement."
        },
        "1.8": {
            "status": "revise",
            "reason": "Informal ('goes for 12 months'). Benchmark uses formal term structure.",
            "revision": "TERM AND TERMINATION\nThis Agreement shall commence on February 1, 2026 and shall remain in effect for a period of twelve (12) months, terminating on January 31, 2027, unless earlier terminated in accordance with the provisions herein."
        },
        "1.9": {
            "status": "revise",
            "reason": "Casual ('If both sides are happy'). Benchmark uses formal renewal clause.",
            "revision": "This Agreement may be renewed for successive twelve-month periods upon mutual written consent of both Parties, provided that such consent is communicated no fewer than thirty (30) calendar days prior to the expiration of the then-current term."
        },
        "1.13": {
            "status": "revise",
            "reason": "Section heading informal ('PAYMENT TERMS'). Benchmark uses 'FEES AND PAYMENT'. Body text also informal.",
            "revision": "FEES AND PAYMENT\nIn consideration of the services rendered hereunder, the Client shall pay the Service Provider a monthly retainer fee of IDR 50,000,000 (fifty million Indonesian Rupiah)."
        },
        "1.14": {
            "status": "revise",
            "reason": "Informal phrasing. Benchmark uses 'due and payable within fourteen (14) calendar days following the issuance'.",
            "revision": "Payment shall be due and payable within fourteen (14) calendar days following the issuance of the Service Provider's invoice at the commencement of each calendar month."
        },
        "1.15": {
            "status": "revise",
            "reason": "Informal ('doesn't pay on time, we'll charge'). Benchmark uses formal penalty clause.",
            "revision": "In the event of late payment, the Client shall be liable for a late payment penalty of 1.5% per month, calculated on the outstanding balance from the date such payment was due."
        },
        "1.19": {
            "status": "revise",
            "reason": "Casual heading and body ('WHAT WE PROMISE TO DO', 'do our best'). Benchmark uses professional standards language.",
            "revision": "SERVICE PROVIDER'S OBLIGATIONS\nThe Service Provider shall perform all services with the degree of skill, care, and diligence consistent with accepted professional standards and practices in the legal consulting industry."
        },
        "1.25": {
            "status": "revise",
            "reason": "Casual ('Both sides agree to keep each other's confidential information secret'). Benchmark uses formal undertaking language.",
            "revision": "CONFIDENTIALITY\nEach Party undertakes to maintain the strict confidentiality of all Confidential Information received from the other Party and shall not disclose such information to any third party without prior written consent."
        },
        "1.27": {
            "status": "revise",
            "reason": "Informal ('will continue for 3 years even after'). Benchmark uses 'survive the termination or expiration' language.",
            "revision": "The obligations of confidentiality set forth herein shall survive the termination or expiration of this Agreement for a period of three (3) years."
        },
    }

    revised_texts = []
    for s in sentences:
        sid = s["sentence_id"]
        if sid in revisions:
            ledger.append({
                "page": 1, "sentence_id": sid,
                "status": revisions[sid]["status"],
                "reason": revisions[sid]["reason"],
                "proposed_revision": revisions[sid]["revision"],
            })
            revised_texts.append(revisions[sid]["revision"])
        else:
            ledger.append({
                "page": 1, "sentence_id": sid,
                "status": "unchanged",
                "reason": "Sentence is acceptable and consistent with benchmark or is structural element.",
                "proposed_revision": "",
            })
            revised_texts.append(s["original_text"])

    revised_text = " ".join(revised_texts)
    return (
        f"--- REVISED TEXT ---\n{revised_text}\n--- END REVISED TEXT ---\n\n"
        f"--- SENTENCE LEDGER ---\n{json.dumps(ledger, indent=2)}\n--- END SENTENCE LEDGER ---"
    )


def mock_agent1_page2(sentences):
    """Generate Agent 1 response for page 2."""
    ledger = []
    revisions = {
        "2.2": {
            "status": "revise",
            "reason": "Informal heading and body ('WHO OWNS THE WORK', 'work product and deliverables we create'). Benchmark uses 'INTELLECTUAL PROPERTY RIGHTS' and formal language.",
            "revision": "INTELLECTUAL PROPERTY RIGHTS\nAll work product, deliverables, and materials created by the Service Provider in the course of performing services under this Agreement shall constitute \"Work Product\" and shall be the exclusive property of the Client upon full payment of all applicable fees."
        },
        "2.4": {
            "status": "revise",
            "reason": "Informal ('our general know-how, methodologies, and tools that we developed independently remain our property'). Benchmark uses formal IP retention clause.",
            "revision": "Notwithstanding the foregoing, the Service Provider shall retain all rights, title, and interest in and to its pre-existing intellectual property, proprietary methodologies, tools, and general professional know-how."
        },
        "2.7": {
            "status": "revise",
            "reason": "Informal heading and liability cap language. Benchmark uses formal limitation of liability clause.",
            "revision": "LIMITATION OF LIABILITY AND INDEMNIFICATION\nThe aggregate liability of the Service Provider under or in connection with this Agreement shall not exceed the total fees actually paid by the Client during the six (6) month period immediately preceding the event giving rise to such liability."
        },
        "2.8": {
            "status": "revise",
            "reason": "Casual ('We're not responsible', 'even if we were told they might happen'). Benchmark uses formal consequential damages exclusion.",
            "revision": "In no event shall either Party be liable to the other for any indirect, incidental, special, consequential, or punitive damages, including but not limited to loss of profits, loss of revenue, or loss of business opportunity, regardless of whether such Party has been advised of the possibility of such damages."
        },
        "2.10": {
            "status": "revise",
            "reason": "Informal force majeure clause. Benchmark includes formal Force Majeure definition.",
            "revision": "Neither Party shall be liable for any delay or failure to perform its obligations under this Agreement to the extent that such delay or failure is attributable to Force Majeure events, including but not limited to natural disasters, acts of government, epidemics, or pandemics."
        },
        "2.12": {
            "status": "revise",
            "reason": "Casual ('If there's a disagreement between us, we'll first try to sort it out through friendly discussion'). Benchmark uses formal dispute resolution language.",
            "revision": "DISPUTE RESOLUTION AND GOVERNING LAW\nIn the event of any dispute, controversy, or claim arising out of or in connection with this Agreement, the Parties shall first attempt to resolve the matter through good-faith negotiation for a period of thirty (30) calendar days."
        },
        "2.13": {
            "status": "revise",
            "reason": "Informal mediation clause. Benchmark specifies Jakarta and formal mediation.",
            "revision": "If the dispute cannot be resolved through negotiation, the Parties agree to submit the matter to mediation administered by a mutually agreed-upon mediator in Jakarta, Indonesia."
        },
        "2.17": {
            "status": "revise",
            "reason": "Casual heading and body ('GENERAL STUFF', 'entire deal'). Benchmark uses 'GENERAL PROVISIONS' and formal entire agreement clause.",
            "revision": "GENERAL PROVISIONS\nThis Agreement constitutes the entire agreement between the Parties with respect to the subject matter hereof and supersedes all prior negotiations, representations, warranties, commitments, offers, and agreements, whether written or oral."
        },
        "2.21": {
            "status": "revise",
            "reason": "Informal counterparts clause. Benchmark uses formal 'executed in counterparts' language.",
            "revision": "This Agreement may be executed in counterparts, each of which shall be deemed an original and all of which together shall constitute one and the same instrument."
        },
        "2.22": {
            "status": "revise",
            "reason": "Informal signature block. Benchmark uses 'IN WITNESS WHEREOF' and 'For and on behalf of' language.",
            "revision": "IN WITNESS WHEREOF\nFor and on behalf of PT Maju Bersama: ___________________     Date: ___________"
        },
        "2.23": {
            "status": "revise",
            "reason": "Signature block missing formal prefix.",
            "revision": "For and on behalf of Mr. Budi Santoso: ___________________    Date: ___________"
        },
    }

    revised_texts = []
    for s in sentences:
        sid = s["sentence_id"]
        if sid in revisions:
            ledger.append({
                "page": 2, "sentence_id": sid,
                "status": revisions[sid]["status"],
                "reason": revisions[sid]["reason"],
                "proposed_revision": revisions[sid]["revision"],
            })
            revised_texts.append(revisions[sid]["revision"])
        else:
            ledger.append({
                "page": 2, "sentence_id": sid,
                "status": "unchanged",
                "reason": "Sentence is acceptable and consistent with benchmark or is structural element.",
                "proposed_revision": "",
            })
            revised_texts.append(s["original_text"])

    revised_text = " ".join(revised_texts)
    return (
        f"--- REVISED TEXT ---\n{revised_text}\n--- END REVISED TEXT ---\n\n"
        f"--- SENTENCE LEDGER ---\n{json.dumps(ledger, indent=2)}\n--- END SENTENCE LEDGER ---"
    )


def mock_agent2_page(page_num, agent1_ledger):
    """Generate Agent 2 validation response for any page."""
    validation_log = []
    for entry in agent1_ledger:
        sid = entry["sentence_id"]
        a1_status = entry["status"]

        if a1_status == "revise":
            validation_log.append({
                "sentence_id": sid,
                "agent1_status": a1_status,
                "validation": "approved",
                "final_decision": "revised",
                "notes": f"Agent 1 revision for {sid} correctly formalizes casual language to match Document B benchmark. Revision approved.",
            })
        else:
            validation_log.append({
                "sentence_id": sid,
                "agent1_status": a1_status,
                "validation": "approved",
                "final_decision": "unchanged",
                "notes": f"Sentence {sid} correctly identified as unchanged. No revision needed.",
            })

    # Agent 2 makes one correction on first revised sentence of the page
    for v in validation_log:
        if v["agent1_status"] == "revise":
            v["validation"] = "corrected"
            v["notes"] = f"Agent 1 revision for {v['sentence_id']} approved with minor refinement: ensured cross-reference consistency with benchmark Document B section numbering."
            break

    return validation_log


# Track which page we're processing
_current_page = [0]
_current_ledger = [None]


def mock_call_claude(client, *, system, user_message, model=None, max_tokens=4096):
    """Mock Claude API with page-aware responses."""
    # Determine page number from user message
    import re
    page_match = re.search(r'Page:\s*(\d+)', user_message)
    page_num = int(page_match.group(1)) if page_match else 1

    if "Agent-1-legal-reviewer" in system:
        print(f"    [MOCK API] Agent 1 called for page {page_num} (model={model})")
        # Extract sentence ledger from user message
        ledger_match = re.search(r'--- Sentence Ledger ---\s*(\[.*?\])', user_message, re.DOTALL)
        sentences = json.loads(ledger_match.group(1)) if ledger_match else []
        _current_ledger[0] = sentences

        if page_num == 1:
            return mock_agent1_page1(sentences)
        else:
            return mock_agent1_page2(sentences)

    elif "Agent-2-legal-reviewer" in system:
        print(f"    [MOCK API] Agent 2 called for page {page_num} (model={model})")
        # Extract Agent 1 ledger from user message
        ledger_match = re.search(r'--- Agent 1 Sentence Ledger ---\s*(\[.*?\])', user_message, re.DOTALL)
        a1_ledger = json.loads(ledger_match.group(1)) if ledger_match else []

        # Extract Agent 1 revised text
        text_match = re.search(r'--- Agent 1 Revised Text ---\s*(.*?)\s*---', user_message, re.DOTALL)
        a1_text = text_match.group(1).strip() if text_match else ""

        validation_log = mock_agent2_page(page_num, a1_ledger)

        return (
            f"--- REVISED TEXT ---\n{a1_text}\n--- END REVISED TEXT ---\n\n"
            f"--- VALIDATION LOG ---\n{json.dumps(validation_log, indent=2)}\n--- END VALIDATION LOG ---"
        )

    return ""


# ---------------------------------------------------------------------------
# Main execution
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("LEGAL DOC REVIEW — FULL 2-PAGE DEMO")
    print("=" * 60)

    # Step 1: Load prompts
    prompts = ldr.load_prompts()
    print(f"\n[1] Prompts loaded")

    # Step 2: Detect pages
    total_a = ldr.detect_total_pages("doc_a.docx")
    total_b = ldr.detect_total_pages("doc_b.docx")
    pages = ldr.parse_page_param("all", total_a)
    print(f"[2] Doc A: {total_a} pages | Doc B: {total_b} pages | Processing: {pages}")

    # Step 3: Show cost estimate
    est = ldr.estimate_cost("doc_a.docx", "doc_b.docx", pages, total_b)
    print(est.display())
    print("\n  >> Auto-confirming for demo (mock mode) <<")

    # Step 4-5: Process each page
    page_results: list[ldr.PageResult] = []

    with patch.object(ldr, '_call_claude', side_effect=mock_call_claude):
        for pg in pages:
            print(f"\n{'='*60}")
            print(f"  PROCESSING PAGE {pg}")
            print(f"{'='*60}")

            doc_a_text = ldr.extract_page_text("doc_a.docx", pg)
            doc_b_text = ldr.extract_page_text("doc_b.docx", pg) if pg <= total_b else ""

            sentences = ldr.segment_sentences(doc_a_text)
            ledger = ldr.build_sentence_ledger(pg, sentences)
            print(f"  Sentences: {len(sentences)}")

            mock_client = MagicMock()

            # Agent 1
            print(f"\n  [Agent 1] Legal review...")
            a1_text, a1_ledger = ldr.run_agent1(mock_client, pg, doc_a_text, doc_b_text, ledger)
            revised_count = sum(1 for e in a1_ledger if e.get("status") == "revise")
            unchanged_count = sum(1 for e in a1_ledger if e.get("status") == "unchanged")
            print(f"  [Agent 1] Result: {revised_count} revised, {unchanged_count} unchanged")

            # Validate Agent 1
            expected_ids = {e.sentence_id for e in ledger}
            missing = ldr.validate_ledger_completeness(expected_ids, a1_ledger)
            print(f"  [Validate] Agent 1 missing IDs: {missing if missing else 'NONE — PASS'}")

            # Agent 2
            print(f"\n  [Agent 2] Validation review...")
            a2_text, a2_log = ldr.run_agent2(mock_client, pg, doc_a_text, doc_b_text, a1_text, a1_ledger)
            approved = sum(1 for e in a2_log if e.get("validation") == "approved")
            corrected = sum(1 for e in a2_log if e.get("validation") == "corrected")
            print(f"  [Agent 2] Result: {approved} approved, {corrected} corrected")

            # Validate Agent 2
            missing2 = ldr.validate_page_result(pg, expected_ids, a2_log)
            print(f"  [Validate] Agent 2 missing IDs: {missing2 if missing2 else 'NONE — PASS'}")

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

    # Step 6: Generate DOCX
    print(f"\n{'='*60}")
    print(f"  GENERATING OUTPUT DOCX")
    print(f"{'='*60}")

    output_path = "review_output.docx"
    try:
        out = ldr.build_tracked_changes_docx("doc_a.docx", page_results, output_path)
        print(f"  Native tracked changes: {out}")
    except Exception as e:
        print(f"  Native failed ({e}), using fallback...")
        out = ldr.build_fallback_markup_docx("doc_a.docx", page_results, output_path)
        print(f"  Fallback markup: {out}")

    # Step 7: Verify output
    print(f"\n{'='*60}")
    print(f"  OUTPUT VERIFICATION")
    print(f"{'='*60}")

    from docx import Document
    from lxml import etree
    from docx.oxml.ns import qn

    doc = Document(output_path)
    total_dels = 0
    total_inss = 0
    for para in doc.paragraphs:
        dels = para._element.findall('.//' + qn('w:del'))
        inss = para._element.findall('.//' + qn('w:ins'))
        total_dels += len(dels)
        total_inss += len(inss)

    print(f"  Total w:del (deletions):   {total_dels}")
    print(f"  Total w:ins (insertions):  {total_inss}")

    # Check comments
    comment_count = 0
    for rel in doc.part.rels.values():
        if 'comments' in rel.reltype:
            root = etree.fromstring(rel.target_part.blob)
            comment_count = len(root.findall(qn('w:comment')))
    print(f"  Total comments:            {comment_count}")

    # Check settings
    tc = doc.settings.element.findall(qn('w:trackChanges'))
    print(f"  Track Changes enabled:     {'YES' if tc else 'NO'}")

    # Summary
    total_pages = len(page_results)
    total_sents = sum(len(pr.agent2_validation_log) for pr in page_results)
    total_revised = sum(
        1 for pr in page_results
        for v in pr.agent2_validation_log
        if v.get("final_decision") == "revised"
    )

    print(f"\n{'='*60}")
    print(f"  FINAL SUMMARY")
    print(f"{'='*60}")
    print(f"  Pages processed:     {total_pages}")
    print(f"  Sentences reviewed:  {total_sents}")
    print(f"  Sentences revised:   {total_revised}")
    print(f"  Sentences unchanged: {total_sents - total_revised}")
    print(f"  Output file:         {output_path}")
    print(f"\n  Open in Word → Review → All Markup → Accept/Reject changes")


if __name__ == "__main__":
    main()
