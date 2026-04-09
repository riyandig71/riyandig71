"""
V3 Orchestrator
===============
Extends V2 orchestrator with multi-part document support.

Key change: Doc A can be a folder with Part 1, Part 2, etc.
Each part is reviewed independently against Doc B.
Output is one redline DOCX per part.
"""

from __future__ import annotations

import logging
import time as _time
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
)
from .doc_resolver import (
    DocPart,
    ResolvedInput,
    resolve_input,
    build_output_paths,
)
from .table_engine import compare_tables, table_diffs_for_agent, table_diffs_to_decisions, CellDiff
from .review_engine import (
    Action,
    ReviewDecision,
    run_agent1,
    run_agent2,
    parse_decisions,
)
from .validation import validate_decisions, ValidationResult
from .redline_engine import apply_redline, AUTHOR_AGENT2
from .checkpoint import Checkpoint
from .prompt_store import MODEL_STRONG, MODEL_CHEAP, PRICING, load_prompts

logger = logging.getLogger("v3.orchestrator")


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------

@dataclass
class CostEstimate:
    """Cost breakdown shown before execution."""
    total_parts: int = 0
    total_paragraphs_a: int = 0
    total_paragraphs_b: int = 0
    total_sentences: int = 0
    total_words: int = 0
    total_tables_a: int = 0
    total_tables_b: int = 0
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    agent1_model: str = ""
    agent2_model: str = ""
    agent1_cost_usd: float = 0.0
    agent2_cost_usd: float = 0.0
    total_cost_usd: float = 0.0
    parts_detail: list[dict[str, Any]] = field(default_factory=list)

    def display(self) -> str:
        lines = [
            "",
            "=" * 60,
            "  COST ESTIMATE — BEFORE EXECUTION",
            "=" * 60,
            "",
            f"  DOCUMENT A: {self.total_parts} part(s)",
        ]
        for pd in self.parts_detail:
            lines.append(f"    {pd['label']}: {pd['paragraphs']} paragraphs, "
                         f"{pd['sentences']} sentences, {pd['tables']} tables")
        lines.extend([
            f"\n  DOCUMENT B: {self.total_paragraphs_b} paragraphs, {self.total_tables_b} tables",
            "",
            "  TOTALS",
            f"    Doc A paragraphs:    {self.total_paragraphs_a}",
            f"    Total sentences:     {self.total_sentences}",
            f"    Total words:         {self.total_words:,}",
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
        ])
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-part result
# ---------------------------------------------------------------------------

@dataclass
class PartResult:
    """Result for one part of Doc A."""
    part: DocPart
    doc_a: DocumentContent | None = None
    agent1_decisions: list[ReviewDecision] = field(default_factory=list)
    agent2_decisions: list[ReviewDecision] = field(default_factory=list)
    final_decisions: list[ReviewDecision] = field(default_factory=list)
    validation: ValidationResult | None = None
    output_path: Path | None = None
    success: bool = False
    error: str = ""


