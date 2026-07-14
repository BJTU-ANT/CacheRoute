#!/usr/bin/env bash
set -euo pipefail

VLLM_C="vllm_lmcache_test"
REDIS_C="lmcache-redis"

GS_SCHEMA="org.gnome.Terminal.Legacy.Settings"
GS_KEY="new-terminal-mode"

# Temporarily set "new terminal behavior" to tab so --tab is always added to the current window
ORIG_MODE="$(gsettings get ${GS_SCHEMA} ${GS_KEY})"
gsettings set ${GS_SCHEMA} ${GS_KEY} 'tab'
trap 'gsettings set '"${GS_SCHEMA}"' '"${GS_KEY}"' '"${ORIG_MODE}"' >/dev/null 2>&1 || true' EXIT

echo "[1/3] Restart ${VLLM_C}"
docker restart "${VLLM_C}" >/dev/null

echo "[2/3] Open 6 vllm tabs in current window"

for i in {1..6}; do
  gnome-terminal \
    --tab --title="vllm #${i}" \
    -- bash -lc "docker exec -it ${VLLM_C} bash -lc 'cd llm-stack/CacheRoute/test && exec bash'"
  sleep 0.3
done

echo "[3/3] Restart ${REDIS_C} & open redis-cli window"
docker restart "${REDIS_C}" >/dev/null

gnome-terminal \
  --window --title="lmcache-redis redis-cli" \
  -- bash -lc "docker exec -it ${REDIS_C} redis-cli < /dev/tty"

echo "Done."
