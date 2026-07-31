#!/usr/bin/env python3
"""Keep an LM Studio model generating continuously, to measure how much GPU
contention it creates for Irodori-TTS running on the same machine."""

from __future__ import annotations

import argparse
import json
import time
import urllib.request

PROMPT = "宇宙物理学について、日本語で詳しく解説を続けてください。途中で止めないでください。"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:1234")
    ap.add_argument("--model", default="qwen3.6-35b-a3b-mlx")
    ap.add_argument("--seconds", type=float, default=300.0)
    args = ap.parse_args()

    deadline = time.time() + args.seconds
    rounds = 0
    tokens = 0
    while time.time() < deadline:
        body = json.dumps({
            "model": args.model,
            "messages": [{"role": "user", "content": PROMPT}],
            "max_tokens": 800,
            "temperature": 0.8,
            "stream": True,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{args.url}/v1/chat/completions", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                for raw in resp:
                    if time.time() > deadline:
                        break
                    if raw.startswith(b"data: "):
                        tokens += 1
        except Exception as e:  # noqa: BLE001
            print(f"load round failed: {type(e).__name__}: {e}", flush=True)
            time.sleep(2)
        rounds += 1
        print(f"load round {rounds} done, ~{tokens} chunks", flush=True)

    print(f"LOAD FINISHED: {rounds} rounds, ~{tokens} stream chunks", flush=True)


if __name__ == "__main__":
    main()
