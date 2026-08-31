#!/usr/bin/env sh
set -eu

export AGENT_SETTING_CONFIG="${AGENT_SETTING_CONFIG:-settings.openai.toml}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-https://inference.local/v1}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-unused}"
export MODEL_NAME="${MODEL_NAME:-openshell-routed-model}"
# Route generated code to the execd sandbox instead of running it in this
# process. execd is reached through the gateway-side relay; cuga-policy.yaml
# must declare that endpoint or the egress is denied.
export DYNACONF_ADVANCED_FEATURES__SANDBOX_MODE="${CUGA_SANDBOX_MODE:-execd}"
export DYNACONF_AUTO_APPROVE="${CUGA_AUTO_APPROVE:-false}"
# Shell tool (run_command) and pip installs. Off by default upstream; the demo
# needs them, and with SANDBOX_MODE=execd they execute inside the code sandbox
# under its OpenShell policy — not in this process.
export DYNACONF_ADVANCED_FEATURES__ENABLE_SHELL_TOOL="${CUGA_ENABLE_SHELL_TOOL:-true}"
# Index for per-workspace installs. Its host must also be listed in the execd
# network policy — egress is deny-by-default. Air-gapped: point both at your
# internal mirror.
export DYNACONF_ADVANCED_FEATURES__EXECD_PACKAGE_INDEX="${CUGA_EXECD_PACKAGE_INDEX:-https://pypi.org/simple}"
export DYNACONF_ADVANCED_FEATURES__EXECD_URL="${CUGA_EXECD_URL:-http://host.openshell.internal:44772}"
# Code running in the execd sandbox calls tools back through CUGA's registry.
# Its own loopback is not CUGA's, so the default http://localhost:8001 would
# reach nothing — point it at the relayed address instead.
export DYNACONF_SERVER_PORTS__FUNCTION_CALL_HOST="${CUGA_FUNCTION_CALL_HOST:-http://host.openshell.internal:8001}"
export CUGA_DBS_DIR="${CUGA_DBS_DIR:-/tmp/cuga/dbs}"
export CUGA_LOGGING_DIR="${CUGA_LOGGING_DIR:-/tmp/cuga/logging}"
export CUGA_FOLDER="${CUGA_FOLDER:-/tmp/cuga/policies}"
# Local embedding cache lives under writable /tmp. The model is fetched at first
# start; the OpenShell policy must allow the Hugging Face hosts or the pull is
# denied. /tmp is ephemeral, so this is paid on every restart.
#   OFFLINE=1     — air-gapped mode: requires a cache seeded into the image
#   DISABLE_XET=1 — take the classic CDN path. Xet resolves its CAS endpoint at
#                   runtime, which cannot be expressed in a host allowlist.
export FASTEMBED_CACHE_PATH="${FASTEMBED_CACHE_PATH:-/tmp/cuga/fastembed}"
export HF_HOME="${HF_HOME:-/tmp/cuga/hf}"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-0}"
export HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET:-1}"

mkdir -p "$CUGA_DBS_DIR" "$CUGA_LOGGING_DIR" "$CUGA_FOLDER" "$FASTEMBED_CACHE_PATH" "$HF_HOME" /sandbox/cuga_workspace

if [ -d /app/cuga_workspace ] && [ ! -f /sandbox/cuga_workspace/contacts.txt ]; then
  cp -R /app/cuga_workspace/. /sandbox/cuga_workspace/
fi

# Seed the baked embedding model into the writable cache on first start.
if [ -d /app/fastembed-cache ] && [ -z "$(ls -A "$FASTEMBED_CACHE_PATH" 2>/dev/null)" ]; then
  cp -R /app/fastembed-cache/. "$FASTEMBED_CACHE_PATH"/
fi

cd /sandbox

export CUGA_LOG_FILE="${CUGA_LOG_FILE:-/tmp/cuga/logging/cuga.log}"

# Start a lightweight TCP proxy in the pod's main network namespace (via nsenter)
# to bridge incoming traffic from eth0 (Pod IP) to the private sandbox netns (10.200.0.2).
if command -v nsenter >/dev/null 2>&1; then
  echo "==> Starting main network namespace TCP proxy for ports 7860, 8001..."
  
  CMD_PREFIX=""
  if [ "$(id -u)" != "0" ] && command -v sudo >/dev/null 2>&1; then
    CMD_PREFIX="sudo"
  fi

  $CMD_PREFIX nsenter -t 1 -n /app/.venv/bin/python -c '
import socket, threading

def set_sock_opts(s):
    try:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
    except OSError:
        pass

def pump(src, dst):
    """Copy src→dst until EOF, then signal half-close on dst write side only."""
    try:
        while True:
            d = src.recv(65536)
            if not d:
                break
            view = memoryview(d)
            pos = 0
            while pos < len(view):
                sent = dst.send(view[pos:])
                if sent == 0:
                    return
                pos += sent
    except OSError:
        pass
    finally:
        # Signal EOF in the dst write direction only.
        # Closing src here would abort the reverse pump still in flight.
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass

def handle(client, port):
    upstream = None
    try:
        set_sock_opts(client)
        upstream = socket.create_connection(("10.200.0.2", port), timeout=10)
        set_sock_opts(upstream)
        # client→upstream in a thread; upstream→client in this thread.
        t = threading.Thread(target=pump, args=(client, upstream), daemon=True)
        t.start()
        pump(upstream, client)
        t.join()
    except Exception:
        pass
    finally:
        for s in (client, upstream):
            if s:
                try:
                    s.close()
                except OSError:
                    pass

def serve(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    set_sock_opts(s)
    s.bind(("0.0.0.0", port))
    s.listen(256)
    while True:
        try:
            c, _ = s.accept()
            threading.Thread(target=handle, args=(c, port), daemon=True).start()
        except Exception:
            pass

for port in [7860, 8001]:
    threading.Thread(target=serve, args=(port,), daemon=True).start()
import time
while True:
    time.sleep(3600)
' &
fi

exec /app/.venv/bin/cuga start demo_crm \
  --host "${CUGA_HOST:-0.0.0.0}" \
  --read-only \
  --no-email \
  --cuga-workspace /sandbox/cuga_workspace \
  2>&1 | tee "$CUGA_LOG_FILE"
