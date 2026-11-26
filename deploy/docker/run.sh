#!/bin/sh
set -eu
umask 077

case "${1:-}" in
  visitor)
    exec python -m deploy.harden.network uvicorn business.app:app --host 0.0.0.0 --port 8080 --no-access-log --no-proxy-headers
    ;;
  control)
    exec python -m uvicorn research.control_app:app --host 0.0.0.0 --port 8081 --no-access-log
    ;;
  dashboard)
    exec python -m uvicorn management.app:app --host 0.0.0.0 --port 8082 --no-access-log
    ;;
  mysql)
    exec python -m deploy.harden.network entries.mysql.app
    ;;
  bridge)
    exec python -m entries.cowrie.bridge --in /cowrie-logs/cowrie.json --follow \
      --cursor /state/runtime/cowrie-bridge.cursor --alerts-only --canary-state "$RESEARCH_STATE_FILE" \
      --dst-host "${HONEYNET_COWRIE_HOST:-web01}"
    ;;
  employees)
    shift
    exec python -m agent_runtime.service_activity "$@"
    ;;
  backup)
    shift
    exec python -m deploy.state_backup "$@"
    ;;
  flow-import)
    shift
    exec python -m audit.flow_evidence "$@"
    ;;
  *)
    printf '%s\n' 'Expected visitor, control, dashboard, mysql, bridge, employees, backup or flow-import.' >&2
    exit 2
    ;;
esac
