from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import voices as voicelib
from .config import PROJECT_ROOT, Speaker, TTSEndpoint, get_settings, save_overrides
from .engine import Engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("autospeak")

settings = get_settings()
engine = Engine(settings)
app = FastAPI(title="Irodori AutoSpeak")

STATIC_DIR = PROJECT_ROOT / "static"


class TopicBody(BaseModel):
    topic: str


class PlaybackBody(BaseModel):
    turn_id: int
    seq: int


class SpeakerBody(BaseModel):
    name: str
    persona: str
    voice_mode: str = "reference"
    voice_id: str = "none"
    voice_caption: str = ""


class EndpointBody(BaseModel):
    url: str
    kind: str = "reference"
    enabled: bool = True


class ProbeBody(BaseModel):
    url: str


class RenameBody(BaseModel):
    name: str


class SettingsBody(BaseModel):
    llm_url: str | None = None
    llm_model: str | None = None
    speaker_mode: str | None = None
    speakers: list[SpeakerBody] | None = None
    tts_endpoints: list[EndpointBody] | None = None
    search_enabled: bool | None = None
    searxng_url: str | None = None
    mcp_enabled: bool | None = None
    mcp_url: str | None = None
    ref_target_seconds: float | None = None
    num_steps: int | None = None
    speed: float | None = None
    prefetch_turns: int | None = None
    prefetch_seconds: float | None = None
    temperature: float | None = None


@app.get("/")
async def index() -> FileResponse:
    # Same reason as NoCacheStatic below: an edited page must take effect on
    # reload rather than being served from the browser's cache.
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


@app.get("/events")
async def events() -> StreamingResponse:
    queue = engine.subscribe()

    async def gen():
        try:
            yield _sse({"type": "state", **engine.state()})
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _sse(event)
        except asyncio.CancelledError:
            raise
        finally:
            engine.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/api/start")
async def start(body: TopicBody) -> dict:
    topic = body.topic.strip()
    if not topic:
        raise HTTPException(status_code=400, detail="topic is empty")
    await engine.start(topic)
    return engine.state()


@app.post("/api/topic")
async def set_topic(body: TopicBody) -> dict:
    topic = body.topic.strip()
    if not topic:
        raise HTTPException(status_code=400, detail="topic is empty")
    # Echoed into the transcript so the log shows what was asked for, not just
    # what came back.
    engine.emit("user_message", text=topic, interjected=engine.running)
    if not engine.running:
        await engine.start(topic)
    else:
        await engine.set_topic(topic)
    return engine.state()


@app.post("/api/stop")
async def stop() -> dict:
    await engine.stop()
    return engine.state()


@app.post("/api/resume")
async def resume() -> dict:
    if not await engine.resume():
        raise HTTPException(
            status_code=409,
            detail="再開できません（話題がまだ無いか、すでに動いています）",
        )
    return engine.state()


@app.post("/api/playing")
async def playing(body: PlaybackBody) -> dict:
    engine.report_playing(body.turn_id, body.seq)
    return {"ok": True}


@app.post("/api/played")
async def played(body: PlaybackBody) -> dict:
    engine.report_played(body.turn_id, body.seq)
    return {"ok": True}


@app.get("/api/state")
async def state() -> dict:
    return engine.state()


@app.get("/api/recent")
async def recent() -> dict:
    return {"turns": engine.recent()}


@app.post("/api/clear")
async def clear() -> dict:
    """Wipe the on-screen log without interrupting speech.

    Engine state is left alone — the publish cursor and prefetch accounting
    depend on it — so this only tells every connected browser to empty its log.
    """
    engine.emit("clear")
    return {"ok": True}


@app.get("/api/settings")
async def read_settings() -> dict:
    return {
        "llm_url": settings.llm_url,
        "llm_model": settings.llm_model,
        "speaker_mode": settings.speaker_mode,
        "speakers": [sp.model_dump() for sp in settings.speakers],
        "tts_endpoints": [e.model_dump() for e in settings.tts_endpoints],
        "single_tts": settings.single_tts(),
        "available_kinds": settings.available_kinds(),
        "search_enabled": settings.search_enabled,
        "mcp_enabled": settings.mcp_enabled,
        "mcp_url": settings.mcp_url,
        "ref_target_seconds": settings.ref_target_seconds,
        "num_steps": settings.tts_num_steps,
        "speed": settings.tts_speed,
        "prefetch_turns": settings.prefetch_turns,
        "prefetch_seconds": settings.prefetch_seconds,
        "temperature": settings.llm_temperature,
        "searxng_url": settings.searxng_url,
        "ref_endpoints": settings.ref_endpoints(),
        "caption_endpoints": settings.caption_endpoints(),
    }


