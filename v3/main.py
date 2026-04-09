#!/usr/bin/env python3
"""
Legal Document Review System — V3
==================================
CLI entrypoint with multi-part document support.

Usage:
    # Single file Doc A:
    python -m v3.main --doc-a CLOD_415_EN.docx --doc-b benchmark.docx

    # Folder Doc A (auto-detects parts):
    python -m v3.main --doc-a "CLOD 394/" --doc-b "20260214 - CLOD 394.docx"

    # Custom output directory:
    python -m v3.main --doc-a "CLOD 394/" --doc-b benchmark.docx --output-dir results/
"""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

load_dotenv()

from .orchestrator import Orchestrator
from .prompt_store import MODEL_STRONG, MODEL_CHEAP, load_prompts
from .review_engine import Action

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("v3.main")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="V3 Legal Document Review — multi-part DOCX redline system",
    )
    parser.add_argument(
        "--doc-a", required=True,
        help="Document A: single .docx file OR folder containing Part 1/2/3... .docx files",
    )
    parser.add_argument(
        "--doc-b", required=True,
        help="Document B: single .docx benchmark file",
    )
    parser.add_argument(
        "--output-dir", default="output",
        help="Output directory for redline DOCX files (default: output/)",
    )
    parser.add_argument("--agent1-model", default=MODEL_STRONG)
    parser.add_argument("--agent2-model", default=MODEL_STRONG)
    parser.add_argument("--batch-size", type=int, default=30,
                        help="Paragraphs per agent batch (default: 30)")
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--skip-confirm", action="store_true",
                        help="Skip cost confirmation prompt")
    args = parser.parse_args()

    # Load prompts
    prompts = load_prompts()
    logger.info("V3 prompts loaded: agent1=%d chars, agent2=%d chars",
                len(prompts.agent1), len(prompts.agent2))

    # Run pipeline
    orch = Orchestrator(
        doc_a_path=args.doc_a,
        doc_b_path=args.doc_b,
        output_dir=args.output_dir,
        agent1_model=args.agent1_model,
        agent2_model=args.agent2_model,
        batch_size=args.batch_size,
        max_retries=args.max_retries,
        api_key=args.api_key,
    )

    result = orch.run(skip_confirmation=args.skip_confirm)

    if result is None:
        sys.exit(0)

    # Final report
    print(f"\n{'='*60}")
    print(f"  V3 REVIEW COMPLETE")
    print(f"{'='*60}")
    print(f"  Doc A: {result.resolved.doc_a_label} ({len(result.resolved.doc_a_parts)} parts)")
    print(f"  Doc B: {result.resolved.doc_b_path.name}")
    print(f"  Multi-part: {'YES' if result.resolved.doc_a_is_multipart else 'NO'}")

    total_decisions = 0
    total_keeps = 0
    total_replaces = 0

    for pr in result.part_results:
        status = "OK" if pr.success else f"FAILED: {pr.error}"
        n_dec = len(pr.final_decisions)
        n_keep = sum(1 for d in pr.final_decisions if d.action == Action.KEEP)
        n_repl = sum(1 for d in pr.final_decisions if d.action == Action.REPLACE)
        total_decisions += n_dec
        total_keeps += n_keep
        total_replaces += n_repl

        print(f"\n  Part {pr.part.part_number}: {pr.part.label}")
        print(f"    Status:    {status}")
        print(f"    Decisions: {n_dec} (KEEP={n_keep}, REPLACE={n_repl})")
        if pr.output_path:
            print(f"    Output:    {pr.output_path}")

    print(f"\n  TOTALS")
    print(f"    Decisions: {total_decisions}")
    print(f"    KEEP:      {total_keeps}")
    print(f"    REPLACE:   {total_replaces}")
    print(f"\n  Open output files in Word → Review → All Markup")


if __name__ == "__main__":
    main()
