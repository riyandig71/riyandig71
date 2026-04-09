"""
V2/V3 Checkpoint — Resume from Last Position
=============================================
Saves completed batch decisions to a JSON file after each batch.
On restart, loads checkpoint and skips already-completed batches.

This prevents wasting API credits when processing dies mid-way
(credit exhaustion, network failure, rate limits, etc.)
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("checkpoint")


class Checkpoint:
    """Manages save/load of intermediate review progress."""

    def __init__(self, checkpoint_path: str | Path) -> None:
        self.path = Path(checkpoint_path)
        self._data: dict[str, Any] = {
            "agent1_batches_done": [],   # list of batch indices completed
            "agent1_decisions": [],       # accumulated decisions
            "agent2_batches_done": [],
            "agent2_decisions": [],
            "status": "in_progress",      # in_progress | agent1_done | complete
        }
        self._load()

    def _load(self) -> None:
        """Load existing checkpoint if it exists."""
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                self._data.update(saved)
                a1_done = len(self._data["agent1_batches_done"])
                a2_done = len(self._data["agent2_batches_done"])
                a1_dec = len(self._data["agent1_decisions"])
                a2_dec = len(self._data["agent2_decisions"])
                logger.info(
                    "Checkpoint loaded: Agent1 %d batches (%d decisions), "
                    "Agent2 %d batches (%d decisions), status=%s",
                    a1_done, a1_dec, a2_done, a2_dec, self._data["status"],
                )
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("Corrupt checkpoint file, starting fresh: %s", exc)

    def _save(self) -> None:
        """Write current state to disk."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    # --- Agent 1 ---

    def is_agent1_batch_done(self, batch_idx: int) -> bool:
        return batch_idx in self._data["agent1_batches_done"]

    def save_agent1_batch(self, batch_idx: int, decisions: list[dict[str, Any]]) -> None:
        """Save one completed Agent 1 batch."""
        if batch_idx not in self._data["agent1_batches_done"]:
            self._data["agent1_batches_done"].append(batch_idx)
        self._data["agent1_decisions"].extend(decisions)
        self._save()
        logger.info("Checkpoint saved: Agent 1 batch %d (%d decisions total)",
                     batch_idx, len(self._data["agent1_decisions"]))

    def get_agent1_decisions(self) -> list[dict[str, Any]]:
        return self._data["agent1_decisions"]

    def mark_agent1_done(self) -> None:
        self._data["status"] = "agent1_done"
        self._save()

    def is_agent1_done(self) -> bool:
        return self._data["status"] in ("agent1_done", "complete")

    # --- Agent 2 ---

    def is_agent2_batch_done(self, batch_idx: int) -> bool:
        return batch_idx in self._data["agent2_batches_done"]

    def save_agent2_batch(self, batch_idx: int, decisions: list[dict[str, Any]]) -> None:
        if batch_idx not in self._data["agent2_batches_done"]:
            self._data["agent2_batches_done"].append(batch_idx)
        self._data["agent2_decisions"].extend(decisions)
        self._save()
        logger.info("Checkpoint saved: Agent 2 batch %d (%d decisions total)",
                     batch_idx, len(self._data["agent2_decisions"]))

    def get_agent2_decisions(self) -> list[dict[str, Any]]:
        return self._data["agent2_decisions"]

    def mark_complete(self) -> None:
        self._data["status"] = "complete"
        self._save()

    def is_complete(self) -> bool:
        return self._data["status"] == "complete"

    @property
    def status(self) -> str:
        return self._data["status"]

    def delete(self) -> None:
        """Remove checkpoint file after successful completion."""
        if self.path.exists():
            self.path.unlink()
            logger.info("Checkpoint deleted: %s", self.path)
