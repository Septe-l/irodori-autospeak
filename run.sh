#!/usr/bin/env bash
# Start the tool.
#
#   ./run.sh                 このMacでWebUIとTTS1つを起動（既定）
#   ./run.sh --webui-only    WebUIだけ。TTSは別マシン
#   ./run.sh --tts-only      TTSだけ。WebUIは別マシン
#
# 1台につきTTSは1つです。既定の v4 は参照音声とテキスト指定の両方を1つの
# チェックポイントで扱うので、通常はこれ1台で足ります。使うモデルを変える場合は
# .env の AUTOSPEAK_TTS_CHECKPOINT を書き換えてください。
#
# 既定モードは .env の AUTOSPEAK_MODE=all|webui|tts でも指定できます。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "$ROOT/.env" ]]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo ".env を作りました。SearXNG と LM Studio の URL を確認してください: $ROOT/.env"
fi

source "$ROOT/_env.sh"

MODE="$RUN_MODE"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --webui-only|--webui) MODE=webui ;;
    --tts-only|--tts)     MODE=tts ;;
    --all)                MODE=all ;;
    -h|--help)
      # Print the header comment, stopping at the first line of actual code.
      awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "${BASH_SOURCE[0]}"
      exit 0 ;;
    *) echo "不明なオプション: $1  （--webui-only / --tts-only / --all）"; exit 2 ;;
  esac
  shift
done

case "$MODE" in
  all|webui|tts) ;;
  *) echo "AUTOSPEAK_MODE が不正です: '$MODE'  （all / webui / tts）"; exit 2 ;;
esac

start_webui() {
  if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
    echo "本体の依存を入れます"
    uv venv --python 3.12 --directory "$ROOT"
    uv pip install --directory "$ROOT" -e "$ROOT"
  fi
  echo
  echo "starting WebUI on http://localhost:$WEBUI_PORT  (mode: $MODE)"
  # A --webui-only flag has to reach the app too, or its own endpoint check
  # would run against whatever AUTOSPEAK_MODE says in .env.
  export AUTOSPEAK_MODE="$MODE"
  exec "$ROOT/.venv/bin/python" -m uvicorn backend.server:app \
    --host "$WEBUI_HOST" --port "$WEBUI_PORT" --app-dir "$ROOT"
}

# In webui mode nothing local would answer on the default loopback endpoint, so
# an unset URL is a configuration mistake rather than a working default.
check_remote_tts() {
  if [[ -z "$REF_URLS" && -z "$CAPTION_URLS" ]]; then
    cat >&2 <<EOF
--webui-only では TTS の接続先を .env に書いてください。空だと127.0.0.1を見にいきます。

  AUTOSPEAK_TTS_REF_URLS=http://<参照音声TTSのホスト>:$TTS_PORT
  AUTOSPEAK_TTS_CAPTION_URLS=http://<テキスト指定TTSのホスト>:$TTS_PORT
EOF
    exit 2
  fi

  local unreachable=0
  for url in ${REF_URLS//,/ } ${CAPTION_URLS//,/ }; do
    if curl -sf --max-time 5 "$url/health" >/dev/null 2>&1; then
      echo "  TTS OK    $url"
    else
      echo "  TTS 未応答 $url"
      unreachable=1
    fi
  done
  # Not fatal: the TTS machine may still be booting, and the WebUI retries.
  if [[ $unreachable -eq 1 ]]; then
    echo "  → 応答がないTTSがあります。相手側で ./run.sh --tts-only を動かしてください。"
  fi
}

case "$MODE" in
  all)
    # Fetches and builds Irodori-TTS-Server if it is not there yet.
    "$ROOT/start_tts.sh"
    start_webui
    ;;
  tts)
    "$ROOT/start_tts.sh"
    echo
    echo "TTS（$TTS_CHECKPOINT）のみ起動しました。WebUIは別マシンで --webui-only で動かしてください。"
    echo "停止: ./stop_tts.sh"
    ;;
  webui)
    check_remote_tts
    start_webui
    ;;
esac
