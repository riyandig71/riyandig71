"""
V2 Validation Layer
===================
Anti-corruption guards that run BEFORE writing the DOCX.

V1 had validation that only checked completeness (are all sentence IDs present?).
V2 adds:
- Append-after-original detection
- Duplication detection
- Suspicious REPLACE detection (original text inside revised text)
- Missing-entry detection
- Table diff sanity checks
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .review_engine import Action, ReviewDecision
from .doc_reader import DocumentContent, Paragraph

logger = logging.getLogger("v2.validation")


@dataclass
class ValidationResult:
    """Summary of validation checks."""
    passed: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fixed_decisions: list[ReviewDecision] | None = None

    def add_error(self, msg: str) -> None:
        self.passed = False
        self.errors.append(msg)
        logger.error("VALIDATION ERROR: %s", msg)

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)
        logger.warning("VALIDATION WARNING: %s", msg)


def _normalize(text: str) -> str:
    """Strip and collapse whitespace for comparison."""
    return " ".join(text.split()).strip()


# ---------------------------------------------------------------------------
# Check 1: Append-after-original detection
# ---------------------------------------------------------------------------

def check_append_corruption(decisions: list[ReviewDecision]) -> list[str]:
    """Detect REPLACE decisions where revised text contains the original text.

    This is the #1 V1 failure: the model returns original + new text
    concatenated, and the old code dumped it in, creating duplication.
    """
    issues: list[str] = []
    for d in decisions:
        if d.action != Action.REPLACE or not d.revised or not d.original:
            continue

        orig_norm = _normalize(d.original)
        rev_norm = _normalize(d.revised)

        # Check if original text appears inside revised text
        if len(orig_norm) > 20 and orig_norm in rev_norm and orig_norm != rev_norm:
            issues.append(
                f"para_idx={d.para_idx} sent_idx={d.sent_idx}: "
                f"REPLACE revised text contains original text (append pattern). "
                f"Original: {orig_norm[:60]!r}..."
            )
    return issues


# ---------------------------------------------------------------------------
# Check 2: Duplication detection
# ---------------------------------------------------------------------------

def check_duplication(decisions: list[ReviewDecision]) -> list[str]:
    """Detect duplicate entries (same para_idx + sent_idx)."""
    seen: set[tuple[int, int | None]] = set()
    dupes: list[str] = []
    for d in decisions:
        key = (d.para_idx, d.sent_idx)
        if key in seen:
            dupes.append(f"Duplicate decision for para_idx={d.para_idx} sent_idx={d.sent_idx}")
        seen.add(key)
    return dupes


# ---------------------------------------------------------------------------
# Check 3: Completeness check
# ---------------------------------------------------------------------------

def check_completeness(
    decisions: list[ReviewDecision],
    doc_a: DocumentContent,
) -> list[str]:
    """Check that every sentence in Document A has a decision."""
    missing: list[str] = []
    decision_keys: set[tuple[int, int | None]] = {
        (d.para_idx, d.sent_idx) for d in decisions
    }

    for para in doc_a.paragraphs:
        for sent in para.sentences:
            if (para.idx, sent.idx) not in decision_keys:
                missing.append(
                    f"Missing decision for para_idx={para.idx} sent_idx={sent.idx}: "
                    f"{sent.text[:60]!r}"
                )
    return missing


# ---------------------------------------------------------------------------
# Check 4: REPLACE sanity
# ---------------------------------------------------------------------------

# Prompt/metadata fragments that must never appear in revised text
_METADATA_PATTERNS = [
    "Sentences in Document A",
    "Document A [para_idx=",
    "Document B counterpart",
    "--- Paragraph",
    "--- Agent 1",
]


def check_replace_sanity(decisions: list[ReviewDecision]) -> list[str]:
    """Detect suspicious REPLACE decisions."""
    issues: list[str] = []
    for d in decisions:
        if d.action == Action.REPLACE:
            if not d.revised:
                issues.append(
                    f"para_idx={d.para_idx}: REPLACE with empty revised text"
                )
            elif _normalize(d.original) == _normalize(d.revised):
                issues.append(
                    f"para_idx={d.para_idx}: REPLACE but text is identical (should be KEEP)"
                )
            # Check for prompt metadata leaking into revised text
            for pattern in _METADATA_PATTERNS:
                if pattern in d.revised:
                    issues.append(
                        f"para_idx={d.para_idx}: REPLACE contains prompt metadata "
                        f"({pattern!r}) — must be stripped"
                    )
                    break
    return issues


# ---------------------------------------------------------------------------
# Auto-fix: clean up decisions
# ---------------------------------------------------------------------------

def auto_fix_decisions(decisions: list[ReviewDecision]) -> list[ReviewDecision]:
    """Apply automatic fixes to common issues.

    - Strip original text from revised text (fix append corruption).
    - Convert identical REPLACE to KEEP.
    - Deduplicate entries (keep last).
    """
    fixed: list[ReviewDecision] = []
    seen: dict[tuple[int, int | None], int] = {}

    for i, d in enumerate(decisions):
        key = (d.para_idx, d.sent_idx)

        # Fix metadata contamination — strip prompt artifacts from revised text
        if d.action == Action.REPLACE and d.revised:
            for pattern in _METADATA_PATTERNS:
                if pattern in d.revised:
                    # Revised text is garbage from prompt — revert to KEEP
                    logger.info("Auto-fixed metadata contamination in para_idx=%d: %r",
                                d.para_idx, pattern)
                    d.action = Action.KEEP
                    d.revised = ""
                    d.reason = "(auto-fixed: prompt metadata detected in revised text)"
                    break

        # Fix append corruption
        if d.action == Action.REPLACE and d.original and d.revised:
            orig_norm = _normalize(d.original)
            rev_norm = _normalize(d.revised)
            if len(orig_norm) > 20 and rev_norm.startswith(orig_norm):
                # Strip the original prefix
                d.revised = d.revised[len(d.original):].strip()
                logger.info("Auto-fixed append corruption in para_idx=%d", d.para_idx)
            elif len(orig_norm) > 20 and rev_norm.endswith(orig_norm):
                # Strip the original suffix
                d.revised = d.revised[:len(d.revised) - len(d.original)].strip()
                logger.info("Auto-fixed append corruption (suffix) in para_idx=%d", d.para_idx)

        # Fix identical REPLACE → KEEP
        if d.action == Action.REPLACE:
            if _normalize(d.original) == _normalize(d.revised):
                d.action = Action.KEEP
                d.revised = ""
                logger.info("Auto-fixed identical REPLACE→KEEP in para_idx=%d", d.para_idx)

        # Track for dedup
        if key in seen:
            # Replace earlier entry with this one
            fixed[seen[key]] = d
        else:
            seen[key] = len(fixed)
            fixed.append(d)

    return fixed


# ---------------------------------------------------------------------------
# Main validation entry point
# ---------------------------------------------------------------------------

def validate_decisions(
    decisions: list[ReviewDecision],
    doc_a: DocumentContent,
    *,
    auto_fix: bool = True,
) -> ValidationResult:
    """Run all validation checks on a set of decisions.

    If auto_fix=True, attempts to fix common issues automatically.
    """
    result = ValidationResult()

    # Run all checks
    append_issues = check_append_corruption(decisions)
    dupe_issues = check_duplication(decisions)
    completeness_issues = check_completeness(decisions, doc_a)
    replace_issues = check_replace_sanity(decisions)

    for issue in append_issues:
        result.add_error(f"APPEND CORRUPTION: {issue}")
    for issue in dupe_issues:
        result.add_warning(f"DUPLICATE: {issue}")
    for issue in completeness_issues:
        result.add_warning(f"MISSING: {issue}")
    for issue in replace_issues:
        result.add_warning(f"REPLACE ISSUE: {issue}")

    # Auto-fix if requested
    if auto_fix and (append_issues or dupe_issues or replace_issues):
        logger.info("Running auto-fix on %d decisions...", len(decisions))
        result.fixed_decisions = auto_fix_decisions(decisions)
        # Re-check after fix
        post_append = check_append_corruption(result.fixed_decisions)
        if not post_append:
            result.passed = True  # fixed the errors
            logger.info("Auto-fix resolved append corruption")
        else:
            for issue in post_append:
                result.add_error(f"UNFIXED APPEND: {issue}")
    elif not result.errors:
        result.fixed_decisions = decisions

    return result
