"""Reference-voice management, over the TTS servers' own REST API.

Reference audio has to live next to the model that clones it, and the TTS
servers may be on a different machine from this one, so voices are pushed to
every reference endpoint rather than written to a local directory. Each endpoint
keeps its own copy — uploads and deletes are fanned out to all of them.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import shutil
import subprocess
import wave
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# What the TTS server itself accepts as a reference file.
ACCEPTED = {".wav", ".flac", ".mp3", ".m4a", ".ogg", ".opus", ".aac", ".webm"}
# Anything not already a wav is converted, because the TTS server reaches for
# ffmpeg to decode compressed audio and ffmpeg is not installed here.
NATIVE = {".wav", ".flac"}

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
# The TTS server rejects anything outside this set, so a Japanese filename
# cannot be used as a voice id even though it is a perfectly good filename.
UNSAFE = re.compile(r"[^0-9A-Za-z_-]+")


def safe_stem(name: str) -> str:
    stem = UNSAFE.sub("_", Path(name).stem.strip()).strip("_")[:48]
    if stem:
        return stem
    # Nothing ASCII survived (e.g. "慈.m4a"). Fall back to a stable-looking
    # generated id; the original filename is reported back to the user.
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
    return f"voice_{digest}"


def wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:  # noqa: BLE001
        return 0.0


def converter() -> tuple[str, list[str]] | None:
    """Pick whichever decoder this machine has.

    macOS ships afconvert, so no extra install is needed there; Linux images
    carry ffmpeg. Returning None means only wav/flac uploads can be accepted.
    """
    if shutil.which("afconvert"):
        return "afconvert", ["-f", "WAVE", "-d", "LEI16@24000", "-c", "1"]
    if shutil.which("ffmpeg"):
        return "ffmpeg", ["-y", "-loglevel", "error", "-ac", "1", "-ar", "24000"]
    return None


def convert_to_wav(src: Path, dest: Path) -> None:
    """Decode to 24kHz mono wav."""
    chosen = converter()
    if chosen is None:
        raise ValueError(
            "wav/flac 以外を変換するには afconvert (macOS) か ffmpeg が必要です"
        )
    name, flags = chosen
    cmd = (
        ["afconvert", *flags, str(src), str(dest)]
        if name == "afconvert"
        else ["ffmpeg", "-i", str(src), *flags, str(dest)]
    )
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)


def trim_wav(path: Path, start: float, length: float) -> tuple[bytes, float]:
    """Cut [start, start+length] out of a wav. Returns (bytes, duration)."""
    with wave.open(str(path), "rb") as src:
        rate = src.getframerate()
        total = src.getnframes()
        begin = max(0, min(int(start * rate), total))
        count = total - begin if length <= 0 else min(int(length * rate), total - begin)
        if count <= 0:
            raise ValueError("切り出し位置が音声の長さを超えています")
        src.setpos(begin)
        frames = src.readframes(count)
        params = (src.getnchannels(), src.getsampwidth(), rate)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as dst:
        dst.setnchannels(params[0])
        dst.setsampwidth(params[1])
        dst.setframerate(params[2])
        dst.writeframes(frames)
    return buf.getvalue(), count / float(rate)


def prepare(
    work_dir: Path,
    filename: str,
    data: bytes,
    *,
    name: str | None = None,
    start: float = 0.0,
    max_seconds: float = 15.0,
) -> tuple[str, str, bytes, float]:
    """Validate an upload, normalize it to wav and cut it to a usable length.

    Returns (voice_id, upload filename, bytes, duration). Conversion happens here
    rather than on the TTS server because that server reaches for ffmpeg to
    decode compressed audio and may not have it. Everything is routed through wav
    so the trim can be done with the stdlib.
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in ACCEPTED:
        raise ValueError(f"対応していない形式です: {suffix or '(拡張子なし)'}")
    if not data:
        raise ValueError("ファイルが空です")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("ファイルが大きすぎます（50MBまで）")

    voice_id = safe_stem(name) if (name or "").strip() else safe_stem(filename)
    work_dir.mkdir(parents=True, exist_ok=True)
    src = work_dir / f".prep_src_{voice_id}{suffix or '.bin'}"
    wav_path = work_dir / f".prep_{voice_id}.wav"
    src.write_bytes(data)

    try:
        if suffix == ".wav":
            wav_path.write_bytes(data)
        else:
            # flac included: decoding it here keeps one trim path for everything.
            convert_to_wav(src, wav_path)

        duration = wav_duration(wav_path)
        if duration <= 0:
            raise ValueError("音声の長さを読み取れませんでした")

        if start > 0 or (max_seconds > 0 and duration > max_seconds):
            out, duration = trim_wav(wav_path, start, max_seconds)
        else:
            out = wav_path.read_bytes()

        return voice_id, f"{voice_id}.wav", out, duration
    except subprocess.CalledProcessError as e:
        raise ValueError(
            f"音声の変換に失敗しました: {e.stderr.decode('utf-8', 'replace')[:200]}"
        ) from e
    finally:
        src.unlink(missing_ok=True)
        wav_path.unlink(missing_ok=True)


