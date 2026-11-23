#!/bin/sh
# Run one listener per process. All listeners bind to loopback.
set -eu
umask 077
cd "$(dirname "$0")/.."
state_dir=${HONEYNET_STATE_DIR:-var/v2}
mkdir -p "$state_dir"
if [ "${HONEYNET_DWS_ATOMIC:-0}" = 1 ]; then
  export BUSINESS_STATE_FILE="$state_dir/world.sqlite"
  export RESEARCH_UNIFIED_PUBLICATION="${RESEARCH_UNIFIED_PUBLICATION:-1}"
  export HONEYNET_CANARY_HANDOFFS="${HONEYNET_CANARY_HANDOFFS:-1}"
else
  export BUSINESS_STATE_FILE="$state_dir/world.json"
fi
export RESEARCH_STATE_FILE="$state_dir/research.sqlite"
export HONEYNET_LATENCY_FILE="$state_dir/http.sqlite"
export HONEYNET_ALERTS_FILE="$state_dir/alerts.sqlite"
export HONEYNET_EMPLOYEE_ACTIVITY_FILE="$state_dir/employees.sqlite"
export HONEYNET_SITUATION_REPORT_FILE="$state_dir/situations.sqlite"
export HONEYNET_FLOW_STATE_FILE="$state_dir/flow-evidence.sqlite"
case "${1:-}" in
  cowrie-events|flow-import) ;;
  visitor|mysql) [ "${HONEYNET_FLOW_DIRECT:-0}" = 1 ] || unset HONEYNET_FLOW_HMAC_KEY ;;
  *) unset HONEYNET_FLOW_HMAC_KEY HONEYNET_FLOW_INTERFACE ;;
esac
case "${1:-}" in dashboard) ;; *) unset HONEYNET_DASHBOARD_APPROVER_PASSWORD ;; esac
python_bin=${PYTHON_BIN:-.venv/bin/python}
if [ "${HONEYNET_DWS_ATOMIC:-0}" = 1 ] || [ "${HONEYNET_RELEASE_MONITOR:-0}" = 1 ] || [ "${RESEARCH_AUTOMATIC_DECOYS:-0}" = 1 ]; then
  export HONEYNET_VISITOR_HEALTH_PORT="${HONEYNET_VISITOR_HEALTH_PORT:-${HONEYNET_VISITOR_PORT:-8080}}"
fi

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
    if [ "${HONEYNET_FLOW_DIRECT:-0}" = 1 ] || [ "${HONEYNET_WEB_REQUESTS_PER_10S:-0}" != 0 ] || [ "${HONEYNET_LLM_REQUESTS_PER_10S:-0}" != 0 ]; then
      set -- --no-proxy-headers
    else
      set --
    fi
    run_public uvicorn business.app:app --no-access-log --host 127.0.0.1 --port "${HONEYNET_VISITOR_PORT:-8080}" "$@"
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
  employees)
    unset RESEARCH_MODEL_API_KEY RESEARCH_MODEL_BASE_URL RESEARCH_MODEL HONEYNET_CONTROL_TOKEN HONEYNET_DASHBOARD_PASSWORD HONEYNET_MYSQL_PASSWORD
    shift
    exec "$python_bin" -m agent_runtime.service_activity "$@"
    ;;
  flow-import)
    shift
    exec "$python_bin" -m audit.flow_evidence "$@"
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
    image=dlp-cowrie-dws:local
    if [ "${HONEYNET_INGRESS_ONLY:-0}" = 1 ]; then
      image=dlp-cowrie-isolated:local
    fi
    set -- docker run --rm --name "${HONEYNET_COWRIE_CONTAINER:-dlp-cowrie-v2}" --cap-drop ALL --security-opt no-new-privileges \
      -p "127.0.0.1:${HONEYNET_COWRIE_PORT:-2222}:2222" \
      -e "HONEYNET_COWRIE_PROJECTION=/projection/${projection##*/}" \
      -e "RESEARCH_AUTOMATIC_DECOYS=${RESEARCH_AUTOMATIC_DECOYS:-0}" \
      -e "HONEYNET_CANARY_HANDOFFS=${HONEYNET_CANARY_HANDOFFS:-0}" \
      -e "HONEYNET_SSH_COMMANDS_PER_10S=${HONEYNET_SSH_COMMANDS_PER_10S:-0}" \
      -e "COWRIE_HONEYPOT_HOSTNAME=${HONEYNET_COWRIE_HOST:-web01}" \
      --mount "type=bind,source=$projection_dir,target=/projection,readonly"
    if [ -n "${HONEYNET_EMPLOYEE_COWRIE_USERDB:-}" ]; then
      employee_userdb=$(realpath "$HONEYNET_EMPLOYEE_COWRIE_USERDB")
      [ -f "$employee_userdb" ] || exit 2
      set -- "$@" --mount "type=bind,source=$employee_userdb,target=/cowrie/cowrie-git/etc/userdb.txt,readonly"
    fi
    exec "$@" "${HONEYNET_COWRIE_IMAGE:-$image}"
    ;;
  cowrie-events)
    docker exec "${HONEYNET_COWRIE_CONTAINER:-dlp-cowrie-v2}" \
      /cowrie/cowrie-env/bin/python -u -c "$(cat entries/cowrie/runtime/follow_events.py)" \
      /cowrie/cowrie-git/var/log/cowrie/cowrie.json |
      "$python_bin" -m entries.cowrie.bridge --in /dev/stdin --alerts-only --canary-state "$RESEARCH_STATE_FILE"
    exit 1
    ;;
  *)
    echo "Usage: $0 visitor|control|dashboard|mysql|cowrie|cowrie-events|employees|flow-import" >&2
    exit 2
    ;;
esac
