#!/usr/bin/env bash
# Start both Irodori-TTS servers in the background and wait for them to be ready.
#   AUTOSPEAK_TTS_REF_PORT      Irodori-TTS-500M-v3           -> reference-audio cloning
#   AUTOSPEAK_TTS_CAPTION_PORT  Irodori-TTS-600M-v3-VoiceDesign -> text-described voices
# They stay up after this script exits. Stop them with ./stop_tts.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$ROOT/_env.sh"

TTS="$ROOT/vendor/Irodori-TTS-Server"
TTS_REPO="https://github.com/Aratako/Irodori-TTS-Server.git"
mkdir -p "$ROOT/logs"

# Irodori-TTS-Server is not vendored into this repository — it stays an
# independent clone so upstream fixes can be pulled with `git -C vendor/... pull`.
# Fetch and build it on first run so a fresh machine needs no manual step.
bootstrap_tts() {
  if [[ ! -d "$TTS/.git" ]]; then
    echo "Irodori-TTS-Server がないので取得します -> vendor/"
    mkdir -p "$ROOT/vendor"
    git clone "$TTS_REPO" "$TTS" || { echo "clone に失敗しました"; exit 1; }
    echo "取得しました: $(git -C "$TTS" rev-parse --short HEAD)"
  fi

  if [[ ! -x "$TTS/.venv/bin/python" ]]; then
    # --extra cpu on macOS still yields the normal PyPI torch build, which has
    # working MPS; the CPU-only index override is gated to linux/win32.
    echo "TTSサーバーの依存を入れます（初回は数分かかります）"
    ( cd "$TTS" && uv sync --extra cpu ) || { echo "uv sync に失敗しました"; exit 1; }
  fi
}

command -v git >/dev/null || { echo "git が必要です: brew install git"; exit 1; }
command -v uv  >/dev/null || { echo "uv が必要です: brew install uv"; exit 1; }
bootstrap_tts

start_one() {
  local port="$1" checkpoint="$2" name="$3"
  if curl -sf --max-time 3 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
    echo "[$name] already running on :$port"
    return 0
  fi
  echo "[$name] starting on :$port ($checkpoint)"
  PYTHONPATH="$TTS/src" \
  IRODORI_MODEL_DEVICE=mps \
  IRODORI_CODEC_DEVICE=mps \
  IRODORI_PRELOAD=true \
  IRODORI_HF_CHECKPOINT="$checkpoint" \
  IRODORI_VOICES_DIR="$ROOT/voices" \
  nohup "$TTS/.venv/bin/python" -m irodori_openai_tts \
      --host 0.0.0.0 --port "$port" \
      >"$ROOT/logs/tts-$port.log" 2>&1 &
  echo $! > "$ROOT/logs/tts-$port.pid"
}

start_one "$REF_PORT"     "Aratako/Irodori-TTS-500M-v3"            "reference"
start_one "$CAPTION_PORT" "Aratako/Irodori-TTS-600M-v3-VoiceDesign" "voicedesign"

for port in "$REF_PORT" "$CAPTION_PORT"; do
  printf "waiting for :%s " "$port"
  for _ in $(seq 1 60); do
    if curl -sf --max-time 3 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
      echo "ready"
      break
    fi
    printf "."
    sleep 5
  done
done

echo
echo "logs: $ROOT/logs/tts-$REF_PORT.log, $ROOT/logs/tts-$CAPTION_PORT.log"
