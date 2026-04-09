"""
V2 Review Engine
================
Calls Claude agents and parses structured decision responses.

V1 problems fixed:
- V1 asked model for free-form rewritten text → blob-in/blob-out corruption.
- V1 used regex to extract JSON from mixed prose+JSON → fragile parsing.
- V2 demands JSON-only output and validates against a schema.
- V2 decisions are structured (KEEP/REPLACE/INSERT/DELETE/UNCERTAIN).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import anthropic

from .prompt_store import AGENT1_PROMPT, AGENT2_PROMPT, MODEL_STRONG

logger = logging.getLogger("v2.review_engine")


# ---------------------------------------------------------------------------
# Action enum
# ---------------------------------------------------------------------------

class Action(str, Enum):
    KEEP = "KEEP"
    REPLACE = "REPLACE"
    INSERT = "INSERT"
    DELETE = "DELETE"
    UNCERTAIN = "UNCERTAIN"


# ---------------------------------------------------------------------------
# Decision dataclass
# ---------------------------------------------------------------------------

@dataclass
class ReviewDecision:
    """One structured decision from an agent."""
    para_idx: int
    sent_idx: int | None       # None for paragraph-level or table-cell decisions
    element_type: str          # "sentence" | "table_cell"
    action: Action
    original: str
    revised: str               # empty for KEEP, UNCERTAIN, DELETE
    reason: str
    # Agent 2 fields (populated only in validation pass)
    validation: str = ""       # "approved" | "corrected" | "missing_fixed"
    agent1_action: str = ""
    correction_note: str = ""

    @property
    def is_change(self) -> bool:
        return self.action in (Action.REPLACE, Action.INSERT, Action.DELETE)


# ---------------------------------------------------------------------------
# Claude API caller
# ---------------------------------------------------------------------------

def call_claude(
    client: anthropic.Anthropic,
    *,
    system: str,
    user_message: str,
    model: str = MODEL_STRONG,
    max_tokens: int = 4096,
) -> str:
    """Send a request to the Claude API.  Returns raw text response."""
    logger.debug("Calling model=%s, system_len=%d, user_len=%d",
                 model, len(system), len(user_message))
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return resp.content[0].text


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _extract_json_array(text: str) -> list[dict[str, Any]]:
    """Extract a JSON array from model output.

    Tries multiple strategies:
    1. Parse entire text as JSON.
    2. Find outermost [ ... ] bracket pair.
    3. Find JSON inside ```json ... ``` fences.
    """
    text = text.strip()

    # Strategy 1: whole text is JSON
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # Strategy 2: find ```json ... ``` block
    fence_match = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    # Strategy 3: find outermost [ ... ]
    start = text.find("[")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break

    logger.error("Failed to extract JSON array from response (len=%d)", len(text))
    return []


def parse_decisions(raw_json: list[dict[str, Any]]) -> list[ReviewDecision]:
    """Convert raw JSON dicts into validated ReviewDecision objects."""
    decisions: list[ReviewDecision] = []
    for entry in raw_json:
        try:
            action_str = entry.get("action", "KEEP").upper()
            action = Action(action_str) if action_str in Action.__members__ else Action.UNCERTAIN

            decisions.append(ReviewDecision(
                para_idx=int(entry.get("para_idx", 0)),
                sent_idx=entry.get("sent_idx"),
                element_type=entry.get("element_type", "sentence"),
                action=action,
                original=entry.get("original", ""),
                revised=entry.get("revised", ""),
                reason=entry.get("reason", ""),
                validation=entry.get("validation", ""),
                agent1_action=entry.get("agent1_action", ""),
                correction_note=entry.get("correction_note", ""),
            ))
        except (ValueError, TypeError) as exc:
            logger.warning("Skipping malformed decision entry: %s (%s)", entry, exc)
    return decisions


# ---------------------------------------------------------------------------
# Agent message builders
# ---------------------------------------------------------------------------

def build_agent1_message(
    paragraphs_a: list[dict[str, Any]],
    paragraphs_b: list[dict[str, Any]],
    table_diffs: list[dict[str, Any]] | None = None,
) -> str:
    """Build the user message for Agent 1.

    Sends mapped paragraph pairs, not raw page text.
    This is the key V2 improvement: the model sees aligned content.
    """
    parts: list[str] = []
    parts.append("Compare each Document A paragraph against its Document B counterpart.")
    parts.append("Return ONLY a JSON array of decisions.\n")

    for i, (a, b) in enumerate(zip(paragraphs_a, paragraphs_b)):
        parts.append(f"--- Paragraph {i} ---")
        parts.append(f"Document A [para_idx={a['para_idx']}]:")
        parts.append(a["text"])
        if b:
            parts.append(f"\nDocument B counterpart:")
            parts.append(b["text"])
        else:
            parts.append("\nDocument B counterpart: [no match found]")
        parts.append(f"\nSentences in Document A:")
        for s in a.get("sentences", []):
            parts.append(f"  [{s['idx']}] {s['text']}")
        parts.append("")

    if table_diffs:
        parts.append("--- Table Data ---")
        parts.append(json.dumps(table_diffs, indent=2))

    return "\n".join(parts)


def build_agent2_message(
    paragraphs_a: list[dict[str, Any]],
    paragraphs_b: list[dict[str, Any]],
    agent1_decisions: list[dict[str, Any]],
) -> str:
    """Build the user message for Agent 2.

    Sends original paragraphs + Agent 1 decisions for validation.
    """
    parts: list[str] = []
    parts.append("Validate each Agent 1 decision against the original documents.")
    parts.append("Return ONLY a JSON array of validated decisions.\n")

    for i, (a, b) in enumerate(zip(paragraphs_a, paragraphs_b)):
        parts.append(f"--- Paragraph {i} ---")
        parts.append(f"Document A [para_idx={a['para_idx']}]:")
        parts.append(a["text"])
        if b:
            parts.append(f"\nDocument B counterpart:")
            parts.append(b["text"])
        else:
            parts.append("\nDocument B counterpart: [no match found]")
        parts.append("")

    parts.append("--- Agent 1 Decisions ---")
    parts.append(json.dumps(agent1_decisions, indent=2))

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Agent runners
# ---------------------------------------------------------------------------

def run_agent1(
    client: anthropic.Anthropic,
    paragraphs_a: list[dict[str, Any]],
    paragraphs_b: list[dict[str, Any]],
    table_diffs: list[dict[str, Any]] | None = None,
    *,
    model: str = MODEL_STRONG,
) -> list[ReviewDecision]:
    """Run Agent 1: produce structured decisions for each paragraph/sentence."""
    user_msg = build_agent1_message(paragraphs_a, paragraphs_b, table_diffs)
    logger.info("Agent 1: reviewing %d paragraph pairs (model=%s)",
                len(paragraphs_a), model)

    raw = call_claude(client, system=AGENT1_PROMPT, user_message=user_msg, model=model)
    raw_json = _extract_json_array(raw)
    decisions = parse_decisions(raw_json)

    logger.info("Agent 1: returned %d decisions", len(decisions))
    return decisions


def run_agent2(
    client: anthropic.Anthropic,
    paragraphs_a: list[dict[str, Any]],
    paragraphs_b: list[dict[str, Any]],
    agent1_decisions: list[ReviewDecision],
    *,
    model: str = MODEL_STRONG,
) -> list[ReviewDecision]:
    """Run Agent 2: validate and correct Agent 1 decisions."""
    a1_dicts = [
        {
            "para_idx": d.para_idx,
            "sent_idx": d.sent_idx,
            "element_type": d.element_type,
            "action": d.action.value,
            "original": d.original,
            "revised": d.revised,
            "reason": d.reason,
        }
        for d in agent1_decisions
    ]
    user_msg = build_agent2_message(paragraphs_a, paragraphs_b, a1_dicts)
    logger.info("Agent 2: validating %d decisions (model=%s)",
                len(agent1_decisions), model)

    raw = call_claude(client, system=AGENT2_PROMPT, user_message=user_msg, model=model)
    raw_json = _extract_json_array(raw)
    decisions = parse_decisions(raw_json)

    logger.info("Agent 2: returned %d validated decisions", len(decisions))
    return decisions
