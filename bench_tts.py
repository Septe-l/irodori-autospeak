#!/usr/bin/env python3
"""Phase 0 benchmark: how many seconds does Irodori-TTS need per sentence on MPS?

Measures wall-clock synthesis time and the real-time factor (RTF = synth_time /
audio_duration) across num_steps settings. RTF < 1.0 means the machine can
produce speech faster than it is spoken, which is the condition for gapless
continuous playback from a single worker.
"""

from __future__ import annotations

import argparse
import io
import json
import statistics
import time
import urllib.error
import urllib.request
import wave

# Sentences sized like the ones the LLM will emit: short, conversational, 20-45 chars.
SENTENCES = [
    "今日はね、宇宙の話をしようと思うんだ。",
    "ブラックホールって、光すら逃げられないくらい重力が強いんだよ。",
    "でもね、実は少しずつ蒸発してるって説があるの。",
    "ホーキング放射っていうんだけど、面白いよね。",
    "めぐちゃん、この話けっこう好きなんだ。",
]


def synth(base_url: str, text: str, num_steps: int, voice: str | None,
          caption: str | None, timeout: float) -> tuple[float, bytes]:
    irodori: dict = {"num_steps": num_steps, "chunking_enabled": False}
    if caption is not None:
        irodori["caption"] = caption
    payload: dict = {
        "model": "irodori-tts",
        "input": text,
        "response_format": "wav",
        "irodori": irodori,
    }
    if voice is not None:
        payload["voice"] = voice

    req = urllib.request.Request(
        f"{base_url}/v1/audio/speech",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return time.perf_counter() - t0, body


def wav_duration(data: bytes) -> float:
    with wave.open(io.BytesIO(data), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def run(base_url: str, num_steps: int, voice: str | None, caption: str | None,
        timeout: float, label: str) -> dict | None:
    times: list[float] = []
    rtfs: list[float] = []
    chars: list[int] = []

    for text in SENTENCES:
        try:
            elapsed, body = synth(base_url, text, num_steps, voice, caption, timeout)
        except urllib.error.HTTPError as e:
            print(f"  [{label}] HTTP {e.code}: {e.read()[:300].decode('utf-8', 'replace')}")
            return None
        except Exception as e:  # noqa: BLE001
            print(f"  [{label}] FAILED: {type(e).__name__}: {e}")
            return None

        dur = wav_duration(body)
        times.append(elapsed)
        rtfs.append(elapsed / dur if dur > 0 else float("inf"))
        chars.append(len(text))
        print(f"  {len(text):3d}ch -> synth {elapsed:6.2f}s | audio {dur:5.2f}s | RTF {elapsed/dur:5.2f}")

    result = {
        "label": label,
        "num_steps": num_steps,
        "median_synth_s": statistics.median(times),
        "mean_synth_s": statistics.mean(times),
        "max_synth_s": max(times),
        "median_rtf": statistics.median(rtfs),
        "sentences": len(times),
    }
    print(f"  => median {result['median_synth_s']:.2f}s/sentence, median RTF {result['median_rtf']:.2f}")
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8088")
    ap.add_argument("--voice", default=None, help="voice id, or 'none' for no reference")
    ap.add_argument("--caption", default=None, help="text voice description (VoiceDesign ckpt only)")
    ap.add_argument("--steps", default="40,24,16", help="comma-separated num_steps values")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--tag", default="", help="label suffix, e.g. 'llm-idle' / 'llm-busy'")
    ap.add_argument("--out", default=None, help="write JSON results here")
    args = ap.parse_args()

    results = []
    for steps in [int(s) for s in args.steps.split(",")]:
        label = f"steps={steps}" + (f" {args.tag}" if args.tag else "")
        print(f"\n--- {label} ---")
        r = run(args.url, steps, args.voice, args.caption, args.timeout, label)
        if r:
            r["tag"] = args.tag
            results.append(r)

    print("\n=== summary ===")
    for r in results:
        print(f"{r['label']:28} median {r['median_synth_s']:6.2f}s/sent  "
              f"max {r['max_synth_s']:6.2f}s  RTF {r['median_rtf']:5.2f}")

    if args.out:
        with open(args.out, "w") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