@dataclass
class ReviewResult:
    """Full result of a multi-part review run."""
    resolved: ResolvedInput
    doc_b: DocumentContent | None = None
    part_results: list[PartResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serialize_para(para: Paragraph) -> dict[str, Any]:
    return {
        "para_idx": para.idx,
        "text": para.text,
        "sentences": [{"idx": s.idx, "text": s.text} for s in para.sentences],
        "is_heading": para.is_heading,
    }


def _empty_para_dict(para_idx: int) -> dict[str, Any]:
    return {"para_idx": para_idx, "text": "", "sentences": [], "is_heading": False}


def _build_batches(
    doc_a: DocumentContent,
    doc_b: DocumentContent,
    para_mapping: list[tuple[int, int | None]],
    batch_size: int = 30,
) -> list[tuple[list[dict], list[dict]]]:
    b_map = {p.idx: p for p in doc_b.paragraphs}
    all_a: list[dict] = []
    all_b: list[dict] = []

    for a_idx, b_idx in para_mapping:
        a_para = next((p for p in doc_a.paragraphs if p.idx == a_idx), None)
        if a_para is None:
            continue
        all_a.append(_serialize_para(a_para))
        if b_idx is not None and b_idx in b_map:
            all_b.append(_serialize_para(b_map[b_idx]))
        else:
            all_b.append(_empty_para_dict(a_idx))

    batches = []
    for i in range(0, len(all_a), batch_size):
        batches.append((all_a[i:i + batch_size], all_b[i:i + batch_size]))
    return batches


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class Orchestrator:
    """V3 pipeline coordinator with multi-part support."""

    def __init__(
        self,
        doc_a_path: str | Path,
        doc_b_path: str | Path,
        *,
        output_dir: str | Path = "output",
        agent1_model: str = MODEL_STRONG,
        agent2_model: str = MODEL_STRONG,
        batch_size: int = 30,
        max_retries: int = 2,
        api_key: str | None = None,
    ) -> None:
        self.doc_a_path = Path(doc_a_path)
        self.doc_b_path = Path(doc_b_path)
        self.output_dir = Path(output_dir)
        self.agent1_model = agent1_model
        self.agent2_model = agent2_model
        self.batch_size = batch_size
        self.max_retries = max_retries
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def run(self, *, skip_confirmation: bool = False) -> ReviewResult | None:
        """Execute the full V3 pipeline."""

        # Step 1: Resolve input (single file or folder with parts)
        logger.info("Resolving input...")
        resolved = resolve_input(self.doc_a_path, self.doc_b_path)
        logger.info("Doc A: %s (%d parts), Doc B: %s",
                     resolved.doc_a_label, len(resolved.doc_a_parts),
                     resolved.doc_b_path.name)

        # Step 2: Read Doc B once (shared across all parts)
        logger.info("Reading Document B...")
        doc_b = read_document(resolved.doc_b_path)
        logger.info("Doc B: %d paragraphs, %d tables",
                     len(doc_b.paragraphs), len(doc_b.tables))

        # Step 3: Cost estimate
        est = self._estimate_cost(resolved, doc_b)
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

        # Step 4: Build output paths
        output_mapping = build_output_paths(resolved, self.output_dir)

        # Step 5: Process each part
        result = ReviewResult(resolved=resolved, doc_b=doc_b)

        for part_idx, (part, out_path) in enumerate(output_mapping):
            print(f"\n{'='*60}")
            print(f"  PART {part.part_number}/{len(resolved.doc_a_parts)}: {part.label}")
            print(f"  Pages: {part.page_range}")
            print(f"{'='*60}")

            pr = self._process_part(part, doc_b, out_path)
            result.part_results.append(pr)

            if pr.success:
                logger.info("Part %d complete → %s", part.part_number, pr.output_path)
            else:
                logger.error("Part %d failed: %s", part.part_number, pr.error)

        # Step 6: Merge all parts into single output DOCX
        if resolved.doc_a_is_multipart:
            merged_path = self.output_dir / f"{resolved.doc_a_label}_redline_MERGED.docx"
            successful_parts = [pr for pr in result.part_results if pr.success and pr.output_path]
            if len(successful_parts) > 1:
                _merge_redline_parts(successful_parts, merged_path)
                logger.info("Merged output: %s", merged_path)
            elif len(successful_parts) == 1:
                # Only one part — just copy it as the merged output
                import shutil
                shutil.copy2(str(successful_parts[0].output_path), str(merged_path))
                logger.info("Single part copied as merged: %s", merged_path)

        return result

    def _estimate_cost(
        self,
        resolved: ResolvedInput,
        doc_b: DocumentContent,
    ) -> CostEstimate:
        est = CostEstimate(
            total_parts=len(resolved.doc_a_parts),
            total_paragraphs_b=len(doc_b.paragraphs),
            total_tables_b=len(doc_b.tables),
            agent1_model=self.agent1_model,
            agent2_model=self.agent2_model,
        )

        for part in resolved.doc_a_parts:
            try:
                doc_a = read_document(part.path)
                n_para = len(doc_a.paragraphs)
                n_sent = sum(len(p.sentences) for p in doc_a.paragraphs)
                n_words = sum(len(p.text.split()) for p in doc_a.paragraphs)
                n_tables = len(doc_a.tables)

                est.total_paragraphs_a += n_para
                est.total_sentences += n_sent
                est.total_words += n_words
                est.total_tables_a += n_tables
                est.parts_detail.append({
                    "label": part.label,
                    "paragraphs": n_para,
                    "sentences": n_sent,
                    "tables": n_tables,
                })
            except Exception as exc:
                est.parts_detail.append({
                    "label": part.label,
                    "paragraphs": 0, "sentences": 0, "tables": 0,
                    "error": str(exc),
                })

        # Token estimate
        prompt_overhead = 2000
        chars_b = sum(len(p.text) for p in doc_b.paragraphs)

        # Per part: system + doc_a_part + doc_b context
        # Agent 1 sees mapped paragraphs; Agent 2 sees same + Agent 1 output
        total_a_chars = est.total_words * 5  # rough chars from words
        a1_input = (prompt_overhead + (total_a_chars + chars_b) // 4) * est.total_parts
        a1_output = est.total_sentences * 50
        a2_input = a1_input + a1_output
        a2_output = a1_output

        est.estimated_input_tokens = a1_input + a2_input
        est.estimated_output_tokens = a1_output + a2_output

        a1p = PRICING.get(self.agent1_model, PRICING[MODEL_STRONG])
        a2p = PRICING.get(self.agent2_model, PRICING[MODEL_STRONG])

        est.agent1_cost_usd = (a1_input / 1e6) * a1p["input"] + (a1_output / 1e6) * a1p["output"]
        est.agent2_cost_usd = (a2_input / 1e6) * a2p["input"] + (a2_output / 1e6) * a2p["output"]
        est.total_cost_usd = est.agent1_cost_usd + est.agent2_cost_usd
        return est

    def _process_part(
        self,
        part: DocPart,
        doc_b: DocumentContent,
        output_path: Path,
    ) -> PartResult:
        """Process a single part of Doc A against Doc B, with checkpoint/resume."""
        pr = PartResult(part=part)

        # Checkpoint per part
        ckpt_name = f"{part.label}".replace(" ", "_").replace("(", "").replace(")", "")
        ckpt_path = self.output_dir / f".checkpoint_{ckpt_name}.json"
        ckpt = Checkpoint(ckpt_path)

        if ckpt.is_complete():
            logger.info("  Part already completed (checkpoint). Skipping to redline.")
            ckpt.delete()

        try:
            logger.info("Reading part: %s", part.path.name)
            doc_a = read_document(part.path)
            pr.doc_a = doc_a
            logger.info("  %d paragraphs, %d tables, %d sentences",
                        len(doc_a.paragraphs), len(doc_a.tables),
                        sum(len(p.sentences) for p in doc_a.paragraphs))

            para_mapping = map_paragraphs(doc_a, doc_b)
            matched = sum(1 for _, b in para_mapping if b is not None)
            logger.info("  Mapped %d paragraphs (%d matched)", len(para_mapping), matched)

            table_mapping = map_tables(doc_a, doc_b)
            all_table_diffs: list[CellDiff] = []
            for a_idx, b_idx in table_mapping:
                if b_idx is not None:
                    a_tbl = next((t for t in doc_a.tables if t.idx == a_idx), None)
                    b_tbl = next((t for t in doc_b.tables if t.idx == b_idx), None)
                    if a_tbl and b_tbl:
                        all_table_diffs.extend(compare_tables(a_tbl, b_tbl))

            batches = _build_batches(doc_a, doc_b, para_mapping, self.batch_size)
            table_diff_dicts = None
            if all_table_diffs:
                table_diff_dicts = table_diffs_for_agent(all_table_diffs[:50])

            # --- Agent 1 with checkpoint ---
            all_a1: list[ReviewDecision] = []
            if ckpt.is_agent1_done():
                all_a1 = parse_decisions(ckpt.get_agent1_decisions())
                logger.info("  RESUMING: Agent 1 done — loaded %d decisions", len(all_a1))
            else:
                saved = ckpt.get_agent1_decisions()
                if saved:
                    all_a1 = parse_decisions(saved)
                    logger.info("  RESUMING: loaded %d Agent 1 decisions", len(all_a1))

                for bi, (ba, bb) in enumerate(batches):
                    if ckpt.is_agent1_batch_done(bi):
                        logger.info("  Agent 1: batch %d/%d — SKIPPED (checkpoint)", bi + 1, len(batches))
                        continue
                    logger.info("  Agent 1: batch %d/%d (%d paragraphs)", bi + 1, len(batches), len(ba))
                    td = table_diff_dicts if bi == 0 else None
                    decisions = run_agent1(self.client, ba, bb, td, model=self.agent1_model)
                    all_a1.extend(decisions)
                    dec_dicts = [
                        {"para_idx": d.para_idx, "sent_idx": d.sent_idx,
                         "element_type": d.element_type, "action": d.action.value,
                         "original": d.original, "revised": d.revised, "reason": d.reason}
                        for d in decisions
                    ]
                    ckpt.save_agent1_batch(bi, dec_dicts)
                    if bi < len(batches) - 1:
                        _time.sleep(3)
                ckpt.mark_agent1_done()

            pr.agent1_decisions = all_a1
            a1_val = validate_decisions(all_a1, doc_a, auto_fix=True)
            if a1_val.fixed_decisions:
                all_a1 = a1_val.fixed_decisions

            # --- Agent 2 with checkpoint ---
            a1_by_para: dict[int, list[ReviewDecision]] = {}
            for d in all_a1:
                a1_by_para.setdefault(d.para_idx, []).append(d)

            all_a2: list[ReviewDecision] = []
            saved_a2 = ckpt.get_agent2_decisions()
            if saved_a2:
                all_a2 = parse_decisions(saved_a2)
                logger.info("  RESUMING: loaded %d Agent 2 decisions", len(all_a2))

            for bi, (ba, bb) in enumerate(batches):
                if ckpt.is_agent2_batch_done(bi):
                    logger.info("  Agent 2: batch %d/%d — SKIPPED (checkpoint)", bi + 1, len(batches))
                    continue
                batch_a1 = []
                for a_dict in ba:
                    batch_a1.extend(a1_by_para.get(a_dict["para_idx"], []))
                if not batch_a1:
                    continue
                logger.info("  Agent 2: batch %d/%d (%d decisions)", bi + 1, len(batches), len(batch_a1))
                decisions = run_agent2(self.client, ba, bb, batch_a1, model=self.agent2_model)
                all_a2.extend(decisions)
                dec_dicts = [
                    {"para_idx": d.para_idx, "sent_idx": d.sent_idx,
                     "element_type": d.element_type, "action": d.action.value,
                     "original": d.original, "revised": d.revised, "reason": d.reason,
                     "validation": d.validation, "agent1_action": d.agent1_action,
                     "correction_note": d.correction_note}
                    for d in decisions
                ]
                ckpt.save_agent2_batch(bi, dec_dicts)
                if bi < len(batches) - 1:
                    _time.sleep(3)

            pr.agent2_decisions = all_a2

            # Final validation
            final = all_a2 if all_a2 else all_a1
            final_val = validate_decisions(final, doc_a, auto_fix=True)
            pr.validation = final_val
            if final_val.fixed_decisions:
                final = final_val.fixed_decisions
            if all_table_diffs:
                final.extend(table_diffs_to_decisions(all_table_diffs))
            pr.final_decisions = final

            # Apply redline to this part
            logger.info("  Applying %d decisions to DOCX...", len(final))
            pr.output_path = apply_redline(part.path, doc_a, final, output_path)
            pr.success = True

            ckpt.mark_complete()
            ckpt.delete()

            keeps = sum(1 for d in final if d.action == Action.KEEP)
            replaces = sum(1 for d in final if d.action == Action.REPLACE)
            logger.info("  Done: KEEP=%d, REPLACE=%d", keeps, replaces)

        except Exception as exc:
            pr.error = str(exc)
            logger.exception("  Part %d failed (checkpoint saved — resume possible): %s",
                             part.part_number, exc)

        return pr


# ---------------------------------------------------------------------------
# Merge multi-part redline outputs into single DOCX
# ---------------------------------------------------------------------------

def _merge_redline_parts(
    part_results: list[PartResult],
    output_path: Path,
) -> Path:
    """Merge multiple per-part redline DOCX files into one combined document.

    Takes the first part as base, then appends all subsequent parts
    with a page break between each. Tracked changes (w:del, w:ins)
    are preserved from each part.
    """
    from docx import Document as DocxDocument
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    import copy

    logger.info("Merging %d parts into single DOCX...", len(part_results))

    # Use first part as base document
    base_doc = DocxDocument(str(part_results[0].output_path))

    for pr in part_results[1:]:
        if not pr.output_path or not pr.output_path.exists():
            continue

        # Add page break before appending next part
        pb_para = base_doc.add_paragraph()
        run = pb_para.add_run()
        br = OxmlElement("w:br")
        br.set(qn("w:type"), "page")
        run._element.append(br)

        # Read the next part
        part_doc = DocxDocument(str(pr.output_path))

        # Copy all body elements (paragraphs, tables) from part into base
        for element in part_doc.element.body:
            tag = element.tag.split("}")[-1] if "}" in element.tag else element.tag
            if tag in ("p", "tbl"):
                base_doc.element.body.append(copy.deepcopy(element))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    base_doc.save(str(output_path))
    logger.info("Merged DOCX saved: %s", output_path)
    return output_path
