#!/usr/bin/env python3
"""End-to-end check: does the pipeline keep the speaker fed without gaps?

Simulates exactly what the browser does — consume the SSE stream, play clips
back to back (by sleeping for each clip's real duration), and report progress —
then reports every moment playback had nothing queued to play.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

import httpx

BASE = "http://127.0.0.1:8000"


class Player:
    def __init__(self) -> None:
        self.queue: list[dict] = []
        self.gaps: list[tuple[float, float]] = []
        self.played = 0
        self.spoken_seconds = 0.0
        self.started_at = time.time()
        self.first_audio_at: float | None = None
        self.stop = False

    async def run(self, client: httpx.AsyncClient) -> None:
        starving_since: float | None = None
        while not self.stop:
            if not self.queue:
                if starving_since is None:
                    starving_since = time.time()
                await asyncio.sleep(0.05)
                continue

            if starving_since is not None:
                # Silence before the very first clip is startup latency, reported
                # separately; only count starvation once speech has begun.
                if self.first_audio_at is not None:
                    gap = time.time() - starving_since
                    if gap > 0.25:
                        self.gaps.append(
                            (round(starving_since - self.started_at, 1), round(gap, 2))
                        )
                starving_since = None

            item = self.queue.pop(0)
            if self.first_audio_at is None:
                self.first_audio_at = time.time() - self.started_at
            await client.post("/api/playing", json={"turn_id": item["turn_id"], "seq": item["seq"]})
            print(f"  [{time.time()-self.started_at:6.1f}s] ▶ t{item['turn_id']}s{item['seq']} "
                  f"({item['duration']:.1f}s) {item['text'][:40]}")
            await asyncio.sleep(item["duration"])
            self.spoken_seconds += item["duration"]
            self.played += 1
            await client.post("/api/played", json={"turn_id": item["turn_id"], "seq": item["seq"]})

    def drop_from(self, turn_id: int) -> int:
        before = len(self.queue)
        self.queue = [q for q in self.queue if q["turn_id"] < turn_id]
        return before - len(self.queue)


async def listen(client: httpx.AsyncClient, player: Player, verbose: bool) -> None:
    async with client.stream("GET", "/events", timeout=None) as resp:
        async for line in resp.aiter_lines():
            if player.stop:
                return
            if not line.startswith("data: "):
                continue
            msg = json.loads(line[6:])
            kind = msg.get("type")
            if kind == "audio":
                player.queue.append(msg)
            elif kind == "turn_start":
                print(f"  [{time.time()-player.started_at:6.1f}s] ── turn {msg['turn_id']}: "
                      f"{msg['subtopic']} ({len(msg.get('sources', []))} sources)")
            elif kind == "drop":
                n = player.drop_from(msg["from_turn"])
                print(f"  [{time.time()-player.started_at:6.1f}s] ✂ dropped {n} queued clips "
                      f"from turn {msg['from_turn']}")
            elif kind == "error":
                print(f"  [{time.time()-player.started_at:6.1f}s] ⚠ {msg['message']}")
            elif kind == "state" and verbose:
                print(f"       state={msg['status']} buf={msg['buffered_seconds']}s "
                      f"ahead={msg['turns_ahead']} ttsq={msg['tts_queue']}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", default="ブラックホール")
    ap.add_argument("--switch-to", default=None, help="inject this topic mid-run")
    ap.add_argument("--switch-after", type=float, default=60.0)
    ap.add_argument("--seconds", type=float, default=180.0)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--base", default=BASE, help="WebUI base URL")
    args = ap.parse_args()

    player = Player()
    async with httpx.AsyncClient(base_url=args.base, timeout=30.0) as client:
        listener = asyncio.create_task(listen(client, player, args.verbose))
        playing = asyncio.create_task(player.run(client))

        await asyncio.sleep(0.5)
        print(f"topic: {args.topic}")
        await client.post("/api/start", json={"topic": args.topic})

        if args.switch_to:
            await asyncio.sleep(args.switch_after)
            print(f"\n>>> injecting new topic: {args.switch_to}\n")
            switch_at = time.time()
            await client.post("/api/topic", json={"topic": args.switch_to})

        await asyncio.sleep(max(0.0, args.seconds - (time.time() - player.started_at)))

        player.stop = True
        listener.cancel()
        playing.cancel()
        await client.post("/api/stop", json={})

    elapsed = time.time() - player.started_at
    print("\n=== result ===")
    print(f"wall clock       : {elapsed:.1f}s")
    print(f"clips played     : {player.played}")
    print(f"audio spoken     : {player.spoken_seconds:.1f}s")
    print(f"first audio at   : {player.first_audio_at:.1f}s"
          if player.first_audio_at else "first audio       : NEVER")
    if player.first_audio_at:
        speaking_window = elapsed - player.first_audio_at
        coverage = player.spoken_seconds / speaking_window * 100 if speaking_window > 0 else 0
        print(f"speech coverage  : {coverage:.1f}%  (100% = never silent)")
    print(f"gaps (>0.25s)    : {len(player.gaps)}")
    for at, dur in player.gaps:
        print(f"    at {at:6.1f}s  silent {dur:.2f}s")
    if args.switch_to:
        print(f"switch injected  : at {args.switch_after:.0f}s -> {args.switch_to!r}")


if __name__ == "__main__":
    asyncio.run(main())
