#!/usr/bin/env bash
# Shared by run.sh / start_tts.sh / stop_tts.sh so the shell side and the Python
# side read their ports from the same .env.
#
# Only reads simple KEY=value lines; quotes and inline comments are stripped.
# An exported shell variable of the same name wins over the .env value.

env_get() {
  local key="$1" fallback="$2" value=""
  local envfile="${ROOT:-.}/.env"

  if [[ -n "${!key-}" ]]; then
    printf '%s' "${!key}"
    return
  fi
  if [[ -f "$envfile" ]]; then
    value="$(grep -E "^[[:space:]]*${key}[[:space:]]*=" "$envfile" | tail -1 \
             | cut -d= -f2- \
             | sed -e 's/[[:space:]]*#.*$//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
                   -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/")"
  fi
  printf '%s' "${value:-$fallback}"
}

WEBUI_HOST="$(env_get AUTOSPEAK_HOST 0.0.0.0)"
WEBUI_PORT="$(env_get AUTOSPEAK_PORT 8000)"
TTS_PORT="$(env_get AUTOSPEAK_TTS_PORT 8088)"
TTS_KIND="$(env_get AUTOSPEAK_TTS_KIND reference)"
REF_URLS="$(env_get AUTOSPEAK_TTS_REF_URLS '')"
CAPTION_URLS="$(env_get AUTOSPEAK_TTS_CAPTION_URLS '')"
RUN_MODE="$(env_get AUTOSPEAK_MODE all)"
