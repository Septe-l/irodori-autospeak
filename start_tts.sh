#!/usr/bin/env bash
# Start the Irodori-TTS server for this machine and wait for it to be ready.
#
# One machine runs one TTS server. A server holds exactly one checkpoint, and
# two checkpoints on the same Mac fight over memory and GPU — measured at ~17%
# slower even when only one of them is synthesizing. To use both voice styles at
# once, run this on a second machine with AUTOSPEAK_TTS_KIND=caption and add its
# address in the WebUI.
#
#   AUTOSPEAK_TTS_PORT   port to listen on               (default 8088)
#   AUTOSPEAK_TTS_KIND   reference | caption             (default reference)
#
# Stays up after this script exits. Stop it with ./stop_tts.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$ROOT/_env.sh"

TTS="$ROOT/vendor/Irodori-TTS-Server"
TTS_REPO="https://github.com/Aratako/Irodori-TTS-Server.git"
mkdir -p "$ROOT/logs"

case "$TTS_KIND" in
  reference) CHECKPOINT="Aratako/Irodori-TTS-500M-v3" ;;
  caption)   CHECKPOINT="Aratako/Irodori-TTS-600M-v3-VoiceDesign" ;;
  *) echo "AUTOSPEAK_TTS_KIND が不正です: '$TTS_KIND'（reference / caption）"; exit 2 ;;
esac

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

if curl -sf --max-time 3 "http://127.0.0.1:$TTS_PORT/health" >/dev/null 2>&1; then
  echo "[$TTS_KIND] already running on :$TTS_PORT"
else
  echo "[$TTS_KIND] starting on :$TTS_PORT ($CHECKPOINT)"
  PYTHONPATH="$TTS/src" \
  IRODORI_MODEL_DEVICE=mps \
  IRODORI_CODEC_DEVICE=mps \
  IRODORI_PRELOAD=true \
  IRODORI_HF_CHECKPOINT="$CHECKPOINT" \
  IRODORI_VOICES_DIR="$ROOT/voices" \
  nohup "$TTS/.venv/bin/python" -m irodori_openai_tts \
      --host 0.0.0.0 --port "$TTS_PORT" \
      >"$ROOT/logs/tts-$TTS_PORT.log" 2>&1 &
  echo $! > "$ROOT/logs/tts-$TTS_PORT.pid"
fi

printf "waiting for :%s " "$TTS_PORT"
for _ in $(seq 1 60); do
  if curl -sf --max-time 3 "http://127.0.0.1:$TTS_PORT/health" >/dev/null 2>&1; then
    echo "ready"
    break
  fi
  printf "."
  sleep 5
done

echo
echo "log: $ROOT/logs/tts-$TTS_PORT.log"
