#!/usr/bin/env bash
# Stop the Irodori-TTS server started by start_tts.sh
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$ROOT/_env.sh"

pidfile="$ROOT/logs/tts-$TTS_PORT.pid"
if [[ -f "$pidfile" ]] && kill "$(cat "$pidfile")" 2>/dev/null; then
  echo "stopped :$TTS_PORT"
elif pkill -f "irodori_openai_tts.*--port $TTS_PORT" 2>/dev/null; then
  echo "stopped :$TTS_PORT (by match)"
else
  echo ":$TTS_PORT not running"
fi
rm -f "$pidfile"
