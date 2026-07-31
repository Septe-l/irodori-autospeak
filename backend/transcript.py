"""Append-only transcript of everything spoken, with the sources behind it."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class Transcript:
    def __init__(self, logs_dir: Path) -> None:
        self.dir = logs_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._path = self.dir / f"session-{datetime.now():%Y%m%d-%H%M%S}.jsonl"

    @property
    def path(self) -> Path:
        return self._path

    def write_turn(
        self,
        *,
        turn_id: int,
        topic: str,
        subtopic: str,
        text: str,
        sources: list[dict[str, str]],
        speaker: str = "",
        discarded: bool = False,
    ) -> None:
        record = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "turn_id": turn_id,
            "topic": topic,
            "subtopic": subtopic,
            "speaker": speaker,
            "text": text,
            "sources": sources,
            "discarded": discarded,
        }
        try:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            logger.error("transcript write failed: %s: %s", type(e).__name__, e)
