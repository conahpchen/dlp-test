#!/bin/sh
# Run one listener per process. All listeners bind to loopback.
set -eu
umask 077
cd "$(dirname "$0")/.."
state_dir=${HONEYNET_STATE_DIR:-var/v2}
mkdir -p "$state_dir"
if [ "${HONEYNET_DWS_ATOMIC:-0}" = 1 ]; then
  export BUSINESS_STATE_FILE="$state_dir/world.sqlite"
else
  export BUSINESS_STATE_FILE="$state_dir/world.json"
fi
export RESEARCH_STATE_FILE="$state_dir/research.sqlite"
export HONEYNET_LATENCY_FILE="$state_dir/http.sqlite"
python_bin=${PYTHON_BIN:-.venv/bin/python}

run_public() {
  if [ "${HONEYNET_INGRESS_ONLY:-0}" = 1 ]; then
    exec "$python_bin" -m deploy.harden.network "$@"
  fi
  exec "$python_bin" -m "$@"
}

case "${1:-}" in
  visitor)
    unset RESEARCH_MODEL_API_KEY RESEARCH_MODEL_BASE_URL RESEARCH_MODEL HONEYNET_DASHBOARD_PASSWORD HONEYNET_MYSQL_PASSWORD
    export HONEYNET_VISITOR_ONLY=1
    if [ "${HONEYNET_INGRESS_ONLY:-0}" = 1 ]; then
      unset HONEYNET_PG_DSN HONEYNET_CONTROL_TOKEN
    fi
    run_public uvicorn business.app:app --no-access-log --host 127.0.0.1 --port "${HONEYNET_VISITOR_PORT:-8080}"
    ;;
  control)
    unset HONEYNET_DASHBOARD_PASSWORD HONEYNET_MYSQL_PASSWORD
    : "${HONEYNET_CONTROL_TOKEN:?set the research control token}"
    exec "$python_bin" -m uvicorn research.control_app:app --no-access-log --host 127.0.0.1 --port "${HONEYNET_CONTROL_PORT:-8081}"
    ;;
  dashboard)
    unset RESEARCH_MODEL_API_KEY RESEARCH_MODEL_BASE_URL RESEARCH_MODEL HONEYNET_CONTROL_TOKEN HONEYNET_MYSQL_PASSWORD
    : "${HONEYNET_DASHBOARD_PASSWORD:?set the dashboard password}"
    exec "$python_bin" -m uvicorn management.app:app --host 127.0.0.1 --port "${HONEYNET_DASHBOARD_PORT:-8082}"
    ;;
  mysql)
    unset RESEARCH_MODEL_API_KEY RESEARCH_MODEL_BASE_URL RESEARCH_MODEL HONEYNET_CONTROL_TOKEN HONEYNET_DASHBOARD_PASSWORD
    run_public entries.mysql.app
    ;;
  cowrie)
    projection=${HONEYNET_COWRIE_EXPORT:?set the path to an exported world.json}
    case "${projection##*/}" in
      world.json|world.sqlite) ;;
      *) echo "Cowrie export must be named world.json or world.sqlite" >&2; exit 2 ;;
    esac
    [ -r "$projection" ] || { echo "Export a DWS projection before starting Cowrie" >&2; exit 2; }
    projection_dir=$(cd "$(dirname "$projection")" && pwd -P)
    checkpoint_dir=$(cd "$state_dir" && pwd -P)
    [ "$projection_dir" != "$checkpoint_dir" ] || {
      echo "Cowrie requires a dedicated export directory" >&2; exit 2;
    }
    exec docker run --rm -p "127.0.0.1:${HONEYNET_COWRIE_PORT:-2222}:2222" \
      -e "HONEYNET_COWRIE_PROJECTION=/projection/${projection##*/}" \
      -e "COWRIE_HONEYPOT_HOSTNAME=${HONEYNET_COWRIE_HOST:-web01}" \
      --mount "type=bind,source=$projection_dir,target=/projection,readonly" \
      "${HONEYNET_COWRIE_IMAGE:-dlp-cowrie-dws:local}"
    ;;
  *)
    echo "Usage: $0 visitor|control|dashboard|mysql|cowrie" >&2
    exit 2
    ;;
esac
