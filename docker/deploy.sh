#!/usr/bin/env bash
# Copy the project to a Docker host and (re)build the WebUI container there.
#
#   ./docker/deploy.sh user@dockerhost [remote-path]
#
# The image needs backend/ and static/, so the whole project is synced — minus
# the venv, the vendored TTS server and the audio cache, which are either huge
# or irrelevant to the container.
set -euo pipefail

TARGET="${1:?使い方: ./docker/deploy.sh user@host [remote-path]}"
REMOTE_PATH="${2:-~/irodori-autospeak}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "syncing $ROOT -> $TARGET:$REMOTE_PATH"
rsync -az --delete \
  --exclude '.venv/' \
  --exclude 'vendor/' \
  --exclude '__pycache__/' \
  --exclude '*.egg-info/' \
  --exclude 'audio_cache/' \
  --exclude 'samples/' \
  --exclude '.git/' \
  --exclude '.env' \
  --exclude 'settings.local.json' \
  --exclude 'docker/data/' \
  --exclude 'docker/logs/' \
  "$ROOT/" "$TARGET:$REMOTE_PATH/"

# .env and data/ are excluded above so a deploy never clobbers what the remote
# is already running with. On a first deploy there is nothing there yet, so seed
# them from this machine if they are missing.
seed_if_absent() {
  local local_file="$1" remote_file="$2"
  local remote_dir="${remote_file%/*}"
  [[ -f "$local_file" ]] || return 0
  # Paths are left unquoted on the remote side on purpose: REMOTE_PATH may start
  # with ~, and a quoted tilde is not expanded by the remote shell.
  if ssh "$TARGET" "[ -e $remote_file ]"; then
    echo "keeping remote $remote_file"
  else
    echo "seeding $remote_file"
    ssh "$TARGET" "mkdir -p $remote_dir"
    scp -q "$local_file" "$TARGET:$remote_file"
  fi
}

seed_if_absent "$ROOT/docker/.env" "$REMOTE_PATH/docker/.env"
seed_if_absent "$ROOT/docker/data/settings.local.json" "$REMOTE_PATH/docker/data/settings.local.json"

echo "building and starting on $TARGET"
# shellcheck disable=SC2029
ssh "$TARGET" "cd $REMOTE_PATH/docker \
  && { [ -f .env ] || { cp .env.example .env; echo '!! docker/.env を作りました。アドレスを確認してください'; }; } \
  && mkdir -p data logs \
  && docker compose up -d --build \
  && docker compose ps"