def cache_path(cache_dir: Path, voice_id: str) -> Path:
    return cache_dir / f"{voice_id}.wav"


def cache_put(cache_dir: Path, voice_id: str, data: bytes) -> None:
    """Keep the converted wav so the voice can later be renamed.

    The TTS server has no rename call and will not hand a file back, so renaming
    means re-uploading — which needs the bytes on this side.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path(cache_dir, voice_id).write_bytes(data)


def cache_drop(cache_dir: Path, voice_id: str) -> None:
    cache_path(cache_dir, voice_id).unlink(missing_ok=True)


async def list_remote(endpoints: list[str], timeout: float = 15.0) -> list[dict]:
    """Voices available on the first reachable reference endpoint."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        for ep in endpoints:
            try:
                resp = await client.get(f"{ep}/v1/audio/voices")
                resp.raise_for_status()
            except Exception as e:  # noqa: BLE001
                logger.warning("voice list failed on %s: %s: %s", ep, type(e).__name__, e)
                continue
            return [
                {"id": v["id"], "file": Path(v.get("ref_wav") or "").name or v["id"]}
                for v in resp.json().get("data", [])
                if not v.get("no_ref")
            ]
    return []


async def upload_remote(
    endpoints: list[str], voice_id: str, filename: str, data: bytes, timeout: float = 60.0
) -> dict:
    """Push one voice to every reference endpoint.

    Each TTS server has its own voices directory, so a voice that only reaches
    some of them would make synthesis fail whenever a sentence lands on one of
    the others.
    """
    results: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=timeout) as client:
        for ep in endpoints:
            try:
                resp = await client.post(
                    f"{ep}/v1/audio/voices",
                    files={"file": (filename, data, "application/octet-stream")},
                    data={"voice_id": voice_id},
                )
                if resp.status_code == 409:
                    resp = await client.put(
                        f"{ep}/v1/audio/voices/{voice_id}",
                        files={"file": (filename, data, "application/octet-stream")},
                    )
                resp.raise_for_status()
                results[ep] = "ok"
            except httpx.HTTPStatusError as e:
                # The server's own message is the useful part; the status line
                # alone does not say why it refused.
                detail = e.response.text[:300]
                logger.error("voice upload failed on %s: %s", ep, detail)
                results[ep] = f"HTTP {e.response.status_code}: {detail}"
            except Exception as e:  # noqa: BLE001
                logger.error("voice upload failed on %s: %s: %s", ep, type(e).__name__, e)
                results[ep] = f"{type(e).__name__}: {e}"

    if not any(v == "ok" for v in results.values()):
        raise ValueError(f"どのTTSサーバーにも登録できませんでした: {results}")
    return results


async def delete_remote(endpoints: list[str], voice_id: str, timeout: float = 30.0) -> dict:
    if UNSAFE.search(voice_id):
        # Files dropped into the voices directory by hand can have any name, and
        # the TTS server will happily list them — but its API only addresses
        # ASCII ids, so it cannot delete them either.
        raise ValueError(
            f"「{voice_id}」はTTSサーバーが扱えない名前（ASCII以外）です。"
            "voices ディレクトリから直接削除してください。"
        )

    results: dict[str, str] = {}
    found = False
    missing = 0
    async with httpx.AsyncClient(timeout=timeout) as client:
        for ep in endpoints:
            try:
                resp = await client.delete(f"{ep}/v1/audio/voices/{voice_id}")
                if resp.status_code == 404:
                    results[ep] = "not found"
                    missing += 1
                    continue
                resp.raise_for_status()
                results[ep] = "deleted"
                found = True
            except httpx.HTTPStatusError as e:
                detail = e.response.text[:300]
                logger.error("voice delete failed on %s: %s", ep, detail)
                results[ep] = f"HTTP {e.response.status_code}: {detail}"
            except Exception as e:  # noqa: BLE001
                logger.error("voice delete failed on %s: %s: %s", ep, type(e).__name__, e)
                results[ep] = f"{type(e).__name__}: {e}"

    if not found:
        if missing == len(endpoints):
            raise FileNotFoundError(f"参照音声が見つかりません: {voice_id}")
        raise ValueError(f"削除できませんでした: {results}")
    return results
