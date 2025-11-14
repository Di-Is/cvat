#!/usr/bin/env bash
set -euo pipefail

SAM2_MODEL_ID=${SAM2_MODEL_ID:-}
SAM2_FUNCTION_ID=${SAM2_FUNCTION_ID:-}
SAM2_FUNCTION_FILE=${SAM2_FUNCTION_FILE:-/workspace/ai-models/tracker/sam2/func.py}
SAM2_DEVICE=${SAM2_DEVICE:-}
SAM2_EXTRA_AGENT_ARGS=${SAM2_EXTRA_AGENT_ARGS:-}
CVAT_URL=${CVAT_URL:-http://cvat_server:8080}
CVAT_ACCESS_TOKEN=${CVAT_ACCESS_TOKEN:-}

if [[ -z "$CVAT_ACCESS_TOKEN" ]]; then
  echo "[sam2-agent] CVAT_ACCESS_TOKEN is not set. Export CVAT_AGENT_TOKEN in your .env file." >&2
  exit 1
fi

if [[ -z "$SAM2_MODEL_ID" ]]; then
  echo "[sam2-agent] SAM2_MODEL_ID is not set (e.g. facebook/sam2.1-hiera-small)." >&2
  exit 1
fi

if [[ -z "$SAM2_FUNCTION_ID" ]]; then
  echo "[sam2-agent] SAM2_FUNCTION_ID is not set. Run 'cvat-cli function create-native' first and export the returned ID." >&2
  exit 1
fi

if [[ ! -f "$SAM2_FUNCTION_FILE" ]]; then
  echo "[sam2-agent] SAM2_FUNCTION_FILE '$SAM2_FUNCTION_FILE' was not found." >&2
  exit 1
fi

cmd=(
  cvat-cli
  --server-host "$CVAT_URL"
  function run-agent "$SAM2_FUNCTION_ID"
  --function-file "$SAM2_FUNCTION_FILE"
  -p "model_id=str:$SAM2_MODEL_ID"
)

if [[ -n "$SAM2_DEVICE" ]]; then
  cmd+=("-p" "device=str:$SAM2_DEVICE")
fi

if [[ -n "$SAM2_EXTRA_AGENT_ARGS" ]]; then
  # shellcheck disable=SC2206
  extra_args=( $SAM2_EXTRA_AGENT_ARGS )
  cmd+=("${extra_args[@]}")
fi

exec "${cmd[@]}"
