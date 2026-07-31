"""The orchestration engine: search -> generate -> synthesize -> publish, forever.

Two cursors drive everything:

* `next_turn_id` — how far generation has run ahead.
* `playing_turn_id` — what the browser reports it is currently speaking.

Generation is gated on the gap between them, which is what "prefetch depth"
means in practice. On a topic interjection the currently-playing turn is allowed
to finish and everything after it is discarded, audio included.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .config import Settings, Speaker
from .llm import LLMClient, SentenceSplitter
from .memory import MemoryClient
from .prompts import (
    DUO_HANDOFF,
    MEMORY_BLOCK,
    NEXT_SUBTOPIC,
    TOPIC_SWITCH_NOTE,
    TURN_INSTRUCTION,
    TURN_INSTRUCTION_NO_CONTEXT,
    covered_block,
)
from .search import SearchResult, Searcher
from .transcript import Transcript
from .tts import TTSClient, TTSPoolSet, TTSRequest, TTSResult

logger = logging.getLogger(__name__)

SENTENCES_PER_TURN = 5


@dataclass
class Sentence:
    turn_id: int
    seq: int
    text: str
    path: Any = None
    duration: float = 0.0
    ready: bool = False
    published: bool = False
    played: bool = False
    dropped: bool = False


@dataclass
class Turn:
    id: int
    topic: str
    subtopic: str
    speaker_index: int = 0
    speaker_name: str = ""
    sources: list[dict[str, str]] = field(default_factory=list)
    sentences: list[Sentence] = field(default_factory=list)
    text_complete: bool = False
    dropped: bool = False
    search_seconds: float = 0.0
    llm_seconds: float = 0.0
    llm_first_token: float = 0.0
    tts_seconds: float = 0.0
    """Wall-clock spent synthesizing this turn, summed across workers — it
    exceeds elapsed time when several endpoints work in parallel."""

    def text(self) -> str:
        return "".join(s.text for s in self.sentences)

    def timing(self) -> dict:
        return {
            "search": round(self.search_seconds, 1),
            "llm": round(self.llm_seconds, 1),
            "llm_first": round(self.llm_first_token, 1),
            "tts": round(self.tts_seconds, 1),
        }


class Engine:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.llm = LLMClient(settings)
        self.searcher = Searcher(settings)
        self.tts_client = TTSClient(settings)
        self.pool = TTSPoolSet(settings, self.tts_client)
        self.memory = MemoryClient(settings)
        self.transcript = Transcript(settings.logs_dir)

        self.running = False
        self.status = "idle"
        self.topic = ""
        self.covered: list[str] = []
        self.turns: dict[int, Turn] = {}
        self.next_turn_id = 0
        self.playing_turn_id = -1
        self.cursor = (0, 0)          # next (turn_id, seq) eligible for publishing
        self.epoch = 0                 # bumped on every interjection/stop
        self.last_error: str | None = None

        self._loop_task: asyncio.Task | None = None
        self._subscribers: set[asyncio.Queue] = set()
        self._topic_changed = asyncio.Event()
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- events

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def emit(self, kind: str, **data: Any) -> None:
        event = {"type": kind, **data}
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("subscriber queue full, dropping %s event", kind)

    def _set_status(self, status: str) -> None:
        self.status = status
        self.emit("state", **self.state())

    def state(self) -> dict:
        return {
            "running": self.running,
            "status": self.status,
            "topic": self.topic,
            "turns_ahead": self.turns_ahead(),
            "buffered_seconds": round(self.buffered_seconds(), 1),
            "playing_turn": self.playing_turn_id,
            "next_turn": self.next_turn_id,
            "tts_queue": self.pool.depth,
            "endpoints": self.pool.endpoints,
            "speaker_mode": self.s.speaker_mode,
            "speakers": [sp.name for sp in self.s.speakers],
            "single_tts": self.s.single_tts(),
            "active_speakers": self.s.active_speaker_count(),
            "search_enabled": self.s.search_enabled,
            "mcp_enabled": self.s.mcp_enabled,
            "can_resume": (not self.running) and bool(self.topic),
            "llm_model": self.s.llm_model,
            "num_steps": self.s.tts_num_steps,
            "error": self.last_error,
        }

    def recent(self, limit: int = 12) -> list[dict]:
        """Snapshot of the visible log, so a page reload can rebuild it.

        The SSE stream only carries new events; without this a refresh would
        leave the listener staring at an empty page while audio keeps playing.
        """
        ordered = sorted(self.turns.values(), key=lambda t: t.id)[-limit:]
        return [
            {
                "turn_id": t.id,
                "subtopic": t.subtopic,
                "sources": t.sources,
                "dropped": t.dropped,
                "speaker_index": t.speaker_index,
                "speaker_name": t.speaker_name,
                "sentences": [
                    {"seq": s.seq, "text": s.text, "played": s.played, "dropped": s.dropped}
                    for s in t.sentences
                ],
            }
            for t in ordered
        ]

    # --------------------------------------------------------------- pacing

    def turns_ahead(self) -> int:
        live = [t for t in self.turns.values() if not t.dropped and t.id > self.playing_turn_id]
        return len(live)

    def buffered_seconds(self) -> float:
        total = 0.0
        for turn in self.turns.values():
            if turn.dropped or turn.id < self.playing_turn_id:
                continue
            for s in turn.sentences:
                if s.ready and not s.played and not s.dropped:
                    total += s.duration
        return total

    def _should_pause_generation(self) -> bool:
        return (
            self.turns_ahead() >= self.s.prefetch_turns
            or self.buffered_seconds() >= self.s.prefetch_seconds
        )

    # ------------------------------------------------------------ lifecycle

    async def start(self, topic: str) -> None:
        async with self._lock:
            if self.running:
                await self._interject(topic)
                return
            self.topic = topic.strip()
            self.covered = []
            self.turns = {}
            self.next_turn_id = 0
            self.playing_turn_id = -1
            self.cursor = (0, 0)
            self.last_error = None
            self.running = True
            self.pool.sync(self.s.active_modes())
            self._loop_task = asyncio.create_task(self._run())
            self._set_status("starting")
            self.emit("topic", topic=self.topic, interjected=False)

    async def resume(self) -> bool:
        """Pick the topic back up after a stop, without repeating what was said.

        `covered` survives the stop, so the next subtopic continues the thread
        rather than starting the topic over.
        """
        async with self._lock:
            if self.running or not self.topic:
                return False
            self.epoch += 1
            self.last_error = None
            self.running = True
            # Playback state was thrown away when the browser stopped, so the
            # publish cursor has to start at whatever will be generated next.
            self.playing_turn_id = self.next_turn_id - 1
            self.cursor = (self.next_turn_id, 0)
            self.pool.sync(self.s.active_modes())
            self._loop_task = asyncio.create_task(self._run())
            self._set_status("starting")
            self.emit("topic", topic=self.topic, interjected=False, resumed=True)
            return True

    async def stop(self) -> None:
        async with self._lock:
            self.running = False
            self.epoch += 1
            if self._loop_task:
                self._loop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._loop_task
                self._loop_task = None
            self.pool.drain()
            self.pool.stop()
            self._set_status("idle")

    async def set_topic(self, topic: str) -> None:
        async with self._lock:
            if not self.running:
                return
            await self._interject(topic)

    async def _interject(self, topic: str) -> None:
        """Keep the turn being spoken, throw away everything after it."""
        self.topic = topic.strip()
        self.epoch += 1
        keep_through = self.playing_turn_id
        dropped_audio = 0

        for turn in list(self.turns.values()):
            if turn.id <= keep_through:
                continue
            turn.dropped = True
            for s in turn.sentences:
                s.dropped = True
                if s.path is not None:
                    with contextlib.suppress(Exception):
                        s.path.unlink(missing_ok=True)
                    dropped_audio += 1
            if turn.text_complete or turn.sentences:
                self.transcript.write_turn(
                    turn_id=turn.id, topic=turn.topic, subtopic=turn.subtopic,
                    text=turn.text(), sources=turn.sources,
                    speaker=turn.speaker_name, discarded=True,
                )

        self.pool.drain()
        # Turn ids stay monotonic. Reusing them would make the browser append the
        # new turn's sentences into the discarded turn's bubble. The publish
        # cursor simply skips over the dropped ids.
        self.cursor = (keep_through + 1, 0)
        self.covered = []
        self._topic_changed.set()

        logger.info(
            "interjected topic=%r keep_through=%d dropped_audio=%d",
            self.topic, keep_through, dropped_audio,
        )
        self.emit("drop", from_turn=keep_through + 1)
        self.emit("topic", topic=self.topic, interjected=True)
        self._set_status("switching")

    # ----------------------------------------------------------- playback io

    def report_playing(self, turn_id: int, seq: int) -> None:
        if turn_id > self.playing_turn_id:
            self.playing_turn_id = turn_id
        turn = self.turns.get(turn_id)
        if turn:
            for s in turn.sentences:
                if s.seq < seq:
                    s.played = True
        self.emit("state", **self.state())

    def report_played(self, turn_id: int, seq: int) -> None:
        turn = self.turns.get(turn_id)
        if turn:
            for s in turn.sentences:
                if s.seq <= seq:
                    s.played = True
        self._cleanup_audio()
        self.emit("state", **self.state())

    def _cleanup_audio(self) -> None:
        files = sorted(
            self.s.audio_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        for stale in files[self.s.audio_retain:]:
            with contextlib.suppress(Exception):
                stale.unlink()

    # ------------------------------------------------------------- the loop

    async def _run(self) -> None:
        try:
            while self.running:
                if self._should_pause_generation():
                    self._set_status("buffered")
                    await self._wait_for_room()
                    continue
                try:
                    await self._generate_turn()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    self.last_error = f"{type(e).__name__}: {e}"
                    logger.exception("turn generation failed")
                    self.emit("error", message=self.last_error)
                    await asyncio.sleep(3)
        except asyncio.CancelledError:
            logger.info("engine loop cancelled")
            raise

    async def _wait_for_room(self) -> None:
        """Sleep until the browser has drained enough buffer, or the topic changes."""
        while self.running and self._should_pause_generation():
            waiter = asyncio.create_task(self._topic_changed.wait())
            done, _ = await asyncio.wait({waiter}, timeout=1.0)
            if waiter in done:
                self._topic_changed.clear()
                waiter.cancel()
                return
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter

    async def _generate_turn(self) -> None:
        epoch = self.epoch
        turn_id = self.next_turn_id
        first_of_topic = not self.covered

        # 1. pick the angle to dig into
        if first_of_topic:
            subtopic = self.topic
        else:
            self._set_status("planning")
            subtopic = await self._pick_subtopic()
            if epoch != self.epoch:
                return

        # 2. research it — except on the opening turn of a topic. Feeding several
        # thousand characters of context makes the model's first token land ~15s
        # later, and that delay is silence the listener actually hears, both at
        # startup and right after a topic is injected. The opening turn talks
        # from the model's own knowledge; the search starts with turn two.
        search_seconds = 0.0
        if first_of_topic or not self.s.search_enabled:
            result = SearchResult(query=subtopic, hits=[])
        else:
            self._set_status("searching")
            t0 = time.perf_counter()
            result = await self.searcher.search(subtopic)
            search_seconds = time.perf_counter() - t0
            if epoch != self.epoch:
                return

        memories = ""
        if self.s.mcp_enabled:
            self._set_status("recalling")
            memories = await self.memory.search(subtopic)
            if epoch != self.epoch:
                return

        speaker_index = self._next_speaker_index()
        speaker = self.s.speaker(speaker_index)
        turn = Turn(
            id=turn_id,
            topic=self.topic,
            subtopic=subtopic,
            speaker_index=speaker_index,
            speaker_name=speaker.name,
            sources=result.sources(),
            search_seconds=search_seconds,
        )
        self.turns[turn_id] = turn
        self.next_turn_id = turn_id + 1
        self.covered.append(subtopic)
        self.emit(
            "turn_start", turn_id=turn_id, subtopic=subtopic,
            sources=turn.sources, topic=self.topic,
            speaker_index=speaker_index, speaker_name=speaker.name,
        )

        # 3. generate prose, dispatching each finished sentence straight to TTS
        self._set_status("speaking" if turn_id > 0 else "generating")
        context = result.context(self.s.search_page_chars)
        messages = self._build_messages(
            subtopic, context, first_of_topic, speaker_index, memories
        )
        splitter = SentenceSplitter(self.s.max_sentence_chars, self.s.min_sentence_chars)
        seq = 0
        t_llm = time.perf_counter()

        try:
            async for delta in self.llm.stream(messages):
                if epoch != self.epoch or not self.running:
                    return
                if not turn.llm_first_token:
                    turn.llm_first_token = time.perf_counter() - t_llm
                for sentence_text in splitter.feed(delta):
                    seq = await self._dispatch(turn, seq, sentence_text, epoch, speaker)
            for sentence_text in splitter.flush():
                if epoch != self.epoch or not self.running:
                    return
                seq = await self._dispatch(turn, seq, sentence_text, epoch, speaker)
        finally:
            turn.llm_seconds = time.perf_counter() - t_llm
            turn.text_complete = True
            self.emit("turn_timing", turn_id=turn.id, **turn.timing())

        if epoch == self.epoch and not turn.dropped:
            self.transcript.write_turn(
                turn_id=turn.id, topic=turn.topic, subtopic=turn.subtopic,
                text=turn.text(), sources=turn.sources, speaker=turn.speaker_name,
            )
            self.emit("turn_end", turn_id=turn.id, sentences=len(turn.sentences))
        self._publish_ready()

    async def _dispatch(
        self, turn: Turn, seq: int, text: str, epoch: int, speaker: Speaker
    ) -> int:
        sentence = Sentence(turn_id=turn.id, seq=seq, text=text)
        turn.sentences.append(sentence)
        self.emit(
            "sentence", turn_id=turn.id, seq=seq, text=text,
            speaker_index=turn.speaker_index, speaker_name=turn.speaker_name,
        )

        req = TTSRequest(
            text=text,
            mode=speaker.endpoint_mode(),
            voice_id=speaker.voice_id,
            caption=speaker.voice_caption,
            num_steps=self.s.tts_num_steps,
            speed=self.s.tts_speed,
        )

        def cancelled() -> bool:
            return epoch != self.epoch or sentence.dropped or turn.dropped

        async def on_done(result: TTSResult) -> None:
            sentence.path = result.path
            sentence.duration = result.duration
            sentence.ready = True
            turn.tts_seconds += result.synth_seconds
            self.emit("turn_timing", turn_id=turn.id, **turn.timing())
            self._publish_ready()

        async def on_error(exc: Exception) -> None:
            # A sentence that cannot be synthesized must not stall the queue
            # behind it — mark it ready with no audio and let publishing skip it.
            sentence.ready = True
            sentence.duration = 0.0
            sentence.path = None
            self.last_error = f"TTS: {type(exc).__name__}: {exc}"
            self.emit("error", message=self.last_error)
            self._publish_ready()

        await self.pool.submit(req, on_done, on_error, cancelled)
        return seq + 1

    def _publish_ready(self) -> None:
        """Release audio to the browser strictly in speaking order."""
        while True:
            turn_id, seq = self.cursor
            turn = self.turns.get(turn_id)

            if turn is None:
                return
            if turn.dropped:
                self.cursor = (turn_id + 1, 0)
                continue

            if seq < len(turn.sentences):
                sentence = turn.sentences[seq]
                if sentence.dropped:
                    self.cursor = (turn_id, seq + 1)
                    continue
                if not sentence.ready:
                    return
                if not sentence.published:
                    sentence.published = True
                    if sentence.path is not None:
                        self.emit(
                            "audio",
                            turn_id=turn_id,
                            seq=seq,
                            url=f"/audio/{sentence.path.name}",
                            duration=round(sentence.duration, 2),
                            text=sentence.text,
                            speaker_index=turn.speaker_index,
                            speaker_name=turn.speaker_name,
                        )
                self.cursor = (turn_id, seq + 1)
                continue

            # Ran out of sentences in this turn: only move on once we know no
            # more are coming, otherwise we would skip ahead mid-turn.
            if turn.text_complete:
                self.cursor = (turn_id + 1, 0)
                continue
            return

    # ------------------------------------------------------------- prompting

    async def _pick_subtopic(self) -> str:
        prompt = NEXT_SUBTOPIC.format(topic=self.topic, covered=covered_block(self.covered))
        try:
            line = await self.llm.complete(
                [{"role": "user", "content": prompt}], max_tokens=200
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("subtopic pick failed: %s: %s", type(e).__name__, e)
            return self.topic

        return self._sanitize_query(line) or f"{self.topic} 詳細"

    def _sanitize_query(self, raw: str) -> str:
        """Keep only output that actually looks like a search query.

        The model periodically answers with a full explanatory sentence instead
        of keywords. Searching that returns unrelated pages, so anything
        sentence-shaped is rejected in favour of the fallback.
        """
        for candidate in reversed([ln.strip() for ln in raw.splitlines() if ln.strip()]):
            cleaned = candidate.strip("「」『』\"'`*-・ 　")
            if not cleaned:
                continue
            if any(ch in cleaned for ch in "。、，,？?！!:："):
                continue
            if len(cleaned) > 40:
                continue
            if cleaned in self.covered:
                continue
            return cleaned
        return ""

    def _next_speaker_index(self) -> int:
        """Alternate speakers in duo mode, continuing from whoever spoke last."""
        if self.s.active_speaker_count() < 2:
            return 0
        live = [t for t in sorted(self.turns.values(), key=lambda t: t.id) if not t.dropped]
        if not live:
            return 0
        return (live[-1].speaker_index + 1) % self.s.active_speaker_count()

    def _build_messages(
        self,
        subtopic: str,
        context: str,
        first_of_topic: bool,
        speaker_index: int,
        memories: str = "",
    ) -> list[dict]:
        if context.strip():
            instruction = TURN_INSTRUCTION.format(
                subtopic=subtopic, context=context, sentences=SENTENCES_PER_TURN
            )
        else:
            instruction = TURN_INSTRUCTION_NO_CONTEXT.format(
                subtopic=subtopic, sentences=SENTENCES_PER_TURN
            )

        if first_of_topic and self.turns:
            instruction += "\n" + TOPIC_SWITCH_NOTE.format(topic=self.topic)

        speaker = self.s.speaker(speaker_index)
        system = speaker.persona
        if memories.strip():
            system += "\n\n" + MEMORY_BLOCK.format(memories=memories.strip())
        messages = [{"role": "system", "content": system}]

        previous = self._previous_turn()
        if previous is not None:
            text = previous.text()[-400:]
            if self.s.active_speaker_count() >= 2 and previous.speaker_index != speaker_index:
                # In duo mode the partner's turn is the other side of a
                # conversation, not this speaker's own words — presenting it as
                # `assistant` would make the model imitate the partner's voice.
                messages.append({"role": "user", "content": f"{previous.speaker_name}「{text}」"})
                instruction = (
                    DUO_HANDOFF.format(partner=previous.speaker_name, me=speaker.name)
                    + "\n"
                    + instruction
                )
            else:
                messages.append({"role": "assistant", "content": text})

        messages.append({"role": "user", "content": instruction})
        return messages

    def _previous_turn(self) -> Turn | None:
        live = [
            t for t in sorted(self.turns.values(), key=lambda t: t.id)
            if not t.dropped and t.sentences
        ]
        return live[-1] if live else None
