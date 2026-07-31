#!/usr/bin/env bash
# Stop the Irodori-TTS servers started by start_tts.sh
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$ROOT/_env.sh"

for port in "$REF_PORT" "$CAPTION_PORT"; do
  pidfile="$ROOT/logs/tts-$port.pid"
  if [[ -f "$pidfile" ]] && kill "$(cat "$pidfile")" 2>/dev/null; then
    echo "stopped :$port"
    rm -f "$pidfile"
  else
    pkill -f "irodori_openai_tts.*--port $port" 2>/dev/null && echo "stopped :$port (by match)" \
      || echo ":$port not running"
    rm -f "$pidfile"
  fi
done