@app.post("/api/settings")
async def write_settings(body: SettingsBody) -> dict:
    warnings: list[str] = []

    if body.llm_url is not None:
        settings.llm_url = _normalize_url(body.llm_url) or settings.llm_url
    if body.llm_model is not None:
        settings.llm_model = body.llm_model
    if body.speaker_mode in ("solo", "duo"):
        settings.speaker_mode = body.speaker_mode
    if body.tts_endpoints is not None:
        seen: set[str] = set()
        endpoints = []
        for ep in body.tts_endpoints:
            norm = TTSEndpoint(**ep.model_dump()).normalized()
            if not norm.url or norm.url in seen:
                continue
            seen.add(norm.url)
            endpoints.append(norm)
        settings.tts_endpoints = endpoints
    if body.speakers:
        settings.speakers = [
            Speaker(
                name=sp.name.strip() or f"話者{i + 1}",
                persona=sp.persona,
                voice_mode="caption" if sp.voice_mode == "caption" else "reference",
                voice_id=sp.voice_id or "none",
                voice_caption=sp.voice_caption,
            )
            for i, sp in enumerate(body.speakers)
        ]
    if body.search_enabled is not None:
        settings.search_enabled = body.search_enabled
    if body.searxng_url is not None:
        settings.searxng_url = body.searxng_url.strip().rstrip("/")
    if body.mcp_enabled is not None:
        settings.mcp_enabled = body.mcp_enabled
    if body.mcp_url is not None:
        settings.mcp_url = body.mcp_url.strip()
    if body.ref_target_seconds is not None:
        settings.ref_target_seconds = max(3.0, min(30.0, body.ref_target_seconds))
    if body.num_steps is not None:
        settings.tts_num_steps = max(4, min(80, body.num_steps))
    if body.speed is not None:
        settings.tts_speed = max(0.25, min(4.0, body.speed))
    if body.prefetch_turns is not None:
        settings.prefetch_turns = max(1, min(12, body.prefetch_turns))
    if body.prefetch_seconds is not None:
        settings.prefetch_seconds = max(10.0, min(600.0, body.prefetch_seconds))
    if body.temperature is not None:
        settings.llm_temperature = max(0.0, min(2.0, body.temperature))

    warnings += await _reconcile_speakers()

    # A speaker may now need a checkpoint whose workers are not running yet.
    if engine.running:
        engine.pool.sync(settings.active_modes())

    save_overrides(settings)
    engine.emit("state", **engine.state())
    return {**await read_settings(), "warnings": warnings}


async def _reconcile_speakers() -> list[str]:
    """Keep the speaker setup answerable by the servers actually configured."""
    notes: list[str] = []
    kinds = settings.available_kinds()

    if not kinds:
        return ["TTSサーバーが1つも設定されていません。音声は生成されません。"]

    # A speaker asking for a style no server holds would fail on every sentence.
    for i in range(settings.active_speaker_count()):
        sp = settings.speaker(i)
        if sp.endpoint_mode() in kinds:
            continue
        fallback = kinds[0]
        notes.append(
            f"{sp.name} の声の指定方法にあたるTTSサーバーがないため、"
            f"「{'テキスト指定' if fallback == 'caption' else '参照音声'}」に切り替えました。"
        )
        sp.voice_mode = fallback

    # A speaker pointing at a voice the TTS servers do not have fails on every
    # single sentence, which just shows up as silence. Catch it here instead.
    ref_speakers = [
        settings.speaker(i)
        for i in range(settings.active_speaker_count())
        if settings.speaker(i).voice_mode == "reference"
    ]
    if ref_speakers:
        available = {v["id"] for v in await voicelib.list_remote(settings.ref_endpoints())}
        for sp in ref_speakers:
            if sp.voice_id in available or sp.voice_id == "none":
                continue
            if available:
                replacement = sorted(available)[0]
                notes.append(
                    f"{sp.name} の参照音声「{sp.voice_id}」がTTSサーバーにないため、"
                    f"「{replacement}」に切り替えました。"
                )
                sp.voice_id = replacement
            else:
                notes.append(
                    f"{sp.name} の参照音声「{sp.voice_id}」がありません。"
                    "参照音声をアップロードするか、声の指定方法をテキストにしてください。"
                )

    # Two speakers sharing one voice is legal but sounds like one person.
    if settings.active_speaker_count() >= 2:
        a, b = settings.speaker(0), settings.speaker(1)
        same_ref = (
            a.voice_mode == b.voice_mode == "reference" and a.voice_id == b.voice_id
        )
        same_caption = (
            a.voice_mode == b.voice_mode == "caption"
            and a.voice_caption.strip() == b.voice_caption.strip()
        )
        if same_ref:
            notes.append(
                f"{a.name} と {b.name} が同じ参照音声を使っています。"
                "別の音声を割り当てないと2人が同じ声になります。"
            )
        elif same_caption:
            notes.append(
                f"{a.name} と {b.name} の声の説明が同じです。"
                "説明を変えないと2人が同じ声になります。"
            )
    return notes


