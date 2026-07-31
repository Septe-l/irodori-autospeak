"""Irodori-TTS client and the worker pool that fans sentences out across servers.

Each Irodori server synthesizes one request at a time
(IRODORI_MAX_CONCURRENT_SYNTHESIS=1), so throughput scales only with the number
of endpoints. A worker is therefore just "one endpoint URL", and 1-machine and
2-machine operation differ only in the length of the URL list.
"""

from __future__ import annotations

import asyncio
import io
import logging
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Settings, kind_from_checkpoint

logger = logging.getLogger(__name__)


@dataclass
class TTSRequest:
    text: str
    mode: str          # "reference" | "caption"
    voice_id: str
    caption: str
    num_steps: int
    speed: float


@dataclass
class TTSResult:
    path: Path
    duration: float
    synth_seconds: float


class TTSClient:
    def __init__(self, settings: Settings) -> None:
        self.s = settings

    async def list_voices(self, endpoint: str) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(f"{endpoint}/v1/audio/voices")
                resp.raise_for_status()
                return [v["id"] for v in resp.json().get("data", [])]
        except Exception as e:  # noqa: BLE001
            logger.warning("voice list failed for %s: %s: %s", endpoint, type(e).__name__, e)
            return []

    async def health(self, endpoint: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(f"{endpoint}/health")
                return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    async def probe(self, endpoint: str) -> dict:
        """Ask a server what it is, so the UI can fill in the endpoint's kind.

        A server reports the checkpoint it loaded, which is what decides whether
        it can clone reference audio or take a text voice description.
        """
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{endpoint}/health")
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        model = data.get("model") or {}
        checkpoint = model.get("hf_checkpoint") or model.get("checkpoint") or ""
        voices = data.get("voices") or {}
        return {
            "ok": True,
            "checkpoint": checkpoint,
            "kind": kind_from_checkpoint(checkpoint),
            "device": model.get("model_device"),
            "voice_files": voices.get("files"),
        }

    async def synthesize(self, endpoint: str, req: TTSRequest) -> TTSResult:
        irodori: dict = {
            "num_steps": req.num_steps,
            # We already split into sentences; letting the server re-chunk would
            # only add latency and risk re-ordering inside a single response.
            "chunking_enabled": False,
        }
        payload: dict = {
            "model": "irodori-tts",
            "input": req.text,
            "response_format": "wav",
            "speed": req.speed,
            "irodori": irodori,
        }
        if req.mode == "caption":
            irodori["caption"] = req.caption
            payload["voice"] = "none"
        else:
            payload["voice"] = req.voice_id

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        async with httpx.AsyncClient(timeout=self.s.tts_timeout) as client:
            resp = await client.post(f"{endpoint}/v1/audio/speech", json=payload)
            if resp.status_code >= 400:
                # The status line alone does not say why the server refused;
                # its body carries the actual reason.
                raise RuntimeError(
                    f"HTTP {resp.status_code} from {endpoint}: "
                    f"{resp.text[:300]} (text={req.text[:40]!r}, voice={req.voice_id!r})"
                )
            data = resp.content
        elapsed = loop.time() - t0

        path = self.s.audio_dir / f"{uuid.uuid4().hex}.wav"
        path.write_bytes(data)
        return TTSResult(path=path, duration=_wav_duration(data), synth_seconds=elapsed)


def _wav_duration(data: bytes) -> float:
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:  # noqa: BLE001
        return 0.0


class TTSPool:
    """Runs `len(endpoints)` workers pulling from one ordered queue.

    Ordering is not preserved by completion — a later sentence can finish first
    if it lands on a faster endpoint — so each job carries its sequence number
    and the engine reassembles playback order.
    """

    def __init__(self, settings: Settings, client: TTSClient) -> None:
        self.s = settings
        self.client = client
        self._queue: asyncio.Queue = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._endpoints: list[str] = []

    @property
    def endpoints(self) -> list[str]:
        return list(self._endpoints)

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    def start(self, endpoints: list[str]) -> None:
        self.stop()
        self._endpoints = endpoints
        self._workers = [
            asyncio.create_task(self._worker(ep, i)) for i, ep in enumerate(endpoints)
        ]
        logger.info("tts pool started with %d endpoint(s): %s", len(endpoints), endpoints)

    def stop(self) -> None:
        for t in self._workers:
            t.cancel()
        self._workers = []

    async def submit(self, req: TTSRequest, on_done, on_error, cancelled) -> None:
        """Queue a sentence.

        `cancelled()` is re-checked right before synthesis so that a job which is
        still waiting when the user changes topic never occupies a worker.
        """
        await self._queue.put((req, on_done, on_error, cancelled))

    def drain(self) -> int:
        """Drop everything still waiting. In-flight jobs are not interrupted."""
        dropped = 0
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        return dropped

    async def _worker(self, endpoint: str, index: int) -> None:
        while True:
            req, on_done, on_error, cancelled = await self._queue.get()
            try:
                if cancelled():
                    continue
                result = await self.client.synthesize(endpoint, req)
                logger.info(
                    "worker%d %s synth %.2fs for %.2fs audio (%d chars)",
                    index, endpoint, result.synth_seconds, result.duration, len(req.text),
                )
                if cancelled():
                    result.path.unlink(missing_ok=True)
                    continue
                await on_done(result)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.error("tts failed on %s: %s: %s", endpoint, type(e).__name__, e)
                try:
                    await on_error(e)
                except Exception:  # noqa: BLE001
                    logger.exception("tts error handler failed")
            finally:
                self._queue.task_done()


class TTSPoolSet:
    """One pool per checkpoint, keyed by voice mode.

    In duo mode the two speakers can use different checkpoints, so both sets of
    workers have to be live at once and each sentence has to reach the pool that
    can actually produce that speaker's voice.
    """

    def __init__(self, settings: Settings, client: TTSClient) -> None:
        self.s = settings
        self.client = client
        self._pools: dict[str, TTSPool] = {}

    @property
    def depth(self) -> int:
        return sum(p.depth for p in self._pools.values())

    @property
    def endpoints(self) -> list[str]:
        return [ep for p in self._pools.values() for ep in p.endpoints]

    def sync(self, modes: list[str]) -> None:
        """Start workers for `modes`, stop the ones no longer needed."""
        for mode in list(self._pools):
            if mode not in modes:
                self._pools.pop(mode).stop()
        for mode in modes:
            endpoints = self.s.endpoints_for_mode(mode)
            if not endpoints:
                logger.warning("no TTS endpoints configured for mode %r", mode)
                continue
            pool = self._pools.get(mode)
            if pool is None:
                pool = TTSPool(self.s, self.client)
                self._pools[mode] = pool
                pool.start(endpoints)
            elif pool.endpoints != endpoints:
                pool.start(endpoints)

    def stop(self) -> None:
        for pool in self._pools.values():
            pool.stop()
        self._pools.clear()

    def drain(self) -> int:
        return sum(p.drain() for p in self._pools.values())

    async def submit(self, req: TTSRequest, on_done, on_error, cancelled) -> None:
        pool = self._pools.get(req.mode)
        if pool is None:
            # Mode was enabled after this turn started planning; bring it up now
            # rather than dropping the sentence.
            self.sync(sorted(set(self._pools) | {req.mode}))
            pool = self._pools.get(req.mode)
        if pool is None:
            await on_error(RuntimeError(f"no TTS endpoint for mode {req.mode!r}"))
            return
        await pool.submit(req, on_done, on_error, cancelled)
