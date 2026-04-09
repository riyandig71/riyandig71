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
    max_tokens: int = 16384,
    max_retries: int = 5,
) -> str:
    """Send a request to the Claude API with retry on rate-limit errors.

    Retries up to *max_retries* times with exponential backoff (2s, 4s, 8s, 16s, 32s)
    on 429 rate-limit errors.
    """
    import time as _time

    logger.debug("Calling model=%s, system_len=%d, user_len=%d",
                 model, len(system), len(user_message))

    for attempt in range(1, max_retries + 1):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_message}],
            )
            return resp.content[0].text
        except anthropic.RateLimitError:
            if attempt == max_retries:
                raise
            wait = 2 ** attempt
            logger.warning("Rate limited (attempt %d/%d). Waiting %ds...",
                           attempt, max_retries, wait)
            _time.sleep(wait)
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            status = getattr(exc, "status_code", 0)
            is_retryable = status >= 500 or status == 529 or isinstance(exc, anthropic.APIConnectionError)
            if is_retryable and attempt < max_retries:
                wait = min(2 ** attempt, 60)  # cap at 60s
                logger.warning("Error %s (attempt %d/%d). Waiting %ds...",
                               status or type(exc).__name__, attempt, max_retries, wait)
                _time.sleep(wait)
            else:
                raise
    return ""  # unreachable, but satisfies type checker


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _extract_json_array(text: str) -> list[dict[str, Any]]:
    """Extract a JSON array from model output.

    Strategies:
    1. Strip ```json fences, then parse as JSON.
    2. Find the LARGEST complete [ ... ] bracket pair.
    3. Repair truncated JSON (model hit max_tokens mid-output).
    """
    raw = text.strip()

    # --- Pre-process: strip ```json fences ---
    cleaned = raw
    if cleaned.startswith("```"):
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'\s*```\s*$', '', cleaned)
    cleaned = cleaned.strip()

    # --- Strategy 1: parse cleaned text directly ---
    try:
        result = json.loads(cleaned)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # --- Strategy 2: extract complete JSON objects, string-aware ---
    # This properly handles [ ] and { } inside string values like
    # "See Article [5] of the Act" which broke naive bracket counting.
    start = cleaned.find("[")
    if start >= 0:
        fragment = cleaned[start + 1:]  # skip the opening [

        objects: list[str] = []
        depth = 0
        in_string = False
        escape_next = False
        obj_start: int | None = None

        for i, ch in enumerate(fragment):
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_string:
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            # Outside strings:
            if ch == "{":
                if depth == 0:
                    obj_start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and obj_start is not None:
                    objects.append(fragment[obj_start:i + 1])
                    obj_start = None
            elif ch == "]" and depth == 0:
                break  # end of array

        if objects:
            array_str = "[" + ",".join(objects) + "]"
            try:
                result = json.loads(array_str)
                if isinstance(result, list) and len(result) > 0:
                    if depth > 0 or obj_start is not None:
                        logger.warning(
                            "Truncated JSON recovered (%d complete entries, "
                            "last entry was incomplete and dropped).",
                            len(result),
                        )
                    return result
            except json.JSONDecodeError:
                # If reassembly fails, try each object individually
                valid_objects: list[str] = []
                for obj in objects:
                    try:
                        json.loads(obj)
                        valid_objects.append(obj)
                    except json.JSONDecodeError:
                        continue
                if valid_objects:
                    array_str = "[" + ",".join(valid_objects) + "]"
                    try:
                        result = json.loads(array_str)
                        if isinstance(result, list):
                            logger.warning(
                                "Truncated JSON recovered (%d/%d objects valid).",
                                len(valid_objects), len(objects),
                            )
                            return result
                    except json.JSONDecodeError:
                        pass

    logger.error("Failed to extract JSON from response (len=%d). "
                 "First 500 chars: %s", len(raw), raw[:500])
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
