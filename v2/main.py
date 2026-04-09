#!/usr/bin/env python3
"""
Legal Document Review System — V2
==================================
CLI entrypoint.

Usage:
    python -m v2.main --doc-a contract.docx --doc-b benchmark.docx
    python -m v2.main --doc-a contract.docx --doc-b benchmark.docx --output redline.docx
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from .orchestrator import Orchestrator
from .prompt_store import MODEL_STRONG, MODEL_CHEAP, load_prompts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("v2.main")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="V2 Legal Document Review — two-agent DOCX redline system",
    )
    parser.add_argument("--doc-a", required=True, help="Document A (DOCX to review)")
    parser.add_argument("--doc-b", required=True, help="Document B (benchmark DOCX)")
    parser.add_argument("--output", default="review_output.docx", help="Output DOCX path")
    parser.add_argument("--agent1-model", default=MODEL_STRONG)
    parser.add_argument("--agent2-model", default=MODEL_STRONG)
    parser.add_argument("--batch-size", type=int, default=30,
                        help="Paragraphs per agent batch (default: 30)")
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--skip-confirm", action="store_true",
                        help="Skip cost confirmation prompt")
    args = parser.parse_args()

    # Load and log prompts
    prompts = load_prompts()
    logger.info("V2 prompts loaded: agent1=%d chars, agent2=%d chars",
                len(prompts.agent1), len(prompts.agent2))

    # Run pipeline
    orch = Orchestrator(
        doc_a_path=args.doc_a,
        doc_b_path=args.doc_b,
        output_path=args.output,
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
    print(f"  V2 REVIEW COMPLETE")
    print(f"{'='*60}")
    print(f"  Doc A paragraphs:  {len(result.doc_a.paragraphs)}")
    print(f"  Doc B paragraphs:  {len(result.doc_b.paragraphs)}")
    print(f"  Paragraph pairs:   {len(result.para_mapping)}")
    print(f"  Table diffs:       {len(result.table_diffs)}")
    print(f"  Agent 1 decisions: {len(result.agent1_decisions)}")
    print(f"  Agent 2 decisions: {len(result.agent2_decisions)}")
    print(f"  Final decisions:   {len(result.final_decisions)}")

    if result.validation:
        if result.validation.passed:
            print(f"  Validation:        PASSED")
        else:
            print(f"  Validation:        FAILED ({len(result.validation.errors)} errors)")
            for err in result.validation.errors[:5]:
                print(f"    - {err}")
        if result.validation.warnings:
            print(f"  Warnings:          {len(result.validation.warnings)}")

    print(f"  Output:            {result.output_path}")
    print(f"\n  Open in Word → Review → All Markup")


if __name__ == "__main__":
    main()
