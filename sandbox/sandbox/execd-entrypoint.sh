#!/usr/bin/env bash
# Start Jupyter (backs execd's /code endpoint), then execd in the foreground.
set -euo pipefail

# Redirect all Jupyter/IPython state to /tmp so any arbitrary non-root UID can write to it
export JUPYTER_CONFIG_DIR=/tmp/.jupyter
export JUPYTER_DATA_DIR=/tmp/.local/share/jupyter
export JUPYTER_RUNTIME_DIR=/tmp/.local/share/jupyter/runtime
export IPYTHONDIR=/tmp/.ipython

EXECD_PORT="${EXECD_PORT:-44772}"
JUPYTER_PORT="${JUPYTER_PORT:-54321}"
# Generated per start: the token only ever travels over this container's loopback.
JUPYTER_TOKEN="${JUPYTER_TOKEN:-$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')}"

mkdir -p /workspace

jupyter notebook \
  --ip=127.0.0.1 \
  --port="$JUPYTER_PORT" \
  --no-browser \
  --NotebookApp.token="$JUPYTER_TOKEN" \
  --notebook-dir=/workspace \
  >/tmp/jupyter.log 2>&1 &

for _ in $(seq 1 90); do
  if curl -sf -o /dev/null "http://127.0.0.1:${JUPYTER_PORT}/api" ; then
    break
  fi
  sleep 1
done

if ! curl -sf -o /dev/null "http://127.0.0.1:${JUPYTER_PORT}/api"; then
  echo "jupyter did not become ready after 90s; last log lines:" >&2
  tail -30 /tmp/jupyter.log >&2
  exit 1
fi

exec /usr/local/bin/execd \
  --jupyter-host="http://127.0.0.1:${JUPYTER_PORT}" \
  --jupyter-token="$JUPYTER_TOKEN" \
  --port="$EXECD_PORT" \
  --isolation-config=/etc/opensandbox/isolation.toml