@app.get("/api/options")
async def options() -> dict:
    models = await engine.llm.list_models()
    health = {}
    for ep in settings.live_endpoints():
        health[ep.url] = await engine.tts_client.health(ep.url)
    return {
        "models": models,
        "voices": await _voice_list(),
        "tts_health": health,
    }


@app.post("/api/tts/probe")
async def probe_tts(body: ProbeBody) -> dict:
    url = TTSEndpoint(url=body.url).normalized().url
    if not url:
        raise HTTPException(status_code=400, detail="URLが空です")
    return {"url": url, **await engine.tts_client.probe(url)}


def _normalize_url(raw: str) -> str:
    url = raw.strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = f"http://{url}"
    return url


@app.post("/api/llm/probe")
async def probe_llm(body: ProbeBody) -> dict:
    """Check an LM Studio address answers, and list what it can run."""
    url = _normalize_url(body.url)
    if not url:
        raise HTTPException(status_code=400, detail="URLが空です")
    previous = settings.llm_url
    settings.llm_url = url
    try:
        models = await engine.llm.list_models()
    finally:
        settings.llm_url = previous
    if not models:
        return {
            "url": url,
            "ok": False,
            "error": "応答がないか、対話できるモデルがありません。"
            "LM Studio の Server を起動し、Serve on Local Network を有効にしてください。",
        }
    return {"url": url, "ok": True, "models": models}


