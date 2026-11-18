#!/bin/sh
# Run one listener per process. All listeners bind to loopback.
set -eu
umask 077
cd "$(dirname "$0")/.."
state_dir=${HONEYNET_STATE_DIR:-var/v2}
mkdir -p "$state_dir"
export BUSINESS_STATE_FILE="$state_dir/world.json"
export RESEARCH_STATE_FILE="$state_dir/research.sqlite"
export HONEYNET_LATENCY_FILE="$state_dir/http.sqlite"
python_bin=${PYTHON_BIN:-.venv/bin/python}

case "${1:-}" in
  visitor)
    unset RESEARCH_MODEL_API_KEY RESEARCH_MODEL_BASE_URL RESEARCH_MODEL HONEYNET_DASHBOARD_PASSWORD HONEYNET_MYSQL_PASSWORD
    export HONEYNET_VISITOR_ONLY=1
    exec "$python_bin" -m uvicorn business.app:app --no-access-log --host 127.0.0.1 --port "${HONEYNET_VISITOR_PORT:-8080}"
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
    exec "$python_bin" -m entries.mysql.app
    ;;
  *)
    echo "Usage: $0 visitor|control|dashboard|mysql" >&2
    exit 2
    ;;
esac
