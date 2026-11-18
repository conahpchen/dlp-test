#!/bin/sh
# Full local rehearsal on synthetic state and loopback listeners.
set -eu
cd "$(dirname "$0")/.."
python_bin=${PYTHON_BIN:-.venv/bin/python}
: "${HONEYNET_TEST_COWRIE_IMAGE:?set the approved Cowrie test image}"
: "${HONEYNET_TEST_COWRIE_ISOLATED_IMAGE:?set the built isolated Cowrie test image}"
: "${HONEYNET_TEST_L3_MYSQL_IMAGE:?set the approved digest-pinned MySQL test image}"
: "${HONEYNET_TEST_L3_HTTP_IMAGE:?set the approved digest-pinned Nginx test image}"
for image in "$HONEYNET_TEST_COWRIE_IMAGE" "$HONEYNET_TEST_COWRIE_ISOLATED_IMAGE" \
  "$HONEYNET_TEST_L3_MYSQL_IMAGE" "$HONEYNET_TEST_L3_HTTP_IMAGE"; do
  docker image inspect "$image" >/dev/null
done
"$python_bin" -m pytest -q
"$python_bin" -m replay.harness verify --world tests/fixtures/world_seed.demo.json --events tests/fixtures/events.demo.jsonl
"$python_bin" -m replay.acceptance