@app.post("/api/search/probe")
async def probe_search(body: ProbeBody) -> dict:
    """Check a SearXNG address answers, and that JSON output is enabled."""
    url = _normalize_url(body.url)
    if not url:
        raise HTTPException(status_code=400, detail="URLが空です")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{url}/search",
                params={"q": "test", "format": "json", "language": "ja"},
            )
        if resp.status_code != 200:
            return {"url": url, "ok": False, "error": f"HTTP {resp.status_code}"}
        data = resp.json()
    except json.JSONDecodeError:
        return {
            "url": url,
            "ok": False,
            "error": "JSONが返りません。settings.yml の search.formats に json を追加してください。",
        }
    except Exception as e:  # noqa: BLE001
        return {"url": url, "ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"url": url, "ok": True, "results": len(data.get("results", []))}


@app.post("/api/mcp/probe")
async def probe_mcp(body: ProbeBody) -> dict:
    previous = settings.mcp_url
    settings.mcp_url = body.url.strip() or previous
    try:
        return {"url": settings.mcp_url, **await engine.memory.ping()}
    finally:
        settings.mcp_url = previous


@app.get("/api/voices")
async def list_voices() -> dict:
    return {"voices": await _voice_list()}


@app.post("/api/voices")
async def upload_voice(
    file: UploadFile = File(...),
    name: str = Form(default=""),
    start: float = Form(default=0.0),
    max_seconds: float = Form(default=0.0),
) -> dict:
    data = await file.read()
    endpoints = settings.ref_endpoints()
    limit = max_seconds if max_seconds > 0 else settings.ref_target_seconds
    try:
        voice_id, out_name, wav, duration = voicelib.prepare(
            settings.audio_dir,
            file.filename or "voice",
            data,
            name=name,
            start=max(0.0, start),
            max_seconds=limit,
        )
        results = await voicelib.upload_remote(endpoints, voice_id, out_name, wav)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    voicelib.cache_put(settings.voices_cache_dir, voice_id, wav)
    logger.info("uploaded reference voice %s (%.1fs) -> %s", voice_id, duration, results)
    return {
        "voice": {
            "id": voice_id,
            "file": out_name,
            "original": file.filename,
            "duration": round(duration, 1),
        },
        "endpoints": results,
        "voices": await _voice_list(),
    }


@app.post("/api/voices/{voice_id}/rename")
async def rename_voice(voice_id: str, body: RenameBody) -> dict:
    """Set the name shown for a voice.

    The TTS server only accepts ASCII ids, so a Japanese name cannot be the id
    itself. Renaming therefore stores a display label here and leaves the id
    alone — which also means it works for voices this machine never uploaded.
    """
    label = body.name.strip()
    if not label:
        raise HTTPException(status_code=400, detail="名前が空です")
    if len(label) > 60:
        raise HTTPException(status_code=400, detail="名前が長すぎます（60文字まで）")

    available = {v["id"] for v in await voicelib.list_remote(settings.ref_endpoints())}
    if voice_id not in available:
        raise HTTPException(status_code=404, detail=f"参照音声が見つかりません: {voice_id}")

    clash = [vid for vid, name in settings.voice_labels.items()
             if name == label and vid != voice_id]
    if label in available and label != voice_id or clash:
        raise HTTPException(status_code=409, detail=f"「{label}」は既に使われています")

    if label == voice_id:
        settings.voice_labels.pop(voice_id, None)
    else:
        settings.voice_labels[voice_id] = label
    save_overrides(settings)

    return {
        "id": voice_id,
        "label": settings.label_for(voice_id),
        "voices": await _voice_list(),
    }


async def _voice_list() -> list[dict]:
    voices = await voicelib.list_remote(settings.ref_endpoints())
    for v in voices:
        v["label"] = settings.label_for(v["id"])
    return voices


@app.delete("/api/voices/{voice_id}")
async def delete_voice(voice_id: str) -> dict:
    in_use = [sp.name for sp in settings.speakers
              if sp.voice_mode == "reference" and sp.voice_id == voice_id]
    endpoints = settings.ref_endpoints()
    try:
        results = await voicelib.delete_remote(endpoints, voice_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    voicelib.cache_drop(settings.voices_cache_dir, voice_id)
    settings.voice_labels.pop(voice_id, None)
    save_overrides(settings)
    return {
        "deleted": voice_id,
        "endpoints": results,
        # Deleting a voice a speaker still points at is allowed, but synthesis
        # for that speaker will fail until it is pointed somewhere else.
        "in_use_by": in_use,
        "voices": await _voice_list(),
    }


@app.get("/audio/{name}")
async def audio(name: str) -> FileResponse:
    path = (settings.audio_dir / name).resolve()
    if path.parent != settings.audio_dir.resolve() or not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type="audio/wav")


@app.get("/api/transcript")
async def transcript() -> dict:
    path: Path = engine.transcript.path
    if not path.is_file():
        return {"path": str(path), "turns": []}
    turns = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            with contextlib.suppress(json.JSONDecodeError):
                turns.append(json.loads(line))
    return {"path": str(path), "turns": turns}


class NoCacheStatic(StaticFiles):
    """Serve the UI without caching, so an edited css/js file takes effect on
    reload instead of silently keeping the previous version."""

    def is_not_modified(self, response_headers, request_headers) -> bool:  # noqa: ANN001
        return False

    async def get_response(self, path: str, scope):  # noqa: ANN001, ANN201
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response


if STATIC_DIR.is_dir():
    app.mount("/static", NoCacheStatic(directory=STATIC_DIR), name="static")


LOOPBACK = ("127.0.0.1", "localhost", "::1", "0.0.0.0")


@app.on_event("startup")
async def _startup() -> None:
    ref, caption = settings.ref_endpoints(), settings.caption_endpoints()
    logger.info("mode=%s  searxng=%s  llm=%s", settings.mode, settings.searxng_url,
                settings.llm_url)
    logger.info("tts reference=%s caption=%s", ref, caption)

    if settings.mode == "webui":
        local = [u for u in ref + caption if any(h in u for h in LOOPBACK)]
        if local:
            logger.warning(
                "mode=webui but these TTS endpoints point at this container/host: %s — "
                "set AUTOSPEAK_TTS_REF_URLS / AUTOSPEAK_TTS_CAPTION_URLS to the machine "
                "running Irodori-TTS", local,
            )
    for ep in ref + caption:
        if not await engine.tts_client.health(ep):
            logger.warning("TTS not responding at %s", ep)

    for note in await _reconcile_speakers():
        logger.warning("%s", note)
    save_overrides(settings)


@app.on_event("shutdown")
async def _shutdown() -> None:
    await engine.stop()
