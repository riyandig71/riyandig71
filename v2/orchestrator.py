"""
V2 Orchestrator
===============
Coordinates the full review pipeline:
  1. Read documents
  2. Map paragraphs and tables
  3. Batch paragraphs for agent processing
  4. Run Agent 1 → validate → run Agent 2 → validate
  5. Apply redline to DOCX

V1 problems fixed:
- V1 Orchestrator was a God Object (config + orchestration + output).
- V1 sent raw page text without paragraph alignment.
- V1 had no batching — just per-page processing.
- V2 sends mapped paragraph pairs in small batches.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic

from .doc_reader import (
    DocumentContent,
    Paragraph,
    read_document,
    map_paragraphs,
    map_tables,
    detect_total_pages,
    parse_page_param,
)
from .table_engine import compare_tables, table_diffs_for_agent, table_diffs_to_decisions, CellDiff
from .review_engine import (
    Action,
    ReviewDecision,
    run_agent1,
    run_agent2,
)
from .validation import validate_decisions, ValidationResult
from .redline_engine import apply_redline, AUTHOR_AGENT1, AUTHOR_AGENT2
from .prompt_store import MODEL_STRONG, MODEL_CHEAP, PRICING, load_prompts

logger = logging.getLogger("v2.orchestrator")

# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------

@dataclass
class CostEstimate:
    """Cost breakdown shown before execution."""
    total_paragraphs_a: int = 0
    total_paragraphs_b: int = 0
    total_sentences: int = 0
    total_words: int = 0
    total_tables_a: int = 0
    total_tables_b: int = 0
    total_table_cells: int = 0
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    agent1_model: str = ""
    agent2_model: str = ""
    agent1_cost_usd: float = 0.0
    agent2_cost_usd: float = 0.0
    total_cost_usd: float = 0.0

    def display(self) -> str:
        lines = [
            "",
            "=" * 60,
            "  COST ESTIMATE — BEFORE EXECUTION",
            "=" * 60,
            "",
            "  DOCUMENT ANALYSIS",
            f"    Doc A paragraphs:    {self.total_paragraphs_a}",
            f"    Doc B paragraphs:    {self.total_paragraphs_b}",
            f"    Total sentences:     {self.total_sentences}",
            f"    Total words:         {self.total_words:,}",
            f"    Doc A tables:        {self.total_tables_a}",
            f"    Doc B tables:        {self.total_tables_b}",
            f"    Table cells to check:{self.total_table_cells}",
            "",
            "  TOKEN ESTIMATES",
            f"    Input tokens:        ~{self.estimated_input_tokens:,}",
            f"    Output tokens:       ~{self.estimated_output_tokens:,}",
            "",
            "  MODEL & PRICING",
            f"    Agent 1:             {self.agent1_model}",
            f"    Agent 2:             {self.agent2_model}",
            "",
            "  COST BREAKDOWN",
            f"    Agent 1:             ${self.agent1_cost_usd:.4f}",
            f"    Agent 2:             ${self.agent2_cost_usd:.4f}",
            "    " + "-" * 30,
            f"    ESTIMATED TOTAL:     ${self.total_cost_usd:.4f}",
            "",
            "=" * 60,
        ]
        return "\n".join(lines)


def estimate_cost(
    doc_a: DocumentContent,
    doc_b: DocumentContent,
    agent1_model: str,
    agent2_model: str,
) -> CostEstimate:
    """Compute cost estimate from document content analysis."""
    est = CostEstimate(agent1_model=agent1_model, agent2_model=agent2_model)

    est.total_paragraphs_a = len(doc_a.paragraphs)
    est.total_paragraphs_b = len(doc_b.paragraphs)
    est.total_sentences = sum(len(p.sentences) for p in doc_a.paragraphs)
    est.total_words = sum(len(p.text.split()) for p in doc_a.paragraphs)
    est.total_tables_a = len(doc_a.tables)
    est.total_tables_b = len(doc_b.tables)
    est.total_table_cells = sum(t.rows * t.cols for t in doc_a.tables)

    # Token estimation: ~4 chars per token
    total_chars_a = sum(len(p.text) for p in doc_a.paragraphs)
    total_chars_b = sum(len(p.text) for p in doc_b.paragraphs)
    prompt_overhead = 2000  # system prompt tokens

    # Agent 1: system + doc_a + doc_b paragraphs
    a1_input = prompt_overhead + (total_chars_a + total_chars_b) // 4
    a1_output = est.total_sentences * 50  # ~50 tokens per decision entry

    # Agent 2: system + doc_a + doc_b + agent1 output
    a2_input = prompt_overhead + (total_chars_a + total_chars_b) // 4 + a1_output
    a2_output = a1_output  # similar size

    est.estimated_input_tokens = a1_input + a2_input
    est.estimated_output_tokens = a1_output + a2_output

    a1_pricing = PRICING.get(agent1_model, PRICING[MODEL_STRONG])
    a2_pricing = PRICING.get(agent2_model, PRICING[MODEL_STRONG])

    est.agent1_cost_usd = (
        (a1_input / 1_000_000) * a1_pricing["input"]
        + (a1_output / 1_000_000) * a1_pricing["output"]
    )
    est.agent2_cost_usd = (
        (a2_input / 1_000_000) * a2_pricing["input"]
        + (a2_output / 1_000_000) * a2_pricing["output"]
    )
    est.total_cost_usd = est.agent1_cost_usd + est.agent2_cost_usd
    return est


# ---------------------------------------------------------------------------
# Paragraph serialization for agent messages
# ---------------------------------------------------------------------------

def _serialize_para(para: Paragraph) -> dict[str, Any]:
    """Convert a Paragraph to a dict for agent messages."""
    return {
        "para_idx": para.idx,
        "text": para.text,
        "sentences": [{"idx": s.idx, "text": s.text} for s in para.sentences],
        "is_heading": para.is_heading,
    }


def _empty_para_dict(para_idx: int) -> dict[str, Any]:
    """Placeholder for when Doc B has no matching paragraph."""
    return {"para_idx": para_idx, "text": "", "sentences": [], "is_heading": False}


# ---------------------------------------------------------------------------
# Batch builder
# ---------------------------------------------------------------------------

def _build_batches(
    doc_a: DocumentContent,
    doc_b: DocumentContent,
    para_mapping: list[tuple[int, int | None]],
    batch_size: int = 10,
) -> list[tuple[list[dict], list[dict]]]:
    """Split mapped paragraphs into batches for agent processing.

    Each batch is a tuple of (paragraphs_a_dicts, paragraphs_b_dicts).
    """
    b_para_map: dict[int, Paragraph] = {p.idx: p for p in doc_b.paragraphs}

    all_pairs_a: list[dict] = []
    all_pairs_b: list[dict] = []

    for a_idx, b_idx in para_mapping:
        a_para = next((p for p in doc_a.paragraphs if p.idx == a_idx), None)
        if a_para is None:
            continue
        all_pairs_a.append(_serialize_para(a_para))
        if b_idx is not None and b_idx in b_para_map:
            all_pairs_b.append(_serialize_para(b_para_map[b_idx]))
        else:
            all_pairs_b.append(_empty_para_dict(a_idx))

    batches: list[tuple[list[dict], list[dict]]] = []
    for i in range(0, len(all_pairs_a), batch_size):
        batches.append((
            all_pairs_a[i:i + batch_size],
            all_pairs_b[i:i + batch_size],
        ))
    return batches


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

@dataclass
class ReviewResult:
    """Full result of a review run."""
    doc_a: DocumentContent
    doc_b: DocumentContent
    para_mapping: list[tuple[int, int | None]]
    table_diffs: list[CellDiff]
    agent1_decisions: list[ReviewDecision] = field(default_factory=list)
    agent2_decisions: list[ReviewDecision] = field(default_factory=list)
    final_decisions: list[ReviewDecision] = field(default_factory=list)
    validation: ValidationResult | None = None
    output_path: Path | None = None


class Orchestrator:
    """V2 pipeline coordinator."""

    def __init__(
        self,
        doc_a_path: str | Path,
        doc_b_path: str | Path,
        *,
        output_path: str | Path = "review_output.docx",
        agent1_model: str = MODEL_STRONG,
        agent2_model: str = MODEL_STRONG,
        batch_size: int = 10,
        max_retries: int = 2,
        api_key: str | None = None,
    ) -> None:
        self.doc_a_path = Path(doc_a_path)
        self.doc_b_path = Path(doc_b_path)
        self.output_path = Path(output_path)
        self.agent1_model = agent1_model
        self.agent2_model = agent2_model
        self.batch_size = batch_size
        self.max_retries = max_retries
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def run(self, *, skip_confirmation: bool = False) -> ReviewResult | None:
        """Execute the full V2 review pipeline."""

        # Step 1: Read documents
        logger.info("Reading documents...")
        doc_a = read_document(self.doc_a_path)
        doc_b = read_document(self.doc_b_path)
        logger.info("Doc A: %d paragraphs, %d tables", len(doc_a.paragraphs), len(doc_a.tables))
        logger.info("Doc B: %d paragraphs, %d tables", len(doc_b.paragraphs), len(doc_b.tables))

        # Step 2: Cost estimate + confirmation
        est = estimate_cost(doc_a, doc_b, self.agent1_model, self.agent2_model)
        if not skip_confirmation:
            print(est.display())
            while True:
                answer = input("\n  Proceed with execution? (y/n): ").strip().lower()
                if answer in ("y", "yes"):
                    break
                if answer in ("n", "no"):
                    print("  Execution cancelled.")
                    return None
                print("  Please enter 'y' or 'n'.")

        # Step 3: Map paragraphs and tables
        logger.info("Mapping paragraphs...")
        para_mapping = map_paragraphs(doc_a, doc_b)
        logger.info("Mapped %d paragraph pairs", len(para_mapping))

        table_mapping = map_tables(doc_a, doc_b)
        all_table_diffs: list[CellDiff] = []
        for a_idx, b_idx in table_mapping:
            if b_idx is not None:
                a_tbl = next(t for t in doc_a.tables if t.idx == a_idx)
                b_tbl = next(t for t in doc_b.tables if t.idx == b_idx)
                all_table_diffs.extend(compare_tables(a_tbl, b_tbl))

        result = ReviewResult(
            doc_a=doc_a,
            doc_b=doc_b,
            para_mapping=para_mapping,
            table_diffs=all_table_diffs,
        )

        # Step 4: Build batches and run Agent 1
        batches = _build_batches(doc_a, doc_b, para_mapping, self.batch_size)
        table_diff_dicts = table_diffs_for_agent(all_table_diffs) if all_table_diffs else None

        all_a1_decisions: list[ReviewDecision] = []
        for batch_idx, (batch_a, batch_b) in enumerate(batches):
            logger.info("Agent 1: batch %d/%d (%d paragraphs)",
                        batch_idx + 1, len(batches), len(batch_a))
            # Only include table diffs in first batch
            td = table_diff_dicts if batch_idx == 0 else None
            decisions = run_agent1(
                self.client, batch_a, batch_b, td, model=self.agent1_model,
            )
            all_a1_decisions.extend(decisions)

        result.agent1_decisions = all_a1_decisions
        logger.info("Agent 1 total: %d decisions", len(all_a1_decisions))

        # Step 5: Validate Agent 1 output
        a1_validation = validate_decisions(all_a1_decisions, doc_a, auto_fix=True)
        if a1_validation.fixed_decisions:
            all_a1_decisions = a1_validation.fixed_decisions

        # Step 6: Run Agent 2 on each batch
        all_a2_decisions: list[ReviewDecision] = []
        # Re-batch Agent 1 decisions by batch alignment
        a1_by_para: dict[int, list[ReviewDecision]] = {}
        for d in all_a1_decisions:
            a1_by_para.setdefault(d.para_idx, []).append(d)

        for batch_idx, (batch_a, batch_b) in enumerate(batches):
            # Gather Agent 1 decisions for this batch's paragraphs
            batch_a1: list[ReviewDecision] = []
            for a_dict in batch_a:
                pidx = a_dict["para_idx"]
                batch_a1.extend(a1_by_para.get(pidx, []))

            if not batch_a1:
                continue

            logger.info("Agent 2: batch %d/%d (%d decisions to validate)",
                        batch_idx + 1, len(batches), len(batch_a1))
            decisions = run_agent2(
                self.client, batch_a, batch_b, batch_a1, model=self.agent2_model,
            )
            all_a2_decisions.extend(decisions)

        result.agent2_decisions = all_a2_decisions
        logger.info("Agent 2 total: %d decisions", len(all_a2_decisions))

        # Step 7: Final validation
        final_decisions = all_a2_decisions if all_a2_decisions else all_a1_decisions
        final_validation = validate_decisions(final_decisions, doc_a, auto_fix=True)
        result.validation = final_validation

        if final_validation.fixed_decisions:
            result.final_decisions = final_validation.fixed_decisions
        else:
            result.final_decisions = final_decisions

        # Add table decisions
        if all_table_diffs:
            table_decisions = table_diffs_to_decisions(all_table_diffs)
            result.final_decisions.extend(table_decisions)

        # Step 8: Apply redline
        logger.info("Applying %d decisions to DOCX...", len(result.final_decisions))
        result.output_path = apply_redline(
            self.doc_a_path,
            doc_a,
            result.final_decisions,
            self.output_path,
        )

        # Summary
        keeps = sum(1 for d in result.final_decisions if d.action == Action.KEEP)
        replaces = sum(1 for d in result.final_decisions if d.action == Action.REPLACE)
        inserts = sum(1 for d in result.final_decisions if d.action == Action.INSERT)
        deletes = sum(1 for d in result.final_decisions if d.action == Action.DELETE)
        uncertain = sum(1 for d in result.final_decisions if d.action == Action.UNCERTAIN)
        logger.info(
            "Review complete: KEEP=%d REPLACE=%d INSERT=%d DELETE=%d UNCERTAIN=%d",
            keeps, replaces, inserts, deletes, uncertain,
        )

        return result
