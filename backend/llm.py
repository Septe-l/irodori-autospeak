"""LM Studio streaming client and the incremental Japanese sentence splitter.

The splitter is what makes the pipeline feel responsive: instead of waiting for a
whole turn of text before touching the TTS servers, every completed sentence is
handed off the moment its terminator arrives.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator

import httpx

from .config import Settings

logger = logging.getLogger(__name__)

SENTENCE_END = "。！？!?…"
CLAUSE_BREAK = "、，,"
# Reasoning models wrap their scratchpad in these; it must never reach the TTS.
THINK_OPEN = re.compile(r"<(think|thinking|reasoning)>", re.I)
THINK_CLOSE = re.compile(r"</(think|thinking|reasoning)>", re.I)
# Markdown decoration the model sometimes emits despite instructions.
STRIP_CHARS = re.compile(r"[*_`#>\[\]{}|~]")
URL_RE = re.compile(r"https?://\S+")
# List markers only. A bare leading number is part of the sentence — matching it
# turned "16インチのMacBook Pro" into "インチのMacBook Pro".
LIST_MARKER = re.compile(r"^\s*(?:[-・*]+\s*|\d{1,2}\s*[.)、]\s+)")


def clean_for_speech(text: str) -> str:
    text = URL_RE.sub("", text)
    text = STRIP_CHARS.sub("", text)
    text = LIST_MARKER.sub("", text)
    return " ".join(text.split())


class SentenceSplitter:
    """Feed it stream deltas, get back speakable sentences as they complete."""

    def __init__(self, max_chars: int, min_chars: int) -> None:
        self.max_chars = max_chars
        self.min_chars = min_chars
        self._buf = ""
        self._in_think = False
        self._pending = ""  # a too-short fragment waiting to merge forward

    def feed(self, delta: str) -> list[str]:
        self._buf += delta
        out: list[str] = []

        while True:
            if self._in_think:
                m = THINK_CLOSE.search(self._buf)
                if not m:
                    # Keep only a tail long enough to catch a split closing tag.
                    self._buf = self._buf[-16:]
                    return out
                self._buf = self._buf[m.end():]
                self._in_think = False
                continue

            m = THINK_OPEN.search(self._buf)
            if m:
                before, self._buf = self._buf[: m.start()], self._buf[m.end():]
                self._in_think = True
                out.extend(self._drain(before))
                continue

            idx = self._find_end(self._buf)
            if idx is None:
                return out
            sentence, self._buf = self._buf[: idx + 1], self._buf[idx + 1:]
            out.extend(self._emit(sentence))

    def flush(self) -> list[str]:
        """Call once the stream ends, to release whatever is left."""
        out = self._drain(self._buf)
        self._buf = ""
        if self._pending:
            out.append(self._pending)
            self._pending = ""
        return out

    def _drain(self, text: str) -> list[str]:
        out: list[str] = []
        rest = text
        while True:
            idx = self._find_end(rest)
            if idx is None:
                break
            out.extend(self._emit(rest[: idx + 1]))
            rest = rest[idx + 1:]
        if rest.strip():
            out.extend(self._emit(rest))
        return out

    @staticmethod
    def _find_end(text: str) -> int | None:
        for i, ch in enumerate(text):
            if ch in SENTENCE_END or ch == "\n":
                return i
        return None

    def _emit(self, raw: str) -> list[str]:
        cleaned = clean_for_speech(raw)
        if not cleaned:
            return []

        if self._pending:
            cleaned = self._pending + cleaned
            self._pending = ""

        if len(cleaned) < self.min_chars:
            self._pending = cleaned
            return []

        return self._split_long(cleaned)

    def _split_long(self, text: str) -> list[str]:
        if len(text) <= self.max_chars:
            return [text]

        parts: list[str] = []
        rest = text
        while len(rest) > self.max_chars:
            window = rest[: self.max_chars]
            cut = max((window.rfind(c) for c in CLAUSE_BREAK), default=-1)
            if cut < self.min_chars:
                cut = self.max_chars - 1
            parts.append(rest[: cut + 1].strip())
            rest = rest[cut + 1:].strip()
        if rest:
            if len(rest) < self.min_chars and parts:
                parts[-1] = parts[-1] + rest
            else:
                parts.append(rest)
        return [p for p in parts if p]


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    async def list_models(self) -> list[dict]:
        """Chat-capable models only, loaded ones first.

        LM Studio's native endpoint reports both type and load state; `/v1/models`
        does not, and would offer embedding models as if they could hold a
        conversation. Loading a second large model on a 32GB Mac fails outright,
        so the UI needs to show which one is already resident.
        """
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(f"{self.s.llm_url}/api/v0/models")
                resp.raise_for_status()
                models = [
                    {"id": m["id"], "loaded": m.get("state") == "loaded"}
                    for m in resp.json().get("data", [])
                    if m.get("type") in ("llm", "vlm")
                ]
                models.sort(key=lambda m: (not m["loaded"], m["id"]))
                return models
        except Exception as e:  # noqa: BLE001
            logger.warning("model list failed: %s: %s", type(e).__name__, e)
            return []

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        payload = {
            "model": model or self.s.llm_model,
            "messages": messages,
            "temperature": self.s.llm_temperature if temperature is None else temperature,
            "max_tokens": self.s.llm_max_tokens if max_tokens is None else max_tokens,
            "stream": True,
        }
        async with httpx.AsyncClient(timeout=self.s.llm_timeout) as client:
            async with client.stream(
                "POST", f"{self.s.llm_url}/v1/chat/completions", json=payload
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    body = line[6:].strip()
                    if body == "[DONE]":
                        return
                    try:
                        chunk = json.loads(body)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        yield piece

    async def complete(
        self, messages: list[dict[str, str]], *, model: str | None = None, max_tokens: int = 120
    ) -> str:
        parts: list[str] = []
        async for piece in self.stream(messages, model=model, max_tokens=max_tokens, temperature=0.6):
            parts.append(piece)
        text = "".join(parts)
        text = THINK_CLOSE.split(text)[-1] if THINK_CLOSE.search(text) else text
        return text.strip()
